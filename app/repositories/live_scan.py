from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.db.models import (
    AlertRow,
    BookmakerRow,
    CompetitionRow,
    ConnectorHealthRow,
    EventRow,
    MarketCandidateRow,
    MarketRow,
    OpportunityRow,
    OrderBookLevelRow,
    OrderBookSnapshotRow,
    PredictionMarketQuoteRow,
    ProviderEventRow,
    ProviderMarketRow,
    SportsbookQuoteRow,
    TeamRow,
)
from app.domain.models import Opportunity, PredictionMarketQuote, SportsbookQuote
from app.providers.records import ProviderHealthRecord
from app.services.alert_deduplication import deduplication_key
from app.services.live_pipeline import LiveScanSnapshot


class LiveScanRepository:
    """Persists one normalized scan atomically using the existing relational schema."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def approved_event_mappings(self) -> dict[tuple[str, str], str]:
        """Return previously persisted prediction-event to OddsPapi event mappings."""

        prediction = aliased(ProviderEventRow)
        sportsbook = aliased(ProviderEventRow)
        async with self._sessions() as session:
            rows = await session.execute(
                select(
                    prediction.provider,
                    prediction.provider_event_id,
                    sportsbook.provider_event_id,
                )
                .join(sportsbook, sportsbook.event_id == prediction.event_id)
                .where(
                    prediction.provider.in_(("kalshi", "polymarket")),
                    sportsbook.provider == "oddspapi",
                )
            )
            return {
                (provider, prediction_event_id): sportsbook_event_id
                for provider, prediction_event_id, sportsbook_event_id in rows
            }

    async def persist(self, snapshot: LiveScanSnapshot) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(OpportunityRow).where(OpportunityRow.active.is_(True)).values(active=False)
            )
            for bookmaker in snapshot.bookmakers:
                bookmaker_row = await session.get(BookmakerRow, bookmaker.canonical_id)
                values = {
                    "display_name": bookmaker.display_name,
                    "provider": bookmaker.provider.value,
                    "provider_bookmaker_id": bookmaker.provider_bookmaker_id,
                    "enabled": bookmaker.enabled,
                    "availability_status": bookmaker.availability_status.value,
                    "last_seen_at": bookmaker.last_seen_at,
                    "last_verified_at": bookmaker.last_verified_at,
                }
                if bookmaker_row is None:
                    session.add(BookmakerRow(canonical_id=bookmaker.canonical_id, **values))
                else:
                    for key, value in values.items():
                        setattr(bookmaker_row, key, value)

            for event in snapshot.events:
                competition = await self._competition(session, event.competition, event.sport)
                home = await self._team(session, event.home_team, event.sport)
                away = await self._team(session, event.away_team, event.sport)
                event_row = await session.get(EventRow, event.id)
                if event_row is None:
                    event_row = EventRow(
                        id=event.id,
                        competition_id=competition.id,
                        home_team_id=home.id,
                        away_team_id=away.id,
                        kickoff_time_utc=event.kickoff_time_utc,
                        status=event.status,
                    )
                    session.add(event_row)
                else:
                    event_row.kickoff_time_utc = event.kickoff_time_utc
                    event_row.status = event.status
            await session.flush()

            provider_events: dict[tuple[str, str], ProviderEventRow] = {}
            all_quotes: list[PredictionMarketQuote | SportsbookQuote] = [
                *snapshot.predictions,
                *snapshot.sportsbooks,
            ]
            for quote in all_quotes:
                provider_key = (quote.provider.value, quote.provider_event_id)
                if provider_key in provider_events:
                    continue
                provider_event_row = await session.scalar(
                    select(ProviderEventRow).where(
                        ProviderEventRow.provider == provider_key[0],
                        ProviderEventRow.provider_event_id == provider_key[1],
                    )
                )
                if provider_event_row is None:
                    provider_event_row = ProviderEventRow(
                        provider=provider_key[0],
                        provider_event_id=provider_key[1],
                        event_id=quote.canonical_event_id,
                        raw_status=quote.market_status.value,
                    )
                    session.add(provider_event_row)
                    await session.flush()
                provider_events[provider_key] = provider_event_row

            market_rows: dict[tuple[UUID, str, str, str | None, str | None], MarketRow] = {}
            prediction_quote_rows: dict[tuple[str, str], PredictionMarketQuoteRow] = {}
            snapshot_rows: dict[str, OrderBookSnapshotRow] = {}
            for quote in snapshot.predictions:
                market = await self._market(session, quote)
                market_rows[self._market_key(quote)] = market
                provider_market = await session.scalar(
                    select(ProviderMarketRow).where(
                        ProviderMarketRow.provider == quote.provider.value,
                        ProviderMarketRow.provider_market_id == quote.provider_market_id,
                    )
                )
                if provider_market is None:
                    provider_market = ProviderMarketRow(
                        provider=quote.provider.value,
                        provider_market_id=quote.provider_market_id,
                        market_id=market.id,
                        status=quote.market_status.value,
                        direct_url=quote.direct_url,
                    )
                    session.add(provider_market)
                    await session.flush()
                quote_row = PredictionMarketQuoteRow(
                    provider_market_id=provider_market.id,
                    best_bid_probability=quote.best_bid_probability,
                    best_ask_probability=quote.best_ask_probability,
                    best_bid_size=quote.best_bid_size,
                    best_ask_size=quote.best_ask_size,
                    source_timestamp=quote.source_timestamp,
                    received_timestamp=quote.received_timestamp,
                )
                session.add(quote_row)
                await session.flush()
                prediction_quote_rows[(quote.provider.value, quote.provider_market_id)] = quote_row
                book = snapshot.order_books[quote.provider_market_id]
                book_row = OrderBookSnapshotRow(
                    provider_market_id=provider_market.id,
                    outcome=book.outcome.value,
                    best_bid=book.best_bid,
                    best_ask=book.best_ask,
                    midpoint=book.midpoint,
                    spread_cents=book.spread_cents,
                    trailing_24h_volume_usd=book.trailing_24h_volume_usd,
                    volume_source=book.volume_source.value if book.volume_source else None,
                    source_timestamp=book.source_timestamp,
                    received_timestamp=book.received_timestamp,
                )
                session.add(book_row)
                await session.flush()
                snapshot_rows[quote.provider_market_id] = book_row
                session.add_all(
                    [
                        OrderBookLevelRow(
                            snapshot_id=book_row.id,
                            side=side,
                            price=level.price,
                            quantity=level.quantity,
                            notional_usd=level.notional_usd,
                        )
                        for side, levels in (("bid", book.bids), ("ask", book.asks))
                        for level in levels
                    ]
                )

            sportsbook_quote_rows: dict[tuple[str, str, str, str], SportsbookQuoteRow] = {}
            for quote in snapshot.sportsbooks:
                market = await self._market(session, quote)
                market_rows[self._market_key(quote)] = market
                provider_event = provider_events[(quote.provider.value, quote.provider_event_id)]
                sportsbook_quote_row = SportsbookQuoteRow(
                    provider_event_id=provider_event.id,
                    market_id=market.id,
                    bookmaker_id=quote.bookmaker_id,
                    decimal_odds=quote.decimal_odds,
                    implied_probability=quote.implied_probability,
                    source_timestamp=quote.source_timestamp,
                    received_timestamp=quote.received_timestamp,
                )
                session.add(sportsbook_quote_row)
                await session.flush()
                sportsbook_quote_rows[
                    (
                        str(quote.canonical_event_id),
                        quote.bookmaker_id,
                        quote.market_type.value,
                        quote.selection.value,
                    )
                ] = sportsbook_quote_row

            for candidate in snapshot.candidates:
                prediction = candidate.prediction_quote
                sportsbook = candidate.sportsbook_quote
                prediction_row = prediction_quote_rows[
                    (prediction.provider.value, prediction.provider_market_id)
                ]
                sportsbook_row = sportsbook_quote_rows[
                    (
                        str(sportsbook.canonical_event_id),
                        sportsbook.bookmaker_id,
                        sportsbook.market_type.value,
                        sportsbook.selection.value,
                    )
                ]
                candidate_row = MarketCandidateRow(
                    id=candidate.id,
                    prediction_quote_id=prediction_row.id,
                    sportsbook_quote_id=sportsbook_row.id,
                    snapshot_id=snapshot_rows[prediction.provider_market_id].id,
                    accepted=candidate.accepted,
                    edge_percentage_points=candidate.edge_percentage_points,
                    rejection_reasons=candidate.rejection_reasons,
                    liquidity_qualification=candidate.liquidity.model_dump(mode="json"),
                    evaluated_at=candidate.evaluated_at,
                )
                session.add(candidate_row)

            for opportunity in snapshot.opportunities:
                prediction_row = prediction_quote_rows[
                    (
                        opportunity.prediction_market_provider.value,
                        opportunity.prediction_market_id,
                    )
                ]
                sportsbook_row = sportsbook_quote_rows[
                    (
                        str(opportunity.canonical_event_id),
                        opportunity.bookmaker_id,
                        opportunity.market_type.value,
                        opportunity.selection.value,
                    )
                ]
                market = market_rows[self._market_key(opportunity)]
                session.add(
                    OpportunityRow(
                        id=opportunity.id,
                        market_id=market.id,
                        prediction_quote_id=prediction_row.id,
                        sportsbook_quote_id=sportsbook_row.id,
                        edge_percentage_points=opportunity.edge_percentage_points,
                        configured_threshold=opportunity.configured_threshold,
                        detected_at=opportunity.detected_at,
                        active=opportunity.active,
                        qualification_status=opportunity.qualification_status,
                        liquidity_qualification={
                            "midpoint": str(opportunity.midpoint),
                            "spread_cents": str(opportunity.spread_cents),
                            "total_depth_within_window_usd": str(
                                opportunity.total_depth_within_window_usd
                            ),
                            "trailing_24h_volume_usd": str(opportunity.trailing_24h_volume_usd),
                        },
                    )
                )

    async def record_alert(
        self, opportunity: Opportunity, sent_at: datetime, delivery_status: str
    ) -> None:
        async with self._sessions.begin() as session:
            session.add(
                AlertRow(
                    id=uuid4(),
                    opportunity_id=opportunity.id,
                    deduplication_key=deduplication_key(opportunity),
                    sent_at=sent_at,
                    edge_percentage_points=opportunity.edge_percentage_points,
                    delivery_status=delivery_status,
                )
            )

    async def persist_health(self, records: Sequence[ProviderHealthRecord]) -> None:
        async with self._sessions.begin() as session:
            for record in records:
                row = await session.get(ConnectorHealthRow, record.provider.value)
                values = {
                    "mode": record.mode,
                    "enabled": record.enabled,
                    "connected": record.connected,
                    "connection_status": (
                        "connected"
                        if record.connected
                        else ("disabled" if not record.enabled else "error")
                    ),
                    "last_successful_request": record.last_success_at,
                    "last_error": record.sanitized_latest_error,
                    "last_error_time": record.last_failure_at,
                    "last_data_timestamp": record.last_payload_timestamp,
                    "missing_required_bookmakers": [],
                    "response_latency_ms": record.latency_ms,
                    "consecutive_failures": record.consecutive_failures,
                    "last_payload_timestamp": record.last_payload_timestamp,
                    "last_order_book_timestamp": record.last_order_book_timestamp,
                    "stale": record.stale,
                    "events_discovered": record.events_discovered,
                    "markets_discovered": record.markets_discovered,
                    "books_updated": record.books_updated,
                    "trades_processed": record.trades_processed,
                    "latest_error_code": record.latest_error_code,
                }
                if row is None:
                    session.add(ConnectorHealthRow(provider=record.provider.value, **values))
                else:
                    for key, value in values.items():
                        setattr(row, key, value)

    @staticmethod
    def _market_key(
        quote: PredictionMarketQuote | SportsbookQuote | Opportunity,
    ) -> tuple[UUID, str, str, str | None, str | None]:
        canonical_event_id = quote.canonical_event_id
        market_type = quote.market_type.value
        selection = quote.selection.value
        participant = quote.participant
        line = quote.line
        return (
            canonical_event_id,
            market_type,
            selection,
            participant,
            str(line) if line is not None else None,
        )

    async def _market(
        self, session: AsyncSession, quote: PredictionMarketQuote | SportsbookQuote
    ) -> MarketRow:
        key = self._market_key(quote)
        canonical_event_id, market_type, selection, participant, _line_text = key
        line = getattr(quote, "line", None)
        row = await session.scalar(
            select(MarketRow).where(
                MarketRow.event_id == canonical_event_id,
                MarketRow.market_type == market_type,
                MarketRow.selection == selection,
                MarketRow.period == quote.period.value,
                MarketRow.participant.is_(None)
                if participant is None
                else MarketRow.participant == participant,
                MarketRow.line.is_(None) if line is None else MarketRow.line == line,
            )
        )
        if row is None:
            row = MarketRow(
                event_id=canonical_event_id,
                market_type=market_type,
                selection=selection,
                period=quote.period.value,
                includes_extra_time=(
                    quote.includes_extra_time if isinstance(quote, PredictionMarketQuote) else False
                ),
                includes_penalties=(
                    quote.includes_penalties if isinstance(quote, PredictionMarketQuote) else False
                ),
                participant=participant,
                line=line,
                settlement_rule=quote.settlement_rule,
            )
            session.add(row)
            await session.flush()
        return row

    async def _competition(self, session: AsyncSession, name: str, sport: str) -> CompetitionRow:
        row = await session.scalar(
            select(CompetitionRow).where(CompetitionRow.name == name, CompetitionRow.sport == sport)
        )
        if row is None:
            row = CompetitionRow(id=uuid4(), name=name, sport=sport)
            session.add(row)
            await session.flush()
        return row

    async def _team(self, session: AsyncSession, name: str, sport: str) -> TeamRow:
        row = await session.scalar(
            select(TeamRow).where(TeamRow.name == name, TeamRow.sport == sport)
        )
        if row is None:
            row = TeamRow(id=uuid4(), name=name, sport=sport)
            session.add(row)
            await session.flush()
        return row

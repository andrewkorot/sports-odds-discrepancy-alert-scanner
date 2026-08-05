from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select, tuple_, update
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
    MissingSportsbookOutcomeRow,
    OpportunityRow,
    OrderBookLevelRow,
    OrderBookSnapshotRow,
    PredictionMarketQuoteRow,
    ProviderEventRow,
    ProviderMarketRow,
    SportsbookQuoteRow,
    SystemSettingRow,
    TeamRow,
)
from app.domain.models import CanonicalEvent, Opportunity, PredictionMarketQuote, SportsbookQuote
from app.providers.records import ProviderHealthRecord
from app.services.alert_deduplication import deduplication_key
from app.services.live_pipeline import LiveScanSnapshot


class LiveScanRepository:
    """Persists one normalized scan atomically using the existing relational schema."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load_system_setting(self, key: str) -> dict[str, object] | None:
        async with self._sessions() as session:
            row = await session.get(SystemSettingRow, key)
            return dict(row.value) if row is not None else None

    async def save_system_setting(
        self,
        key: str,
        value: dict[str, object],
        updated_at: datetime,
    ) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(SystemSettingRow, key)
            if row is None:
                session.add(SystemSettingRow(key=key, value=value, updated_at=updated_at))
            else:
                row.value = value
                row.updated_at = updated_at

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

            event_ids: dict[UUID, UUID] = {}
            for event in snapshot.events:
                competition = await self._competition(session, event.competition, event.sport)
                home = await self._team(session, event.home_team, event.sport)
                away = await self._team(session, event.away_team, event.sport)
                event_row = await self._event(
                    session,
                    event,
                    competition,
                    home,
                    away,
                )
                event_ids[event.id] = event_row.id
            await session.flush()

            provider_events: dict[tuple[str, str], ProviderEventRow] = {}
            all_quotes: list[PredictionMarketQuote | SportsbookQuote] = [
                *snapshot.predictions,
                *snapshot.sportsbooks,
            ]
            provider_keys = {
                (quote.provider.value, quote.provider_event_id) for quote in all_quotes
            }
            if provider_keys:
                existing_provider_events = await session.scalars(
                    select(ProviderEventRow).where(
                        tuple_(
                            ProviderEventRow.provider,
                            ProviderEventRow.provider_event_id,
                        ).in_(provider_keys)
                    )
                )
                provider_events = {
                    (row.provider, row.provider_event_id): row for row in existing_provider_events
                }
            quote_by_provider_key = {
                (quote.provider.value, quote.provider_event_id): quote for quote in all_quotes
            }
            for provider_key in provider_keys:
                quote = quote_by_provider_key[provider_key]
                provider_event_row = provider_events.get(provider_key)
                if provider_event_row is None:
                    provider_event_row = ProviderEventRow(
                        id=uuid4(),
                        provider=provider_key[0],
                        provider_event_id=provider_key[1],
                        event_id=event_ids[quote.canonical_event_id],
                        raw_status=quote.market_status.value,
                    )
                    session.add(provider_event_row)
                elif provider_event_row.event_id != event_ids[quote.canonical_event_id]:
                    provider_event_row.event_id = event_ids[quote.canonical_event_id]
                provider_events[provider_key] = provider_event_row

            persisted_market_rows = await session.scalars(
                select(MarketRow).where(MarketRow.event_id.in_(set(event_ids.values())))
            )
            resolved_markets = {
                self._resolved_market_key(
                    row.event_id,
                    row.market_type,
                    row.selection,
                    row.period,
                    row.participant,
                    row.line,
                ): row
                for row in persisted_market_rows
            }
            market_rows: dict[tuple[UUID, str, str, str | None, str | None], MarketRow] = {}
            prediction_quote_rows: dict[tuple[str, str], PredictionMarketQuoteRow] = {}
            snapshot_rows: dict[str, OrderBookSnapshotRow] = {}
            pending_prediction_parents: list[PredictionMarketQuoteRow | OrderBookSnapshotRow] = []
            pending_order_book_levels: list[OrderBookLevelRow] = []
            provider_market_keys = {
                (quote.provider.value, quote.provider_market_id) for quote in snapshot.predictions
            }
            provider_markets: dict[tuple[str, str], ProviderMarketRow] = {}
            if provider_market_keys:
                existing_provider_markets = await session.scalars(
                    select(ProviderMarketRow).where(
                        tuple_(
                            ProviderMarketRow.provider,
                            ProviderMarketRow.provider_market_id,
                        ).in_(provider_market_keys)
                    )
                )
                provider_markets = {
                    (row.provider, row.provider_market_id): row for row in existing_provider_markets
                }
            for quote in snapshot.predictions:
                market = self._cached_market(
                    session,
                    quote,
                    event_ids[quote.canonical_event_id],
                    resolved_markets,
                )
                market_rows[self._market_key(quote)] = market
                provider_market_key = (
                    quote.provider.value,
                    quote.provider_market_id,
                )
                provider_market = provider_markets.get(provider_market_key)
                if provider_market is None:
                    provider_market = ProviderMarketRow(
                        id=uuid4(),
                        provider=quote.provider.value,
                        provider_market_id=quote.provider_market_id,
                        market_id=market.id,
                        status=quote.market_status.value,
                        direct_url=quote.direct_url,
                    )
                    session.add(provider_market)
                    provider_markets[provider_market_key] = provider_market
                else:
                    provider_market.market_id = market.id
                    provider_market.status = quote.market_status.value
                    provider_market.direct_url = quote.direct_url
                quote_row = PredictionMarketQuoteRow(
                    id=uuid4(),
                    provider_market_id=provider_market.id,
                    provider_source_market_id=quote.provider_source_market_id,
                    provider_market_name=quote.provider_market_name,
                    provider_market_type=quote.provider_market_type,
                    provider_outcome_id=quote.provider_outcome_id,
                    provider_outcome_name=quote.provider_outcome_name,
                    best_bid_probability=quote.best_bid_probability,
                    best_ask_probability=quote.best_ask_probability,
                    best_bid_size=quote.best_bid_size,
                    best_ask_size=quote.best_ask_size,
                    source_timestamp=quote.source_timestamp,
                    received_timestamp=quote.received_timestamp,
                )
                pending_prediction_parents.append(quote_row)
                prediction_quote_rows[(quote.provider.value, quote.provider_market_id)] = quote_row
                book = snapshot.order_books[quote.provider_market_id]
                book_row = OrderBookSnapshotRow(
                    id=uuid4(),
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
                pending_prediction_parents.append(book_row)
                snapshot_rows[quote.provider_market_id] = book_row
                pending_order_book_levels.extend(
                    OrderBookLevelRow(
                        snapshot_id=book_row.id,
                        side=side,
                        price=level.price,
                        quantity=level.quantity,
                        notional_usd=level.notional_usd,
                    )
                    for side, levels in (("bid", book.bids), ("ask", book.asks))
                    for level in levels
                )

            # Phase 1: canonical and provider markets must exist before prediction
            # quotes and order-book snapshots that reference them by UUID.
            await session.flush()
            session.add_all(pending_prediction_parents)

            # Phase 2: prediction quotes and order-book snapshot parents must exist
            # before direct-UUID level rows.
            await session.flush()
            session.add_all(pending_order_book_levels)

            sportsbook_quote_rows: dict[
                tuple[str, str, str, str, str, str], SportsbookQuoteRow
            ] = {}
            pending_sportsbook_quotes: list[
                tuple[SportsbookQuote, MarketRow, ProviderEventRow]
            ] = []
            for quote in snapshot.sportsbooks:
                market = self._cached_market(
                    session,
                    quote,
                    event_ids[quote.canonical_event_id],
                    resolved_markets,
                )
                market_rows[self._market_key(quote)] = market
                provider_event = provider_events[(quote.provider.value, quote.provider_event_id)]
                pending_sportsbook_quotes.append((quote, market, provider_event))

            # Phase 3: any sportsbook-only canonical markets must exist before their
            # direct-UUID sportsbook quote rows.
            await session.flush()
            for quote, market, provider_event in pending_sportsbook_quotes:
                sportsbook_quote_row = SportsbookQuoteRow(
                    id=uuid4(),
                    provider_event_id=provider_event.id,
                    market_id=market.id,
                    bookmaker_id=quote.bookmaker_id,
                    provider_market_id=quote.provider_market_id,
                    provider_market_name=quote.provider_market_name,
                    provider_market_type=quote.provider_market_type,
                    provider_outcome_id=quote.provider_outcome_id,
                    provider_outcome_name=quote.provider_outcome_name,
                    bookmaker_outcome_id=quote.bookmaker_outcome_id,
                    decimal_odds=quote.decimal_odds,
                    implied_probability=quote.implied_probability,
                    source_timestamp=quote.source_timestamp,
                    received_timestamp=quote.received_timestamp,
                )
                session.add(sportsbook_quote_row)
                sportsbook_quote_rows[
                    (
                        str(quote.canonical_event_id),
                        quote.bookmaker_id,
                        quote.market_type.value,
                        quote.selection.value,
                        str(quote.line) if quote.line is not None else "",
                        quote.participant or "",
                    )
                ] = sportsbook_quote_row

            # Phase 4: levels and sportsbook quotes must exist before candidate and
            # opportunity rows reference their UUIDs.
            await session.flush()

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
                        str(sportsbook.line) if sportsbook.line is not None else "",
                        sportsbook.participant or "",
                    )
                ]
                candidate_row = MarketCandidateRow(
                    id=candidate.id,
                    prediction_quote_id=prediction_row.id,
                    sportsbook_quote_id=sportsbook_row.id,
                    snapshot_id=snapshot_rows[prediction.provider_market_id].id,
                    accepted=candidate.accepted,
                    edge_percentage_points=candidate.edge_percentage_points,
                    configured_threshold=candidate.configured_threshold,
                    rejection_reasons=candidate.rejection_reasons,
                    liquidity_qualification=candidate.liquidity.model_dump(mode="json"),
                    evaluated_at=candidate.evaluated_at,
                )
                session.add(candidate_row)

            for audit in snapshot.missing_outcomes:
                prediction = audit.prediction_quote
                prediction_row = prediction_quote_rows[
                    (prediction.provider.value, prediction.provider_market_id)
                ]
                session.add(
                    MissingSportsbookOutcomeRow(
                        id=audit.id,
                        prediction_quote_id=prediction_row.id,
                        bookmaker_id=audit.bookmaker_id,
                        rejection_reason=audit.rejection_reason,
                        evaluated_at=audit.evaluated_at,
                    )
                )

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
                        str(opportunity.line) if opportunity.line is not None else "",
                        opportunity.participant or "",
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

    def _cached_market(
        self,
        session: AsyncSession,
        quote: PredictionMarketQuote | SportsbookQuote,
        event_id: UUID,
        cache: dict[
            tuple[UUID, str, str, str, str | None, Decimal | None],
            MarketRow,
        ],
    ) -> MarketRow:
        market_type = quote.market_type.value
        selection = quote.selection.value
        participant = quote.participant
        line = getattr(quote, "line", None)
        cache_key = self._resolved_market_key(
            event_id,
            market_type,
            selection,
            quote.period.value,
            participant,
            line,
        )
        row = cache.get(cache_key)
        if row is None:
            row = MarketRow(
                id=uuid4(),
                event_id=event_id,
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
            cache[cache_key] = row
        return row

    @staticmethod
    def _resolved_market_key(
        event_id: UUID,
        market_type: str,
        selection: str,
        period: str,
        participant: str | None,
        line: Decimal | None,
    ) -> tuple[UUID, str, str, str, str | None, Decimal | None]:
        return (
            event_id,
            market_type,
            selection,
            period,
            participant,
            line,
        )

    async def _competition(self, session: AsyncSession, name: str, sport: str) -> CompetitionRow:
        row = await session.scalar(
            select(CompetitionRow).where(CompetitionRow.name == name, CompetitionRow.sport == sport)
        )
        if row is None:
            row = CompetitionRow(id=uuid4(), name=name, sport=sport)
            session.add(row)
            await session.flush()
        return row

    async def _event(
        self,
        session: AsyncSession,
        event: CanonicalEvent,
        competition: CompetitionRow,
        home: TeamRow,
        away: TeamRow,
    ) -> EventRow:
        row = await session.get(EventRow, event.id)
        if row is None:
            row = await session.scalar(
                select(EventRow).where(
                    EventRow.competition_id == competition.id,
                    EventRow.home_team_id == home.id,
                    EventRow.away_team_id == away.id,
                    EventRow.kickoff_time_utc == event.kickoff_time_utc,
                )
            )
        if row is None:
            row = EventRow(
                id=event.id,
                competition_id=competition.id,
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_time_utc=event.kickoff_time_utc,
                status=event.status,
            )
            session.add(row)
            await session.flush()
        else:
            row.status = event.status
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

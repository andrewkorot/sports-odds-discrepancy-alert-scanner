from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from app.domain.enums import AvailabilityStatus, MarketType, Provider, Selection, VolumeSource
from app.domain.models import (
    Bookmaker,
    CanonicalEvent,
    OrderBookSnapshot,
    PredictionMarketQuote,
    SportsbookQuote,
)
from app.services.edge_calculator import decimal_odds_to_implied_probability
from app.services.liquidity import make_level

EVENT_ID = UUID("12345678-1234-5678-1234-567812345678")
BOOKMAKERS = {
    "bookmaker_eu": ("BookMaker.eu", Decimal("2.14")),
    "stake": ("Stake", Decimal("2.18")),
    "cloudbet": ("Cloudbet", Decimal("2.16")),
    "betus": ("BetUS", Decimal("2.10")),
    "pinnacle": ("Pinnacle", Decimal("2.15")),
    "coolbet": ("Coolbet", Decimal("2.12")),
}
SELECTIONS: list[tuple[MarketType, Selection, str | None, Decimal | None, Decimal]] = [
    (MarketType.MONEYLINE, Selection.HOME, "Inter Miami", None, Decimal("0.520")),
    (MarketType.MONEYLINE, Selection.DRAW, None, None, Decimal("0.330")),
    (MarketType.MONEYLINE, Selection.AWAY, "Atlanta United", None, Decimal("0.280")),
    (MarketType.TOTAL, Selection.OVER, None, Decimal("2.5"), Decimal("0.540")),
    (MarketType.TOTAL, Selection.UNDER, None, Decimal("2.5"), Decimal("0.510")),
    (MarketType.SPREAD, Selection.HOME, "Inter Miami", Decimal("-0.5"), Decimal("0.550")),
    (
        MarketType.SPREAD,
        Selection.AWAY,
        "Atlanta United",
        Decimal("0.5"),
        Decimal("0.500"),
    ),
    (MarketType.BTTS, Selection.YES, None, None, Decimal("0.560")),
    (MarketType.BTTS, Selection.NO, None, None, Decimal("0.470")),
]


def mock_snapshot(
    now: datetime | None = None,
) -> tuple[
    CanonicalEvent,
    list[PredictionMarketQuote],
    list[SportsbookQuote],
    list[Bookmaker],
]:
    now = now or datetime.now(UTC)
    kickoff = now + timedelta(hours=4)
    event = CanonicalEvent(
        id=EVENT_ID,
        competition="MLS",
        home_team="Inter Miami",
        away_team="Atlanta United",
        kickoff_time_utc=kickoff,
    )
    common = dict(
        canonical_event_id=EVENT_ID,
        competition="MLS",
        home_team="Inter Miami",
        away_team="Atlanta United",
        kickoff_time_utc=kickoff,
        source_timestamp=now,
        received_timestamp=now,
    )
    predictions: list[PredictionMarketQuote] = []
    for provider in (Provider.KALSHI, Provider.POLYMARKET):
        for market_type, selection, participant, line, ask in SELECTIONS:
            slug = f"{market_type}-{selection}-{line or 'na'}"
            predictions.append(
                PredictionMarketQuote(
                    provider=provider,
                    provider_event_id=f"{provider}-inter-miami-atlanta",
                    provider_market_id=f"{provider}-{slug}",
                    market_type=market_type,
                    selection=selection,
                    participant=participant,
                    line=line,
                    best_bid_probability=ask - Decimal("0.03"),
                    best_ask_probability=ask,
                    best_bid_size=Decimal("3000"),
                    best_ask_size=Decimal("3000"),
                    direct_url=f"https://example.test/{provider}/{slug}",
                    **common,
                )
            )
    books = [
        Bookmaker(
            canonical_id=canonical_id,
            display_name=display_name,
            provider_bookmaker_id=f"mock-{canonical_id}",
            availability_status=AvailabilityStatus.AVAILABLE,
            last_seen_at=now,
        )
        for canonical_id, (display_name, _) in BOOKMAKERS.items()
    ]
    sportsbook_quotes: list[SportsbookQuote] = []
    for market_type, selection, participant, line, ask in SELECTIONS:
        # A 4pp lower implied probability guarantees a qualifying ask-based edge.
        target_probability = ask - Decimal("0.04")
        odds = Decimal("1") / target_probability
        for canonical_id, (display_name, moneyline_home_odds) in BOOKMAKERS.items():
            selected_odds = (
                moneyline_home_odds
                if market_type == MarketType.MONEYLINE and selection == Selection.HOME
                else odds
            )
            sportsbook_quotes.append(
                SportsbookQuote(
                    provider_event_id="oddspapi-mock-mls-1",
                    bookmaker_id=canonical_id,
                    bookmaker_display_name=display_name,
                    market_type=market_type,
                    selection=selection,
                    participant=participant,
                    line=line,
                    decimal_odds=selected_odds,
                    implied_probability=decimal_odds_to_implied_probability(selected_odds),
                    **common,
                )
            )
    return event, predictions, sportsbook_quotes, books


def mock_order_books(
    predictions: list[PredictionMarketQuote],
) -> dict[str, OrderBookSnapshot]:
    snapshots: dict[str, OrderBookSnapshot] = {}
    for quote in predictions:
        bid = quote.best_bid_probability
        ask = quote.best_ask_probability
        midpoint = (bid + ask) / Decimal("2")
        snapshots[quote.provider_market_id] = OrderBookSnapshot(
            provider=quote.provider,
            provider_market_id=quote.provider_market_id,
            outcome=quote.selection,
            bids=[
                make_level(bid, Decimal("3000")),
                make_level(midpoint - Decimal("0.01"), Decimal("1500")),
            ],
            asks=[
                make_level(ask, Decimal("3000")),
                make_level(midpoint + Decimal("0.02"), Decimal("1500")),
            ],
            best_bid=bid,
            best_ask=ask,
            midpoint=midpoint,
            spread=ask - bid,
            spread_cents=(ask - bid) * Decimal("100"),
            source_timestamp=quote.source_timestamp,
            received_timestamp=quote.received_timestamp,
            trailing_24h_volume_usd=Decimal("8200"),
            volume_source=VolumeSource.PROVIDER_REPORTED,
        )
    return snapshots

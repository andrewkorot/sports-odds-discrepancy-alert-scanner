from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from app.domain.enums import AvailabilityStatus, Provider, Selection
from app.domain.models import (
    Bookmaker,
    CanonicalEvent,
    PredictionMarketQuote,
    SportsbookQuote,
)
from app.services.edge_calculator import decimal_odds_to_implied_probability

EVENT_ID = UUID("12345678-1234-5678-1234-567812345678")
BOOKMAKERS = {
    "bookmaker_eu": ("BookMaker.eu", Decimal("2.14")),
    "stake": ("Stake", Decimal("2.18")),
    "cloudbet": ("Cloudbet", Decimal("2.16")),
    "betus": ("BetUS", Decimal("2.10")),
    "pinnacle": ("Pinnacle", Decimal("2.15")),
    "coolbet": ("Coolbet", Decimal("2.12")),
}


def mock_snapshot(
    now: datetime | None = None,
) -> tuple[
    CanonicalEvent,
    list[PredictionMarketQuote],
    list[SportsbookQuote],
    list[Bookmaker],
]:
    now = now or datetime.now(UTC)
    kickoff = now + timedelta(hours=24)
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
        selection=Selection.HOME,
        source_timestamp=now,
        received_timestamp=now,
    )
    predictions = [
        PredictionMarketQuote(
            provider=Provider.KALSHI,
            provider_event_id="kalshi-inter-miami-atlanta",
            provider_market_id="KXMLS-MIA-ATL-MIA",
            best_bid_probability=Decimal("0.510"),
            best_ask_probability=Decimal("0.520"),
            best_bid_size=Decimal("920"),
            best_ask_size=Decimal("850"),
            direct_url="https://kalshi.com/",
            **common,
        ),
        PredictionMarketQuote(
            provider=Provider.POLYMARKET,
            provider_event_id="poly-inter-miami-atlanta",
            provider_market_id="poly-mls-mia-atl-home",
            best_bid_probability=Decimal("0.507"),
            best_ask_probability=Decimal("0.508"),
            best_bid_size=Decimal("4100"),
            best_ask_size=Decimal("3400"),
            direct_url="https://polymarket.com/",
            **common,
        ),
    ]
    books = [
        Bookmaker(
            canonical_id=canonical_id,
            display_name=display_name,
            provider_bookmaker_id=f"mock-{canonical_id}",
            availability_status=AvailabilityStatus.AVAILABLE,
            last_seen_at=now,
            last_verified_at=None,
        )
        for canonical_id, (display_name, _) in BOOKMAKERS.items()
    ]
    sportsbook_quotes = [
        SportsbookQuote(
            provider_event_id="oddspapi-mock-mls-1",
            bookmaker_id=canonical_id,
            bookmaker_display_name=display_name,
            decimal_odds=odds,
            implied_probability=decimal_odds_to_implied_probability(odds),
            **common,
        )
        for canonical_id, (display_name, odds) in BOOKMAKERS.items()
    ]
    return event, predictions, sportsbook_quotes, books

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import Provider


class ProviderRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def normalize_aware_datetimes_to_utc(cls, value: object) -> object:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("provider timestamps must be timezone-aware")
            return value.astimezone(UTC)
        return value


class ProviderEvent(ProviderRecord):
    provider: Provider
    provider_event_id: str
    title: str
    category: str | None = None
    scheduled_start: datetime | None = None
    status: str
    sport: str | None = None
    competition: str | None = None
    provider_competition_id: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    participant_one: str | None = None
    participant_two: str | None = None
    orientation_known: bool = True
    extraction_source: str | None = None
    competition_country: str | None = None
    competition_league_level: int | None = None
    competition_gender: str | None = None
    competition_age_group: str | None = None
    competition_season: str | None = None
    competition_type: str | None = None
    settlement_scope: str = "soccer_regulation"
    raw_market_ids: list[str] = Field(default_factory=list)


class ProviderOutcome(ProviderRecord):
    name: str
    selection_id: str
    token_id: str | None = None


class ProviderMarket(ProviderRecord):
    provider: Provider
    provider_event_id: str
    provider_market_id: str
    condition_id: str | None = None
    title: str
    status: str
    order_book_enabled: bool
    outcomes: list[ProviderOutcome]
    close_time: datetime | None = None
    trailing_24h_volume_usd: Decimal | None = None


class ProviderBookLevel(ProviderRecord):
    price: Decimal
    quantity: Decimal


class ProviderOrderBook(ProviderRecord):
    provider: Provider
    provider_market_id: str
    selection_id: str
    bids: list[ProviderBookLevel]
    asks: list[ProviderBookLevel]
    source_timestamp: datetime
    ask_derived: bool = False
    source_no_price: Decimal | None = None


class ProviderTrade(ProviderRecord):
    provider: Provider
    provider_market_id: str
    trade_id: str
    price: Decimal
    quantity: Decimal
    executed_at: datetime


class ProviderHealthRecord(ProviderRecord):
    provider: Provider
    mode: str
    enabled: bool
    connected: bool
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    consecutive_failures: int = 0
    latency_ms: Decimal | None = None
    last_payload_timestamp: datetime | None = None
    last_order_book_timestamp: datetime | None = None
    stale: bool = False
    events_discovered: int = 0
    markets_discovered: int = 0
    books_updated: int = 0
    trades_processed: int = 0
    latest_error_code: str | None = None
    sanitized_latest_error: str | None = None


class ProviderSportsbookQuote(ProviderRecord):
    provider_event_id: str
    bookmaker_id: str
    provider_outcome_id: int
    bookmaker_outcome_id: str | None = None
    market_id: int
    decimal_odds: Decimal
    active: bool
    market_active: bool
    main_line: bool
    changed_at: datetime
    market_type: str
    selection: str
    period: str
    direct_url: str | None = None

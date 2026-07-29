from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import (
    AvailabilityStatus,
    MarketStatus,
    MarketType,
    MatchConfidence,
    Period,
    Provider,
    Selection,
    VolumeSource,
)


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def require_aware_datetimes(cls, value: object) -> object:
        if isinstance(value, datetime) and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware UTC values")
        return value


class CanonicalEvent(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    sport: str = "soccer"
    competition: str
    home_team: str
    away_team: str
    kickoff_time_utc: datetime
    status: str = "pregame"

    @field_validator("sport")
    @classmethod
    def normalize_sport(cls, value: str) -> str:
        return value.strip().casefold()


class PredictionMarketQuote(DomainModel):
    provider: Provider
    provider_event_id: str
    provider_market_id: str
    canonical_event_id: UUID
    sport: str = "soccer"
    competition: str
    home_team: str
    away_team: str
    kickoff_time_utc: datetime
    market_type: MarketType = MarketType.MONEYLINE
    selection: Selection
    participant: str | None = None
    line: Decimal | None = None
    settlement_rule: str = "soccer_regulation"
    period: Period = Period.REGULATION
    includes_extra_time: bool = False
    includes_penalties: bool = False
    best_bid_probability: Decimal
    best_ask_probability: Decimal
    best_bid_size: Decimal
    best_ask_size: Decimal
    source_timestamp: datetime
    received_timestamp: datetime
    market_status: MarketStatus = MarketStatus.OPEN
    direct_url: str | None = None

    @field_validator("sport")
    @classmethod
    def normalize_sport(cls, value: str) -> str:
        return value.strip().casefold()


class SportsbookQuote(DomainModel):
    provider: Provider = Provider.ODDSPAPI
    provider_event_id: str
    canonical_event_id: UUID
    sport: str = "soccer"
    bookmaker_id: str
    bookmaker_display_name: str
    competition: str
    home_team: str
    away_team: str
    kickoff_time_utc: datetime
    market_type: MarketType = MarketType.MONEYLINE
    selection: Selection
    participant: str | None = None
    line: Decimal | None = None
    settlement_rule: str = "soccer_regulation"
    period: Period = Period.REGULATION
    decimal_odds: Decimal
    implied_probability: Decimal
    source_timestamp: datetime
    received_timestamp: datetime
    market_status: MarketStatus = MarketStatus.OPEN
    direct_url: str | None = None

    @field_validator("sport")
    @classmethod
    def normalize_sport(cls, value: str) -> str:
        return value.strip().casefold()


class MarketMapping(DomainModel):
    prediction_market_provider: Provider
    prediction_market_id: str
    sportsbook_provider: Provider = Provider.ODDSPAPI
    sportsbook_event_id: str
    canonical_event_id: UUID
    match_confidence: MatchConfidence
    settlement_compatible: bool
    approved: bool
    rejection_reason: str | None = None


class Opportunity(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    canonical_event_id: UUID
    sport: str = "soccer"
    competition: str
    home_team: str
    away_team: str
    kickoff_time_utc: datetime
    market_type: MarketType
    selection: Selection
    participant: str | None = None
    line: Decimal | None = None
    period: Period = Period.REGULATION
    settlement_rule: str = "soccer_regulation"
    prediction_market_provider: Provider
    prediction_market_id: str
    prediction_market_best_bid: Decimal
    prediction_market_best_ask: Decimal
    prediction_market_bid_size: Decimal
    prediction_market_ask_size: Decimal
    prediction_market_direct_url: str | None = None
    bookmaker_id: str
    bookmaker_display_name: str
    sportsbook_decimal_odds: Decimal
    sportsbook_implied_probability: Decimal
    sportsbook_direct_url: str | None = None
    edge_percentage_points: Decimal
    configured_threshold: Decimal
    prediction_quote_age_seconds: Decimal
    sportsbook_quote_age_seconds: Decimal
    liquidity_passed: bool
    freshness_passed: bool
    mapping_confidence: MatchConfidence
    detected_at: datetime
    active: bool = True
    midpoint: Decimal
    spread_cents: Decimal
    bid_depth_within_window_usd: Decimal
    ask_depth_within_window_usd: Decimal
    total_depth_within_window_usd: Decimal
    trailing_24h_volume_usd: Decimal
    volume_source: VolumeSource
    qualification_status: str = "accepted"

    @field_validator("sport")
    @classmethod
    def normalize_sport(cls, value: str) -> str:
        return value.strip().casefold()


class Bookmaker(DomainModel):
    canonical_id: str
    display_name: str
    provider: Provider = Provider.ODDSPAPI
    provider_bookmaker_id: str | None = None
    enabled: bool = True
    availability_status: AvailabilityStatus = AvailabilityStatus.UNVERIFIED
    last_seen_at: datetime | None = None
    last_verified_at: datetime | None = None


class ConnectorHealth(DomainModel):
    provider: Provider
    connection_status: str
    last_successful_request: datetime | None = None
    last_error: str | None = None
    last_error_time: datetime | None = None
    last_data_timestamp: datetime | None = None
    missing_required_bookmakers: list[str] = Field(default_factory=list)
    response_latency_ms: Decimal | None = None
    consecutive_failures: int = 0


class CanonicalSelection(DomainModel):
    market_type: MarketType
    participant: str | None = None
    outcome: Selection
    line: Decimal | None = None
    period: Period = Period.REGULATION
    regulation_only: bool = True
    includes_extra_time: bool = False
    includes_penalties: bool = False
    settlement_rule: str = "soccer_regulation"


class CanonicalMarket(DomainModel):
    market_type: MarketType
    period: Period
    line: Decimal | None = None
    settlement_scope: str = "soccer_regulation"
    provider: Provider
    provider_market_id: str
    provider_selection_id: str
    selection_type: Selection
    selection_team: str | None = None
    selection_side: str | None = None
    outcome_name: str


class NormalizedTrade(DomainModel):
    provider: Provider
    provider_market_id: str
    selection_id: str
    price: Decimal
    quantity: Decimal
    notional_usd: Decimal
    executed_at: datetime


class OrderBookLevel(DomainModel):
    price: Decimal
    quantity: Decimal
    notional_usd: Decimal


class OrderBookSnapshot(DomainModel):
    provider: Provider
    provider_market_id: str
    outcome: Selection
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    best_bid: Decimal | None
    best_ask: Decimal | None
    midpoint: Decimal | None
    spread: Decimal | None
    spread_cents: Decimal | None
    source_timestamp: datetime
    received_timestamp: datetime
    trailing_24h_volume_usd: Decimal | None
    volume_source: VolumeSource | None


class LiquidityQualification(DomainModel):
    best_bid: Decimal | None
    best_ask: Decimal | None
    midpoint: Decimal | None
    spread_cents: Decimal | None
    maximum_spread_cents: Decimal
    spread_passed: bool
    depth_window_cents: Decimal
    bid_depth_within_window_usd: Decimal
    ask_depth_within_window_usd: Decimal
    total_depth_within_window_usd: Decimal
    minimum_depth_usd: Decimal
    depth_passed: bool
    trailing_24h_volume_usd: Decimal | None
    minimum_trailing_24h_volume_usd: Decimal
    volume_source: VolumeSource | None
    volume_passed: bool
    overall_passed: bool
    rejection_reasons: list[str]


class MarketCandidate(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    prediction_quote: PredictionMarketQuote
    sportsbook_quote: SportsbookQuote
    order_book: OrderBookSnapshot
    liquidity: LiquidityQualification
    accepted: bool
    rejection_reasons: list[str]
    edge_percentage_points: Decimal
    evaluated_at: datetime

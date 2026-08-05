from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BookmakerRow(Base):
    __tablename__ = "bookmakers"
    canonical_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(32))
    provider_bookmaker_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    availability_status: Mapped[str] = mapped_column(String(32), index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BookmakerAliasRow(Base):
    __tablename__ = "bookmaker_aliases"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    bookmaker_id: Mapped[str] = mapped_column(ForeignKey("bookmakers.canonical_id"))
    alias: Mapped[str] = mapped_column(String(128), unique=True)


class CompetitionRow(Base):
    __tablename__ = "competitions"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    sport: Mapped[str] = mapped_column(String(32), index=True)


class CompetitionAliasRow(Base):
    __tablename__ = "competition_aliases"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    competition_id: Mapped[UUID] = mapped_column(ForeignKey("competitions.id"))
    alias: Mapped[str] = mapped_column(String(128), unique=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=True)


class TeamRow(Base):
    __tablename__ = "teams"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    sport: Mapped[str] = mapped_column(String(32), index=True)


class TeamAliasRow(Base):
    __tablename__ = "team_aliases"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(ForeignKey("teams.id"))
    alias: Mapped[str] = mapped_column(String(128), unique=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=True)


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    competition_id: Mapped[UUID] = mapped_column(ForeignKey("competitions.id"))
    home_team_id: Mapped[UUID] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[UUID] = mapped_column(ForeignKey("teams.id"))
    kickoff_time_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    __table_args__ = (
        UniqueConstraint("competition_id", "home_team_id", "away_team_id", "kickoff_time_utc"),
    )


class ProviderEventRow(Base):
    __tablename__ = "provider_events"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(32))
    provider_event_id: Mapped[str] = mapped_column(String(256))
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    raw_status: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("provider", "provider_event_id"),)


class MarketRow(Base):
    __tablename__ = "markets"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    market_type: Mapped[str] = mapped_column(String(64))
    selection: Mapped[str] = mapped_column(String(64))
    period: Mapped[str] = mapped_column(String(32))
    includes_extra_time: Mapped[bool] = mapped_column(Boolean, default=False)
    includes_penalties: Mapped[bool] = mapped_column(Boolean, default=False)
    participant: Mapped[str | None] = mapped_column(String(128))
    line: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    settlement_rule: Mapped[str] = mapped_column(String(128), default="soccer_regulation")
    __table_args__ = (
        UniqueConstraint("event_id", "market_type", "selection", "participant", "line", "period"),
    )


class ProviderMarketRow(Base):
    __tablename__ = "provider_markets"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(32))
    provider_market_id: Mapped[str] = mapped_column(String(256))
    market_id: Mapped[UUID] = mapped_column(ForeignKey("markets.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    direct_url: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("provider", "provider_market_id"),)


class MarketMappingRow(Base):
    __tablename__ = "market_mappings"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    prediction_provider_market_id: Mapped[UUID] = mapped_column(ForeignKey("provider_markets.id"))
    sportsbook_provider_event_id: Mapped[UUID] = mapped_column(ForeignKey("provider_events.id"))
    match_confidence: Mapped[str] = mapped_column(String(32))
    settlement_compatible: Mapped[bool] = mapped_column(Boolean)
    approved: Mapped[bool] = mapped_column(Boolean)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("prediction_provider_market_id", "sportsbook_provider_event_id"),
    )


class PredictionMarketQuoteRow(Base):
    __tablename__ = "prediction_market_quotes"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    provider_market_id: Mapped[UUID] = mapped_column(ForeignKey("provider_markets.id"))
    provider_source_market_id: Mapped[str | None] = mapped_column(String(256))
    provider_market_name: Mapped[str | None] = mapped_column(Text)
    provider_market_type: Mapped[str | None] = mapped_column(String(128))
    provider_outcome_id: Mapped[str | None] = mapped_column(String(256))
    provider_outcome_name: Mapped[str | None] = mapped_column(Text)
    best_bid_probability: Mapped[Decimal] = mapped_column(Numeric(12, 8))
    best_ask_probability: Mapped[Decimal] = mapped_column(Numeric(12, 8))
    best_bid_size: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    best_ask_size: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SportsbookQuoteRow(Base):
    __tablename__ = "sportsbook_quotes"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    provider_event_id: Mapped[UUID] = mapped_column(ForeignKey("provider_events.id"))
    market_id: Mapped[UUID] = mapped_column(ForeignKey("markets.id"))
    bookmaker_id: Mapped[str] = mapped_column(ForeignKey("bookmakers.canonical_id"), index=True)
    provider_market_id: Mapped[str | None] = mapped_column(String(128))
    provider_market_name: Mapped[str | None] = mapped_column(Text)
    provider_market_type: Mapped[str | None] = mapped_column(String(128))
    provider_outcome_id: Mapped[str | None] = mapped_column(String(256))
    provider_outcome_name: Mapped[str | None] = mapped_column(Text)
    bookmaker_outcome_id: Mapped[str | None] = mapped_column(String(256))
    decimal_odds: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    implied_probability: Mapped[Decimal] = mapped_column(Numeric(12, 8))
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_sportsbook_quote_lookup", "market_id", "bookmaker_id", "source_timestamp"),
    )


class OpportunityRow(Base):
    __tablename__ = "opportunities"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    market_id: Mapped[UUID] = mapped_column(ForeignKey("markets.id"))
    prediction_quote_id: Mapped[UUID] = mapped_column(ForeignKey("prediction_market_quotes.id"))
    sportsbook_quote_id: Mapped[UUID] = mapped_column(ForeignKey("sportsbook_quotes.id"))
    edge_percentage_points: Mapped[Decimal] = mapped_column(Numeric(12, 6), index=True)
    configured_threshold: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    qualification_status: Mapped[str] = mapped_column(String(32), default="accepted")
    liquidity_qualification: Mapped[dict[str, object] | None] = mapped_column(JSON)


class OrderBookSnapshotRow(Base):
    __tablename__ = "order_book_snapshots"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    provider_market_id: Mapped[UUID] = mapped_column(ForeignKey("provider_markets.id"))
    outcome: Mapped[str] = mapped_column(String(32))
    best_bid: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    best_ask: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    midpoint: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    spread_cents: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    trailing_24h_volume_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    volume_source: Mapped[str | None] = mapped_column(String(32))
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrderBookLevelRow(Base):
    __tablename__ = "order_book_levels"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("order_book_snapshots.id"))
    side: Mapped[str] = mapped_column(String(8))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 8))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    notional_usd: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    __table_args__ = (UniqueConstraint("snapshot_id", "side", "price", "quantity"),)


class MarketCandidateRow(Base):
    __tablename__ = "market_candidates"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    prediction_quote_id: Mapped[UUID] = mapped_column(ForeignKey("prediction_market_quotes.id"))
    sportsbook_quote_id: Mapped[UUID] = mapped_column(ForeignKey("sportsbook_quotes.id"))
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("order_book_snapshots.id"))
    accepted: Mapped[bool] = mapped_column(Boolean, index=True)
    edge_percentage_points: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    configured_threshold: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    rejection_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    liquidity_qualification: Mapped[dict[str, object]] = mapped_column(JSON)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class MissingSportsbookOutcomeRow(Base):
    __tablename__ = "missing_sportsbook_outcomes"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    prediction_quote_id: Mapped[UUID] = mapped_column(ForeignKey("prediction_market_quotes.id"))
    bookmaker_id: Mapped[str] = mapped_column(ForeignKey("bookmakers.canonical_id"))
    rejection_reason: Mapped[str] = mapped_column(String(64), index=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AlertRow(Base):
    __tablename__ = "alerts"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    opportunity_id: Mapped[UUID] = mapped_column(ForeignKey("opportunities.id"))
    deduplication_key: Mapped[str] = mapped_column(String(512), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    edge_percentage_points: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    delivery_status: Mapped[str] = mapped_column(String(32))


class SystemSettingRow(Base):
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, object]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConnectorHealthRow(Base):
    __tablename__ = "connector_health"
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), default="mock")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    connection_status: Mapped[str] = mapped_column(String(32), index=True)
    last_successful_request: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_data_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    missing_required_bookmakers: Mapped[list[str]] = mapped_column(JSON, default=list)
    response_latency_ms: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    last_payload_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_order_book_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    events_discovered: Mapped[int] = mapped_column(default=0)
    markets_discovered: Mapped[int] = mapped_column(default=0)
    books_updated: Mapped[int] = mapped_column(default=0)
    trades_processed: Mapped[int] = mapped_column(default=0)
    latest_error_code: Mapped[str | None] = mapped_column(String(64))

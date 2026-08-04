from __future__ import annotations

from datetime import time
from decimal import Decimal
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

RUNTIME_SETTING_KEYS = frozenset(
    {
        "min_kalshi_ask_size",
        "min_polymarket_ask_size",
        "discovery_calendar_days",
        "alert_cooldown_minutes",
        "realert_edge_increase_pp",
        "min_minutes_before_kickoff",
        "event_match_kickoff_tolerance_minutes",
        "max_prediction_price_age_seconds",
        "max_sportsbook_price_age_seconds",
        "max_bid_ask_spread_cents",
        "depth_window_from_midpoint_cents",
        "min_depth_within_window_usd",
        "min_trailing_24h_volume_usd",
        "edge_threshold_pp",
        "price_poll_interval_seconds",
        "provider_request_concurrency",
        "auto_start_stop_enabled",
        "scan_auto_start_time",
        "scan_auto_stop_time",
        "event_match_fuzzy_min_score",
        "event_match_ambiguity_margin",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "development"
    app_mode: str = "mock"
    mock_mode: bool = True
    database_url: str = "postgresql+asyncpg://scanner:scanner@postgres:5432/scanner"
    redis_url: str = "redis://redis:6379/0"
    sports_odds_api_key: str | None = None
    sports_odds_base_url: str = "https://api.oddspapi.io/v4"
    oddspapi_discovery_dump_path: str | None = "runtime/oddspapi_discovered_events.json"
    oddspapi_api_key: str | None = None  # Backward-compatible alias.
    oddspapi_base_url: str = "https://api.oddspapi.io/v4"
    kalshi_api_key_id: str | None = None
    kalshi_api_key: str | None = None  # Backward-compatible alias.
    kalshi_private_key_path: str | None = None
    kalshi_mode: str = "mock"
    kalshi_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    polymarket_mode: str = "mock"
    polymarket_gamma_base_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_base_url: str = "https://clob.polymarket.com"
    polymarket_data_base_url: str = "https://data-api.polymarket.com"
    sports_odds_mode: str = "mock"
    live_dry_run: bool = True
    alerts_enabled: bool = False
    price_poll_interval_seconds: int = Field(default=30, ge=5, le=86400)
    auto_start_stop_enabled: bool = False
    scan_auto_start_time: time = time(6, 0)
    scan_auto_stop_time: time = time(23, 0)
    provider_request_concurrency: int = Field(default=8, ge=1, le=32)
    event_match_kickoff_tolerance_minutes: int = Field(default=15, ge=0, le=180)
    event_match_fuzzy_min_score: Decimal = Field(default=Decimal("80"), ge=0, le=100)
    event_match_ambiguity_margin: Decimal = Field(default=Decimal("5"), ge=0, le=100)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_client_bot_token: str | None = None
    telegram_client_chat_id: str | None = None
    telegram_owner_bot_token: str | None = None
    telegram_owner_chat_id: str | None = None
    enabled_bookmakers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "bookmaker_eu",
            "stake",
            "cloudbet",
            "betus",
            "pinnacle",
            "coolbet",
        ]
    )
    client_timezone: str = "America/Los_Angeles"
    enabled_market_types: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["moneyline", "total", "spread", "btts"]
    )
    enabled_sports: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["soccer"])
    max_bid_ask_spread_cents: Decimal = Field(default=Decimal("5"), ge=0)
    depth_window_from_midpoint_cents: Decimal = Field(default=Decimal("3"), ge=0)
    min_depth_within_window_usd: Decimal = Field(default=Decimal("2000"), ge=0)
    min_trailing_24h_volume_usd: Decimal = Field(default=Decimal("5000"), ge=0)
    edge_threshold_pp: Decimal = Field(default=Decimal("3.0"), ge=0)
    max_prediction_price_age_seconds: int = Field(default=20, ge=1)
    max_sportsbook_price_age_seconds: int = Field(default=60, ge=1)
    min_kalshi_ask_size: Decimal = Field(default=Decimal("100"), ge=0)
    min_polymarket_ask_size: Decimal = Field(default=Decimal("100"), ge=0)
    min_minutes_before_kickoff: int = Field(default=10, ge=0)
    discovery_calendar_days: int = Field(default=3, ge=1)
    alert_cooldown_minutes: int = Field(default=10, ge=0)
    realert_edge_increase_pp: Decimal = Field(default=Decimal("1.0"), ge=0)

    @field_validator("enabled_bookmakers", "enabled_market_types", "enabled_sports", mode="before")
    @classmethod
    def parse_csv_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip().casefold() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item).strip().casefold() for item in value if str(item).strip()]
        return value

    @field_validator("client_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def validate_scan_schedule(self) -> Settings:
        if self.scan_auto_start_time == self.scan_auto_stop_time:
            raise ValueError("SCAN_AUTO_START_TIME and SCAN_AUTO_STOP_TIME must differ")
        return self

    @field_validator("app_mode")
    @classmethod
    def validate_app_mode(cls, value: str) -> str:
        if value not in {"mock", "live"}:
            raise ValueError("APP_MODE must be mock or live")
        return value

    @field_validator("kalshi_mode", "polymarket_mode", "sports_odds_mode")
    @classmethod
    def validate_provider_mode(cls, value: str) -> str:
        if value not in {"mock", "live", "disabled"}:
            raise ValueError("provider mode must be mock, live, or disabled")
        return value

    @model_validator(mode="after")
    def validate_live_credentials(self) -> Settings:
        if self.app_mode == "live" or not self.mock_mode:
            missing: list[str] = []
            if self.kalshi_mode == "live":
                if not (self.kalshi_api_key_id or self.kalshi_api_key):
                    missing.append("KALSHI_API_KEY_ID")
                if not self.kalshi_private_key_path:
                    missing.append("KALSHI_PRIVATE_KEY_PATH")
            if self.sports_odds_mode == "live" and not (
                self.sports_odds_api_key or self.oddspapi_api_key
            ):
                missing.append("SPORTS_ODDS_API_KEY")
            if missing:
                raise ValueError(f"Live mode requires: {', '.join(missing)}")
        telegram_pairs = {
            "legacy": (self.telegram_bot_token, self.telegram_chat_id),
            "client": (self.telegram_client_bot_token, self.telegram_client_chat_id),
            "owner": (self.telegram_owner_bot_token, self.telegram_owner_chat_id),
        }
        incomplete = [
            name
            for name, (token, chat_id) in telegram_pairs.items()
            if bool(token) != bool(chat_id)
        ]
        if incomplete:
            raise ValueError(
                "Telegram token/chat ID must both be configured for: " + ", ".join(incomplete)
            )
        if not self.live_dry_run and self.alerts_enabled:
            if not any(token and chat_id for token, chat_id in telegram_pairs.values()):
                raise ValueError("Telegram delivery requires at least one complete destination")
        return self

    def telegram_destinations(self) -> list[tuple[str, str]]:
        """Return configured bot/chat pairs without exposing them through the API."""
        pairs = [
            (self.telegram_client_bot_token, self.telegram_client_chat_id),
            (self.telegram_owner_bot_token, self.telegram_owner_chat_id),
        ]
        if not any(token and chat_id for token, chat_id in pairs):
            pairs.append((self.telegram_bot_token, self.telegram_chat_id))
        return [(token, chat_id) for token, chat_id in pairs if token and chat_id]


@lru_cache
def get_settings() -> Settings:
    return Settings()

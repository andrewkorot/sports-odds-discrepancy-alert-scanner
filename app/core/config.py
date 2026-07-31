from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "development"
    dashboard_ui: str = "simple"
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
    kalshi_rest_enabled: bool = True
    kalshi_ws_enabled: bool = False
    kalshi_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    kalshi_ws_url: str = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    polymarket_mode: str = "mock"
    polymarket_gamma_base_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_base_url: str = "https://clob.polymarket.com"
    polymarket_data_base_url: str = "https://data-api.polymarket.com"
    polymarket_ws_enabled: bool = False
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    sports_odds_mode: str = "mock"
    live_dry_run: bool = True
    alerts_enabled: bool = False
    telegram_enabled: bool = False
    price_poll_interval_seconds: int = 30
    provider_request_concurrency: int = Field(default=8, ge=1, le=32)
    event_match_kickoff_tolerance_minutes: int = 15
    event_match_fuzzy_min_score: Decimal = Decimal("80")
    event_match_ambiguity_margin: Decimal = Decimal("5")
    alert_dedupe_ttl_seconds: int = 900
    alert_edge_change_threshold: Decimal = Decimal("0.01")
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
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
    max_bid_ask_spread_cents: Decimal = Decimal("5")
    depth_window_from_midpoint_cents: Decimal = Decimal("3")
    min_depth_within_window_usd: Decimal = Decimal("2000")
    min_trailing_24h_volume_usd: Decimal = Decimal("5000")
    edge_threshold_pp: Decimal = Decimal("3.0")
    max_prediction_price_age_seconds: int = 20
    max_sportsbook_price_age_seconds: int = 60
    min_kalshi_ask_size: Decimal = Decimal("100")
    min_polymarket_ask_size: Decimal = Decimal("100")
    min_minutes_before_kickoff: int = 10
    max_hours_before_kickoff: int = 72
    alert_cooldown_minutes: int = 10
    realert_edge_increase_pp: Decimal = Decimal("1.0")

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

    @field_validator("app_mode")
    @classmethod
    def validate_app_mode(cls, value: str) -> str:
        if value not in {"mock", "live"}:
            raise ValueError("APP_MODE must be mock or live")
        return value

    @field_validator("dashboard_ui")
    @classmethod
    def validate_dashboard_ui(cls, value: str) -> str:
        value = value.casefold()
        if value not in {"simple", "full"}:
            raise ValueError("DASHBOARD_UI must be simple or full")
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
        if (
            not self.live_dry_run
            and self.alerts_enabled
            and self.telegram_enabled
            and (not self.telegram_bot_token or not self.telegram_chat_id)
        ):
            raise ValueError("Telegram delivery requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

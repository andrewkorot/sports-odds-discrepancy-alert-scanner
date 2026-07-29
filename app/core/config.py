from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "development"
    mock_mode: bool = True
    database_url: str = "postgresql+asyncpg://scanner:scanner@postgres:5432/scanner"
    redis_url: str = "redis://redis:6379/0"
    oddspapi_api_key: str | None = None
    oddspapi_base_url: str = "https://v5.oddspapi.io/en"
    kalshi_api_key: str | None = None
    kalshi_private_key_path: str | None = None
    polymarket_api_key: str | None = None
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
    edge_threshold_pp: Decimal = Decimal("3.0")
    max_prediction_price_age_seconds: int = 20
    max_sportsbook_price_age_seconds: int = 60
    min_kalshi_ask_size: Decimal = Decimal("100")
    min_polymarket_ask_size: Decimal = Decimal("100")
    min_minutes_before_kickoff: int = 10
    max_hours_before_kickoff: int = 72
    alert_cooldown_minutes: int = 10
    realert_edge_increase_pp: Decimal = Decimal("1.0")
    oddspapi_poll_interval_seconds: int = 30

    @field_validator("enabled_bookmakers", mode="before")
    @classmethod
    def parse_enabled_bookmakers(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def validate_live_credentials(self) -> Settings:
        if not self.mock_mode:
            missing = [
                name
                for name, value in {
                    "ODDSPAPI_API_KEY": self.oddspapi_api_key,
                    "KALSHI_API_KEY": self.kalshi_api_key,
                    "KALSHI_PRIVATE_KEY_PATH": self.kalshi_private_key_path,
                    "POLYMARKET_API_KEY": self.polymarket_api_key,
                }.items()
                if not value
            ]
            if missing:
                raise ValueError(f"Live mode requires: {', '.join(missing)}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

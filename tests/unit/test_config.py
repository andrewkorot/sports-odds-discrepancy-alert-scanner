import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_enabled_sports_are_configuration_driven() -> None:
    settings = Settings(enabled_sports="soccer,Baseball,BASKETBALL")
    assert settings.enabled_sports == ["soccer", "baseball", "basketball"]


def test_live_mode_does_not_require_polymarket_credentials() -> None:
    settings = Settings(
        app_mode="live",
        mock_mode=False,
        kalshi_mode="disabled",
        polymarket_mode="live",
        sports_odds_mode="disabled",
    )
    assert settings.mock_mode is False
    assert not hasattr(settings, "polymarket_api_key")


def test_live_mode_still_requires_kalshi_credentials() -> None:
    with pytest.raises(ValidationError, match="KALSHI_API_KEY"):
        Settings(
            mock_mode=False,
            kalshi_mode="live",
            oddspapi_api_key="odds-key",
            kalshi_api_key=None,
            kalshi_private_key_path=None,
        )

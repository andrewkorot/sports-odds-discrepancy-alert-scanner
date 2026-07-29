import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_live_mode_does_not_require_polymarket_credentials() -> None:
    settings = Settings(
        mock_mode=False,
        oddspapi_api_key="odds-key",
        kalshi_api_key="kalshi-key",
        kalshi_private_key_path="/run/secrets/kalshi.pem",
    )
    assert settings.mock_mode is False
    assert not hasattr(settings, "polymarket_api_key")


def test_live_mode_still_requires_kalshi_credentials() -> None:
    with pytest.raises(ValidationError, match="KALSHI_API_KEY"):
        Settings(
            mock_mode=False,
            oddspapi_api_key="odds-key",
            kalshi_api_key=None,
            kalshi_private_key_path=None,
        )

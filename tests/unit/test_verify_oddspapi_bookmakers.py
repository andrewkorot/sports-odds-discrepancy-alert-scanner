from app.core.config import Settings
from app.scripts.verify_oddspapi_bookmakers import oddspapi_connection


def test_verifier_prefers_current_sports_odds_settings() -> None:
    settings = Settings(
        sports_odds_api_key="current-key",
        sports_odds_base_url="https://current.example",
        oddspapi_api_key="legacy-key",
        oddspapi_base_url="https://legacy.example",
    )
    assert oddspapi_connection(settings) == ("current-key", "https://current.example")


def test_verifier_accepts_legacy_key_alias() -> None:
    settings = Settings(
        sports_odds_api_key=None,
        oddspapi_api_key="legacy-key",
        oddspapi_base_url="https://legacy.example",
    )
    assert oddspapi_connection(settings) == ("legacy-key", "https://legacy.example")


def test_verifier_reports_missing_credentials_without_assertion() -> None:
    settings = Settings(sports_odds_api_key=None, oddspapi_api_key=None)
    assert oddspapi_connection(settings) is None

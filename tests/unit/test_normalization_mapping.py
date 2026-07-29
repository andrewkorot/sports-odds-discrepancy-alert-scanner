from app.providers.oddspapi.mapping import (
    CANONICAL_BOOKMAKERS,
    ProviderBookmaker,
    map_provider_bookmakers,
    normalize_bookmaker_alias,
)
from app.services.normalization import normalize_team


def test_all_six_canonical_bookmakers() -> None:
    assert set(CANONICAL_BOOKMAKERS) == {
        "bookmaker_eu",
        "stake",
        "cloudbet",
        "betus",
        "pinnacle",
        "coolbet",
    }


def test_bookmaker_aliases() -> None:
    for alias in ("BookMaker", "Bookmaker", "BookMaker.eu", "Bookmaker.eu"):
        assert normalize_bookmaker_alias(alias) == "bookmaker_eu"
    for alias in ("Bet US", "BetUS", "BetUS Sportsbook"):
        assert normalize_bookmaker_alias(alias) == "betus"


def test_provider_id_mapping_and_unknown_recording() -> None:
    mapped, unknown = map_provider_bookmakers(
        [
            ProviderBookmaker("pinny", "Pinnacle", True),
            ProviderBookmaker("mystery-id", "Unknown Book", True),
        ]
    )
    assert mapped[0].provider_bookmaker_id == "pinny"
    assert mapped[0].canonical_id == "pinnacle"
    assert unknown == ["Unknown Book (mystery-id)"]


def test_team_aliases_are_careful() -> None:
    assert normalize_team("Inter Miami CF") == "inter miami"
    assert normalize_team("Atlanta Utd") == "atlanta united"
    assert normalize_team("Random AFC") == "random afc"

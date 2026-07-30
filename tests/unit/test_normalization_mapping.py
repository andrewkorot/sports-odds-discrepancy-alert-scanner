from app.providers.oddspapi.mapping import (
    CANONICAL_BOOKMAKERS,
    ProviderBookmaker,
    map_provider_bookmakers,
    normalize_bookmaker_alias,
)
from app.services.normalization import (
    competition_identity,
    normalize_competition,
    normalize_team,
    qualifiers_compatible,
    team_identity,
)


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
    assert normalize_team("Random AFC") == "random"
    assert normalize_team("AFC Bournemouth") == "afc bournemouth"
    assert normalize_team("Manchester United") != normalize_team("Manchester City")


def test_team_qualifiers_are_preserved_and_compared() -> None:
    assert normalize_team("FC Barcelona Women") == "barcelona women"
    assert normalize_team("Barcelona B") == "barcelona b"
    assert normalize_team("Barcelona U19") == "barcelona u19"
    assert not qualifiers_compatible(
        team_identity("Barcelona"),
        team_identity("Barcelona Women"),
    )


def test_accents_punctuation_and_spacing_are_normalized() -> None:
    assert normalize_team("  Atlético—Madrid FC ") == "atletico madrid"


def test_competition_aliases_are_explicit() -> None:
    assert normalize_competition("English Premier League") == "premier league"
    assert normalize_competition("EPL") == "premier league"
    assert normalize_competition("Unrelated League") == "unrelated league"


def test_competition_metadata_distinguishes_ambiguous_names() -> None:
    england = competition_identity("Premier League", country="England")
    egypt = competition_identity("Premier League", country="Egypt")
    assert england.canonical_name == egypt.canonical_name
    assert england.country != egypt.country

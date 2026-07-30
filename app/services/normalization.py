import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

TEAM_ALIASES = {
    "inter miami cf": "inter miami",
    "inter miami": "inter miami",
    "atlanta utd": "atlanta united",
    "atlanta united fc": "atlanta united",
    "atlanta united": "atlanta united",
    "manchester united fc": "manchester united",
    "manchester united": "manchester united",
    "man utd": "manchester united",
    "manchester utd": "manchester united",
    "paris saint germain fc": "psg",
    "paris saint germain": "psg",
    "psg": "psg",
}
COMPETITION_ALIASES = {
    "major league soccer": "mls",
    "usa major league soccer": "mls",
    "mls": "mls",
    "english premier league": "premier league",
    "england premier league": "premier league",
    "epl": "premier league",
    "premier league": "premier league",
    "uefa champions league": "champions league",
    "champions league": "champions league",
    "ucl": "champions league",
    "usa usl championship": "usl championship",
    "us usl championship": "usl championship",
    "usl championship": "usl championship",
    "national womens soccer league": "nwsl",
    "national women s soccer league": "nwsl",
    "nwsl": "nwsl",
}

_CLUB_SUFFIXES = {"fc", "cf", "sc", "afc"}
_GENERIC_TEAM_QUALIFIERS = {
    "women": "women",
    "womens": "women",
    "ladies": "women",
    "reserves": "reserves",
    "reserve": "reserves",
    "academy": "academy",
    "esports": "esports",
    "esport": "esports",
}
_COUNTRY_PREFIXES = {
    "england": "england",
    "english": "england",
    "egypt": "egypt",
    "egyptian": "egypt",
    "south africa": "south africa",
    "usa": "usa",
    "united states": "usa",
    "mexico": "mexico",
    "mexican": "mexico",
}
_COMPETITION_PHASE_SUFFIX = re.compile(r"\s+(?:apertura|clausura)(?:\s+20\d{2})?$")


@dataclass(frozen=True)
class TeamIdentity:
    raw_name: str
    base_name: str
    qualifiers: tuple[str, ...]

    @property
    def canonical_name(self) -> str:
        return " ".join((self.base_name, *self.qualifiers)).strip()

    @property
    def matching_key(self) -> str:
        return f"{self.base_name}|{'|'.join(self.qualifiers)}"


@dataclass(frozen=True)
class CompetitionIdentity:
    raw_name: str
    canonical_name: str
    country: str | None = None
    league_level: int | None = None
    gender: str | None = None
    age_group: str | None = None
    season: str | None = None
    competition_type: str | None = None


@lru_cache(maxsize=16384)
def normalize_text(value: str) -> str:
    value = "".join(
        " " if unicodedata.category(character).startswith("P") else character for character in value
    )
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value.lower())).strip()


def _normalized_aliases(defaults: dict[str, str], aliases: dict[str, str] | None) -> dict[str, str]:
    return {
        normalize_text(key): normalize_text(value)
        for key, value in (defaults | (aliases or {})).items()
    }


def team_identity(value: str, aliases: dict[str, str] | None = None) -> TeamIdentity:
    if aliases is None:
        return _cached_team_identity(value)
    return _team_identity(value, aliases)


@lru_cache(maxsize=8192)
def _cached_team_identity(value: str) -> TeamIdentity:
    return _team_identity(value, None)


def _team_identity(value: str, aliases: dict[str, str] | None) -> TeamIdentity:
    normalized = normalize_text(value)
    mapping = _normalized_aliases(TEAM_ALIASES, aliases)
    normalized = mapping.get(normalized, normalized)
    tokens = normalized.split()
    qualifiers: list[str] = []

    joined = " ".join(tokens)
    age_matches = re.findall(r"\b(?:u|under)\s*(\d{2})\b", joined)
    for age in age_matches:
        qualifiers.append(f"u{age}")
    joined = re.sub(r"\b(?:u|under)\s*\d{2}\b", " ", joined)
    tokens = joined.split()

    remaining: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        two_tokens = " ".join(tokens[index : index + 2])
        if two_tokens in {"b team", "team b"}:
            qualifiers.append("b")
            index += 2
            continue
        qualifier = _GENERIC_TEAM_QUALIFIERS.get(token)
        if qualifier is not None:
            qualifiers.append(qualifier)
        else:
            remaining.append(token)
        index += 1

    if remaining and remaining[-1] == "ii":
        qualifiers.append("ii")
        remaining.pop()
    elif remaining and remaining[-1] == "b":
        qualifiers.append("b")
        remaining.pop()

    while len(remaining) > 1 and remaining[-1] in _CLUB_SUFFIXES:
        remaining.pop()
    if len(remaining) > 1 and remaining[0] in {"fc", "cf", "sc"}:
        remaining.pop(0)

    base = " ".join(remaining)
    base = mapping.get(base, base)
    return TeamIdentity(
        raw_name=value,
        base_name=base,
        qualifiers=tuple(sorted(set(qualifiers))),
    )


def normalize_team(value: str, aliases: dict[str, str] | None = None) -> str:
    return team_identity(value, aliases).canonical_name


def qualifiers_compatible(
    left: TeamIdentity,
    right: TeamIdentity,
) -> bool:
    return left.qualifiers == right.qualifiers


def competition_identity(
    value: str,
    aliases: dict[str, str] | None = None,
    *,
    country: str | None = None,
    league_level: int | None = None,
    gender: str | None = None,
    age_group: str | None = None,
    season: str | None = None,
    competition_type: str | None = None,
) -> CompetitionIdentity:
    if (
        aliases is None
        and country is None
        and league_level is None
        and gender is None
        and age_group is None
        and season is None
        and competition_type is None
    ):
        return _cached_competition_identity(value)
    return _competition_identity(
        value,
        aliases,
        country=country,
        league_level=league_level,
        gender=gender,
        age_group=age_group,
        season=season,
        competition_type=competition_type,
    )


@lru_cache(maxsize=4096)
def _cached_competition_identity(value: str) -> CompetitionIdentity:
    return _competition_identity(value, None)


def _competition_identity(
    value: str,
    aliases: dict[str, str] | None = None,
    *,
    country: str | None = None,
    league_level: int | None = None,
    gender: str | None = None,
    age_group: str | None = None,
    season: str | None = None,
    competition_type: str | None = None,
) -> CompetitionIdentity:
    normalized = normalize_text(value)
    mapping = _normalized_aliases(COMPETITION_ALIASES, aliases)
    canonical = mapping.get(normalized, normalized)
    # Some feeds append a Mexican season phase to the league display name.
    # Preserve it in raw metadata, but compare the underlying competition.
    canonical = _COMPETITION_PHASE_SUFFIX.sub("", canonical)
    canonical = mapping.get(canonical, canonical)
    detected_country = normalize_text(country) if country else None
    if detected_country is None:
        for prefix, canonical_country in _COUNTRY_PREFIXES.items():
            if canonical.startswith(f"{prefix} "):
                detected_country = canonical_country
                break
    detected_gender = gender
    if detected_gender is None and re.search(r"\b(women|womens|ladies)\b", canonical):
        detected_gender = "women"
    detected_age = age_group
    if detected_age is None:
        age_match = re.search(r"\b(?:u|under)\s*(\d{2})\b", canonical)
        detected_age = f"u{age_match.group(1)}" if age_match else None
    detected_season = season
    if detected_season is None:
        season_match = re.search(r"\b(20\d{2}(?:\s+\d{2,4})?)\b", canonical)
        detected_season = season_match.group(1) if season_match else None
    return CompetitionIdentity(
        raw_name=value,
        canonical_name=canonical,
        country=detected_country,
        league_level=league_level,
        gender=normalize_text(detected_gender) if detected_gender else None,
        age_group=normalize_text(detected_age) if detected_age else None,
        season=normalize_text(detected_season) if detected_season else None,
        competition_type=(normalize_text(competition_type) if competition_type else None),
    )


def normalize_competition(value: str, aliases: dict[str, str] | None = None) -> str:
    return competition_identity(value, aliases).canonical_name

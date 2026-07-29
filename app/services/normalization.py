import re
import unicodedata

TEAM_ALIASES = {
    "inter miami cf": "inter miami",
    "inter miami": "inter miami",
    "atlanta utd": "atlanta united",
    "atlanta united fc": "atlanta united",
    "atlanta united": "atlanta united",
    "manchester united fc": "manchester united",
    "manchester united": "manchester united",
    "paris saint germain": "psg",
    "psg": "psg",
}
COMPETITION_ALIASES = {"major league soccer": "mls", "mls": "mls"}


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value.lower())).strip()


def normalize_team(value: str, aliases: dict[str, str] | None = None) -> str:
    normalized = normalize_text(value)
    mapping = TEAM_ALIASES | (aliases or {})
    return mapping.get(normalized, normalized)


def normalize_competition(value: str) -> str:
    normalized = normalize_text(value)
    return COMPETITION_ALIASES.get(normalized, normalized)

from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.enums import AvailabilityStatus
from app.domain.models import Bookmaker

CANONICAL_BOOKMAKERS = {
    "bookmaker_eu": "BookMaker.eu",
    "stake": "Stake",
    "cloudbet": "Cloudbet",
    "betus": "BetUS",
    "pinnacle": "Pinnacle",
    "coolbet": "Coolbet",
}
ALIASES = {
    "bookmaker": "bookmaker_eu",
    "bookmakereu": "bookmaker_eu",
    "betus": "betus",
    "betussportsbook": "betus",
    "stake": "stake",
    "cloudbet": "cloudbet",
    "pinnacle": "pinnacle",
    "coolbet": "coolbet",
}


def normalize_bookmaker_alias(value: str) -> str | None:
    return ALIASES.get(re.sub(r"[^a-z0-9]", "", value.casefold()))


@dataclass(frozen=True)
class ProviderBookmaker:
    provider_id: str
    name: str
    active: bool


def map_provider_bookmakers(items: list[ProviderBookmaker]) -> tuple[list[Bookmaker], list[str]]:
    mapped: dict[str, Bookmaker] = {}
    unknown: list[str] = []
    for item in items:
        canonical_id = normalize_bookmaker_alias(item.name) or normalize_bookmaker_alias(
            item.provider_id
        )
        if canonical_id is None:
            unknown.append(f"{item.name} ({item.provider_id})")
            continue
        mapped[canonical_id] = Bookmaker(
            canonical_id=canonical_id,
            display_name=CANONICAL_BOOKMAKERS[canonical_id],
            provider_bookmaker_id=item.provider_id,
            availability_status=(
                AvailabilityStatus.AVAILABLE if item.active else AvailabilityStatus.UNAVAILABLE
            ),
        )
    return list(mapped.values()), unknown

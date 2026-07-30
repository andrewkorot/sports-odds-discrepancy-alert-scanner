from __future__ import annotations

import asyncio

from app.core.config import Settings, get_settings
from app.providers.oddspapi.client import OddsPapiClient
from app.providers.oddspapi.mapping import CANONICAL_BOOKMAKERS, map_provider_bookmakers


def oddspapi_connection(settings: Settings) -> tuple[str, str] | None:
    """Resolve current settings while retaining the legacy variable aliases."""
    if settings.sports_odds_api_key:
        return settings.sports_odds_api_key, settings.sports_odds_base_url
    if settings.oddspapi_api_key:
        return settings.oddspapi_api_key, settings.oddspapi_base_url
    return None


async def run() -> int:
    settings = get_settings()
    if settings.mock_mode:
        print("Bookmaker coverage remains UNVERIFIED: run with MOCK_MODE=false and credentials.")
        return 2
    connection = oddspapi_connection(settings)
    if connection is None:
        print("Configuration error: SPORTS_ODDS_API_KEY is required in live mode.")
        return 2
    api_key, base_url = connection
    client = OddsPapiClient(api_key, base_url)
    try:
        mapped, unknown = map_provider_bookmakers(await client.list_bookmakers())
    finally:
        await client.aclose()
    found = {item.canonical_id for item in mapped if item.availability_status == "available"}
    print("Required OddsPapi bookmaker coverage\n")
    for canonical_id, display_name in CANONICAL_BOOKMAKERS.items():
        print(f"{display_name:<16}{'FOUND' if canonical_id in found else 'MISSING'}")
    if unknown:
        print(f"\nUnmapped provider bookmakers recorded: {len(unknown)}")
    return 0 if set(CANONICAL_BOOKMAKERS) <= found else 1


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

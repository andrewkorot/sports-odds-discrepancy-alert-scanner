from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.services.scanner import ScannerState


async def run() -> None:
    settings = get_settings()
    if not settings.mock_mode:
        raise RuntimeError("seed_mock_data requires MOCK_MODE=true")
    scanner = ScannerState(settings)
    await scanner.refresh()
    print(
        f"Seeded 1 event, {len(scanner.bookmakers)} bookmakers, "
        f"{len(scanner.opportunities)} qualifying opportunities."
    )


if __name__ == "__main__":
    asyncio.run(run())

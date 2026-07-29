from datetime import UTC, datetime

from app.core.config import Settings
from app.domain.models import (
    Bookmaker,
    CanonicalEvent,
    Opportunity,
    PredictionMarketQuote,
    SportsbookQuote,
)
from app.providers.mock.data import mock_snapshot
from app.services.opportunity_detector import detect_opportunities


class ScannerState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.events: list[CanonicalEvent] = []
        self.predictions: list[PredictionMarketQuote] = []
        self.sportsbooks: list[SportsbookQuote] = []
        self.bookmakers: list[Bookmaker] = []
        self.opportunities: list[Opportunity] = []
        self.last_updated: datetime | None = None

    async def refresh(self, now: datetime | None = None) -> None:
        if not self.settings.mock_mode:
            raise NotImplementedError(
                "Live scan mapping is not yet validated; run bookmaker verification first"
            )
        current = now or datetime.now(UTC)
        event, predictions, sportsbooks, bookmakers = mock_snapshot(current)
        self.events = [event]
        self.predictions = predictions
        self.sportsbooks = sportsbooks
        self.bookmakers = bookmakers
        self.opportunities = detect_opportunities(
            predictions, sportsbooks, bookmakers, self.settings, current
        )
        self.last_updated = current

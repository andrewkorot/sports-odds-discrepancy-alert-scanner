from datetime import datetime, timedelta
from decimal import Decimal

from app.core.config import Settings
from app.domain.models import (
    Bookmaker,
    CanonicalEvent,
    EventMatchAudit,
    MarketCandidate,
    MissingSportsbookOutcomeAudit,
    Opportunity,
    PredictionMarketQuote,
    SportsbookQuote,
)
from app.providers.mock.data import mock_order_books, mock_snapshot
from app.services.clock import Clock, SystemClock
from app.services.live_pipeline import LiveScanSnapshot
from app.services.opportunity_detector import (
    evaluate_candidates,
    missing_sportsbook_outcomes,
    opportunities_from_candidates,
)


class ScannerState:
    def __init__(self, settings: Settings, clock: Clock | None = None) -> None:
        self.settings = settings
        self.clock = clock or SystemClock()
        self.events: list[CanonicalEvent] = []
        self.predictions: list[PredictionMarketQuote] = []
        self.sportsbooks: list[SportsbookQuote] = []
        self.bookmakers: list[Bookmaker] = []
        self.opportunities: list[Opportunity] = []
        self.candidates: list[MarketCandidate] = []
        self.missing_outcomes: list[MissingSportsbookOutcomeAudit] = []
        self.event_matches: list[EventMatchAudit] = []
        self.last_updated: datetime | None = None

    async def refresh(self, now: datetime | None = None) -> None:
        if not self.settings.mock_mode:
            raise NotImplementedError(
                "Live scan mapping is not yet validated; run bookmaker verification first"
            )
        current = now or self.clock.now()
        event, predictions, sportsbooks, bookmakers = mock_snapshot(current)
        self.events = [event]
        self.predictions = predictions
        self.sportsbooks = sportsbooks
        self.bookmakers = bookmakers
        self.event_matches = []
        order_books = mock_order_books(predictions)
        self.candidates = evaluate_candidates(
            predictions, sportsbooks, bookmakers, self.settings, current, order_books
        )
        self.missing_outcomes = missing_sportsbook_outcomes(
            predictions, sportsbooks, bookmakers, current
        )
        prediction = predictions[0]
        sportsbook = next(
            quote
            for quote in sportsbooks
            if quote.market_type == prediction.market_type
            and quote.selection == prediction.selection
            and quote.bookmaker_id == "pinnacle"
        )
        base_book = order_books[prediction.provider_market_id]
        rejected_books = [
            base_book.model_copy(
                update={"best_ask": prediction.best_bid_probability + Decimal("0.05")}
            ),
            base_book.model_copy(
                update={"best_ask": prediction.best_bid_probability + Decimal("0.06")}
            ),
            base_book.model_copy(update={"bids": [], "asks": []}),
            base_book.model_copy(update={"trailing_24h_volume_usd": Decimal("4999.99")}),
        ]
        for rejected_book in rejected_books:
            self.candidates.extend(
                evaluate_candidates(
                    [prediction],
                    [sportsbook],
                    bookmakers,
                    self.settings,
                    current,
                    {prediction.provider_market_id: rejected_book},
                )
            )
        for offset in (
            timedelta(days=1),
            -timedelta(days=1),
            -timedelta(minutes=1),
        ):
            changed_prediction = prediction.model_copy(
                update={"kickoff_time_utc": current + offset}
            )
            changed_sportsbook = sportsbook.model_copy(
                update={"kickoff_time_utc": current + offset}
            )
            self.candidates.extend(
                evaluate_candidates(
                    [changed_prediction],
                    [changed_sportsbook],
                    bookmakers,
                    self.settings,
                    current,
                    {prediction.provider_market_id: base_book},
                )
            )
        self.opportunities = opportunities_from_candidates(self.candidates, self.settings)
        self.last_updated = current

    def apply_live_snapshot(self, snapshot: LiveScanSnapshot, updated_at: datetime) -> None:
        self.events = snapshot.events
        self.predictions = snapshot.predictions
        self.sportsbooks = snapshot.sportsbooks
        self.bookmakers = snapshot.bookmakers
        self.candidates = snapshot.candidates
        self.missing_outcomes = snapshot.missing_outcomes
        self.opportunities = snapshot.opportunities
        self.event_matches = snapshot.event_matches
        self.last_updated = updated_at

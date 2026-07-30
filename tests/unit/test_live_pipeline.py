from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.config import Settings
from app.domain.enums import AvailabilityStatus, MatchConfidence, Provider
from app.domain.models import Bookmaker
from app.providers.records import (
    ProviderBookLevel,
    ProviderEvent,
    ProviderHealthRecord,
    ProviderMarket,
    ProviderOrderBook,
    ProviderOutcome,
    ProviderSportsbookQuote,
    ProviderTrade,
)
from app.services.live_pipeline import audit_prediction_event, collect_live_snapshot


class FakePredictionConnector:
    async def discover_events(
        self, start_time: datetime, end_time: datetime
    ) -> list[ProviderEvent]:
        return [
            ProviderEvent(
                provider=Provider.KALSHI,
                provider_event_id="kalshi-event",
                title="Inter Miami vs Atlanta United",
                category="MLS",
                scheduled_start=datetime(2026, 7, 30, 20, tzinfo=UTC),
                status="open",
            )
        ]

    async def discover_markets(self, event_id: str) -> list[ProviderMarket]:
        return [
            ProviderMarket(
                provider=Provider.KALSHI,
                provider_event_id=event_id,
                provider_market_id="kalshi-home",
                title="Will Inter Miami win?",
                status="open",
                order_book_enabled=True,
                outcomes=[
                    ProviderOutcome(name="Yes", selection_id="yes"),
                    ProviderOutcome(name="No", selection_id="no"),
                ],
            )
        ]

    async def get_order_book(self, market_or_token_id: str) -> ProviderOrderBook:
        return ProviderOrderBook(
            provider=Provider.KALSHI,
            provider_market_id=market_or_token_id,
            selection_id="yes",
            bids=[ProviderBookLevel(price=Decimal("0.50"), quantity=Decimal("5000"))],
            asks=[ProviderBookLevel(price=Decimal("0.52"), quantity=Decimal("5000"))],
            source_timestamp=datetime(2026, 7, 30, 16, tzinfo=UTC),
        )

    async def get_recent_trades(
        self, market_or_token_id: str, since: datetime
    ) -> list[ProviderTrade]:
        return [
            ProviderTrade(
                provider=Provider.KALSHI,
                provider_market_id=market_or_token_id,
                trade_id="trade-1",
                price=Decimal("0.50"),
                quantity=Decimal("20000"),
                executed_at=datetime(2026, 7, 30, 15, tzinfo=UTC),
            )
        ]

    async def health(self) -> ProviderHealthRecord:
        return ProviderHealthRecord(
            provider=Provider.KALSHI, mode="live", enabled=True, connected=True
        )

    async def aclose(self) -> None:
        return None


class FakeSportsConnector:
    def use_provider_bookmaker_ids(
        self, mapped: list[Bookmaker], enabled_canonical_ids: list[str]
    ) -> None:
        return None

    async def discover_events(
        self, start_time: datetime, end_time: datetime
    ) -> list[ProviderEvent]:
        return [
            ProviderEvent(
                provider=Provider.ODDSPAPI,
                provider_event_id="odds-event",
                title="Inter Miami vs Atlanta United",
                category="MLS",
                sport="soccer",
                competition="MLS",
                home_team="Inter Miami",
                away_team="Atlanta United",
                scheduled_start=datetime(2026, 7, 30, 20, tzinfo=UTC),
                status="Pre-Game",
            )
        ]

    async def list_bookmakers(self) -> tuple[list[Bookmaker], list[str]]:
        return [
            Bookmaker(
                canonical_id="pinnacle",
                display_name="Pinnacle",
                provider_bookmaker_id="pinnacle",
                availability_status=AvailabilityStatus.AVAILABLE,
            )
        ], []

    async def get_event_odds(self, event_id: str) -> list[ProviderSportsbookQuote]:
        return [
            ProviderSportsbookQuote(
                provider_event_id=event_id,
                bookmaker_id="pinnacle",
                provider_outcome_id=101,
                market_id=101,
                decimal_odds=Decimal("1.75"),
                active=True,
                market_active=True,
                main_line=True,
                changed_at=datetime(2026, 7, 30, 16, tzinfo=UTC),
                market_type="moneyline",
                selection="home",
                period="regulation",
            )
        ]


class FailingPredictionConnector(FakePredictionConnector):
    def __init__(self) -> None:
        self.called = False

    async def discover_events(
        self, start_time: datetime, end_time: datetime
    ) -> list[ProviderEvent]:
        self.called = True
        raise RuntimeError("provider unavailable")


class RenamedPredictionConnector(FakePredictionConnector):
    def __init__(self, title: str) -> None:
        self.title = title

    async def discover_events(
        self, start_time: datetime, end_time: datetime
    ) -> list[ProviderEvent]:
        events = await super().discover_events(start_time, end_time)
        return [events[0].model_copy(update={"title": self.title})]


class UnorderedPredictionConnector(FakePredictionConnector):
    async def discover_events(
        self, start_time: datetime, end_time: datetime
    ) -> list[ProviderEvent]:
        return [
            ProviderEvent(
                provider=Provider.KALSHI,
                provider_event_id="kalshi-event",
                title="Atlanta United vs Inter Miami",
                category="MLS",
                competition="MLS",
                participant_one="Atlanta United",
                participant_two="Inter Miami",
                orientation_known=False,
                extraction_source="event_title",
                scheduled_start=datetime(2026, 7, 30, 20, tzinfo=UTC),
                status="open",
            )
        ]


async def test_live_pipeline_retrieves_normalizes_matches_and_calculates() -> None:
    now = datetime(2026, 7, 30, 16, tzinfo=UTC)
    settings = Settings(
        enabled_bookmakers=["pinnacle"],
        client_timezone="UTC",
        edge_threshold_pp=Decimal("3"),
    )
    snapshot = await collect_live_snapshot(
        [FakePredictionConnector()],
        FakeSportsConnector(),  # type: ignore[arg-type]
        settings,
        now,
        now,
        now + timedelta(hours=8),
    )

    assert len(snapshot.events) == 1
    assert len(snapshot.predictions) == 1
    assert len(snapshot.sportsbooks) == 1
    assert len(snapshot.candidates) == 1
    assert len(snapshot.opportunities) == 1
    opportunity = snapshot.opportunities[0]
    assert opportunity.sport == "soccer"
    assert opportunity.bookmaker_id == "pinnacle"
    assert opportunity.prediction_market_best_ask == Decimal("0.52")
    assert opportunity.edge_percentage_points > Decimal("5")


async def test_prediction_provider_failure_does_not_block_other_discovery() -> None:
    now = datetime(2026, 7, 30, 16, tzinfo=UTC)
    failing = FailingPredictionConnector()
    snapshot = await collect_live_snapshot(
        [failing, FakePredictionConnector()],
        FakeSportsConnector(),  # type: ignore[arg-type]
        Settings(
            enabled_bookmakers=["pinnacle"],
            client_timezone="UTC",
            edge_threshold_pp=Decimal("3"),
        ),
        now,
        now,
        now + timedelta(hours=8),
    )
    assert failing.called
    assert len(snapshot.predictions) == 1
    assert len(snapshot.opportunities) == 1


async def test_approved_team_alias_can_flow_into_automatic_matching() -> None:
    now = datetime(2026, 7, 30, 16, tzinfo=UTC)
    snapshot = await collect_live_snapshot(
        [RenamedPredictionConnector("Inter Miami CF vs Atlanta Utd")],
        FakeSportsConnector(),  # type: ignore[arg-type]
        Settings(enabled_bookmakers=["pinnacle"], edge_threshold_pp=Decimal("3")),
        now,
        now,
        now + timedelta(hours=8),
    )

    prediction_audit = next(
        item for item in snapshot.event_matches if item.provider == Provider.KALSHI
    )
    assert prediction_audit.matched
    assert prediction_audit.match_confidence == MatchConfidence.APPROVED_ALIAS
    assert snapshot.opportunities


async def test_fuzzy_event_is_manual_review_and_never_prices_markets() -> None:
    now = datetime(2026, 7, 30, 16, tzinfo=UTC)
    snapshot = await collect_live_snapshot(
        [RenamedPredictionConnector("Inter Miamii vs Atlanta United")],
        FakeSportsConnector(),  # type: ignore[arg-type]
        Settings(enabled_bookmakers=["pinnacle"], edge_threshold_pp=Decimal("3")),
        now,
        now,
        now + timedelta(hours=8),
    )

    prediction_audit = next(
        item for item in snapshot.event_matches if item.provider == Provider.KALSHI
    )
    assert not prediction_audit.matched
    assert prediction_audit.match_confidence == MatchConfidence.MANUAL_REVIEW
    assert "fuzzy_match_requires_manual_review" in prediction_audit.rejection_reasons
    assert prediction_audit.normalized_participant_one == "inter miamii"
    assert prediction_audit.sportsbook_home_team == "Inter Miami"
    assert prediction_audit.sportsbook_away_team == "Atlanta United"
    assert prediction_audit.sportsbook_kickoff_time_utc == datetime(2026, 7, 30, 20, tzinfo=UTC)
    assert snapshot.predictions == []
    assert snapshot.opportunities == []


async def test_unordered_kalshi_pair_adopts_unique_sportsbook_orientation() -> None:
    now = datetime(2026, 7, 30, 16, tzinfo=UTC)
    snapshot = await collect_live_snapshot(
        [UnorderedPredictionConnector()],
        FakeSportsConnector(),  # type: ignore[arg-type]
        Settings(enabled_bookmakers=["pinnacle"], edge_threshold_pp=Decimal("3")),
        now,
        now,
        now + timedelta(hours=8),
    )

    audit = next(item for item in snapshot.event_matches if item.provider == Provider.KALSHI)
    assert audit.matched
    assert audit.match_confidence == MatchConfidence.APPROVED_ALIAS
    assert not audit.orientation_known
    assert audit.extraction_source == "event_title"
    assert audit.sportsbook_home_team == "Inter Miami"
    assert audit.sportsbook_away_team == "Atlanta United"
    assert snapshot.predictions[0].home_team == "Inter Miami"
    assert snapshot.predictions[0].away_team == "Atlanta United"
    assert snapshot.opportunities


def test_qualifier_conflict_is_rejected_even_when_club_name_matches() -> None:
    kickoff = datetime(2026, 7, 30, 20, tzinfo=UTC)
    prediction = ProviderEvent(
        provider=Provider.POLYMARKET,
        provider_event_id="prediction",
        title="Barcelona Women vs Real Madrid Women",
        sport="soccer",
        competition="Liga F",
        home_team="Barcelona Women",
        away_team="Real Madrid Women",
        scheduled_start=kickoff,
        status="open",
    )
    sportsbook = ProviderEvent(
        provider=Provider.ODDSPAPI,
        provider_event_id="sportsbook",
        title="Barcelona vs Real Madrid",
        sport="soccer",
        competition="Liga F",
        home_team="Barcelona",
        away_team="Real Madrid",
        scheduled_start=kickoff,
        status="open",
    )

    matched, audit = audit_prediction_event(prediction, [sportsbook], 15)

    assert matched is None
    assert audit.match_confidence == MatchConfidence.REJECTED
    assert "team_qualifier_mismatch" in audit.rejection_reasons


def test_close_fuzzy_candidates_require_manual_review_with_ambiguity_reason() -> None:
    kickoff = datetime(2026, 7, 30, 20, tzinfo=UTC)
    prediction = ProviderEvent(
        provider=Provider.POLYMARKET,
        provider_event_id="prediction",
        title="Inter Miamii vs Atlanta United",
        sport="soccer",
        competition="MLS",
        home_team="Inter Miamii",
        away_team="Atlanta United",
        scheduled_start=kickoff,
        status="open",
    )
    candidates = [
        ProviderEvent(
            provider=Provider.ODDSPAPI,
            provider_event_id=f"sportsbook-{index}",
            title="Inter Miami vs Atlanta United",
            sport="soccer",
            competition="MLS",
            home_team="Inter Miami",
            away_team="Atlanta United",
            scheduled_start=kickoff + timedelta(seconds=index),
            status="open",
        )
        for index in range(2)
    ]

    matched, audit = audit_prediction_event(prediction, candidates, 15)

    assert matched is None
    assert audit.match_confidence == MatchConfidence.MANUAL_REVIEW
    assert "ambiguous_candidate_margin" in audit.rejection_reasons
    assert audit.runner_up_score is not None

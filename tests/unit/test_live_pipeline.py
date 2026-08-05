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
from app.services.live_pipeline import (
    SportsbookEventIndex,
    audit_prediction_event,
    collect_live_snapshot,
)


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
    def __init__(self) -> None:
        self.odds_requests = 0
        self.bulk_odds_requests = 0

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
                provider_competition_id="mls",
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
        self.odds_requests += 1
        return [
            ProviderSportsbookQuote(
                provider_event_id=event_id,
                bookmaker_id="pinnacle",
                provider_outcome_id=101,
                market_id=101,
                decimal_odds=Decimal("2.15"),
                active=True,
                market_active=True,
                main_line=True,
                changed_at=datetime(2026, 7, 30, 16, tzinfo=UTC),
                market_type="moneyline",
                selection="home",
                period="regulation",
            )
        ]

    async def get_events_odds(
        self, events: list[ProviderEvent]
    ) -> dict[str, list[ProviderSportsbookQuote]]:
        self.bulk_odds_requests += 1
        return {
            event.provider_event_id: await self.get_event_odds(event.provider_event_id)
            for event in events
        }


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
        self.market_requests = 0

    async def discover_events(
        self, start_time: datetime, end_time: datetime
    ) -> list[ProviderEvent]:
        events = await super().discover_events(start_time, end_time)
        return [events[0].model_copy(update={"title": self.title})]

    async def discover_markets(self, event_id: str) -> list[ProviderMarket]:
        self.market_requests += 1
        return await super().discover_markets(event_id)


class NoEligibleMarketsPredictionConnector(FakePredictionConnector):
    async def discover_markets(self, event_id: str) -> list[ProviderMarket]:
        return []


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


class MultiEventPredictionConnector(FakePredictionConnector):
    async def discover_events(
        self, start_time: datetime, end_time: datetime
    ) -> list[ProviderEvent]:
        kickoff = datetime(2026, 7, 30, 20, tzinfo=UTC)
        return [
            ProviderEvent(
                provider=Provider.KALSHI,
                provider_event_id=f"kalshi-{index}",
                title=f"Home {index} vs Away {index}",
                category="MLS",
                home_team=f"Home {index}",
                away_team=f"Away {index}",
                orientation_known=True,
                scheduled_start=kickoff,
                status="open",
            )
            for index in range(2)
        ]

    async def discover_markets(self, event_id: str) -> list[ProviderMarket]:
        index = event_id.rsplit("-", 1)[-1]
        return [
            ProviderMarket(
                provider=Provider.KALSHI,
                provider_event_id=event_id,
                provider_market_id=f"market-{index}",
                title=f"Will Home {index} win?",
                status="open",
                order_book_enabled=True,
                outcomes=[
                    ProviderOutcome(name="Yes", selection_id="yes"),
                    ProviderOutcome(name="No", selection_id="no"),
                ],
            )
        ]


class ConcurrentSportsConnector(FakeSportsConnector):
    def __init__(self) -> None:
        super().__init__()

    async def discover_events(
        self, start_time: datetime, end_time: datetime
    ) -> list[ProviderEvent]:
        kickoff = datetime(2026, 7, 30, 20, tzinfo=UTC)
        return [
            ProviderEvent(
                provider=Provider.ODDSPAPI,
                provider_event_id=f"odds-{index}",
                title=f"Home {index} vs Away {index}",
                category="MLS",
                sport="soccer",
                competition="MLS",
                provider_competition_id="mls",
                home_team=f"Home {index}",
                away_team=f"Away {index}",
                scheduled_start=kickoff,
                status="Pre-Game",
            )
            for index in range(2)
        ]

    async def get_events_odds(
        self, events: list[ProviderEvent]
    ) -> dict[str, list[ProviderSportsbookQuote]]:
        self.bulk_odds_requests += 1
        return {event.provider_event_id: [] for event in events}


class FailingBulkSportsConnector(FakeSportsConnector):
    async def get_events_odds(
        self, events: list[ProviderEvent]
    ) -> dict[str, list[ProviderSportsbookQuote]]:
        self.bulk_odds_requests += 1
        raise RuntimeError("bulk pricing unavailable")


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
    assert snapshot.predictions[0].provider_outcome_id == "yes"
    assert snapshot.predictions[0].provider_outcome_name == "Yes"
    assert snapshot.predictions[0].provider_source_market_id == "kalshi-home"
    assert snapshot.sportsbooks[0].provider_market_id == "101"
    assert snapshot.sportsbooks[0].provider_outcome_id == "101"
    opportunity = snapshot.opportunities[0]
    assert opportunity.sport == "soccer"
    assert opportunity.bookmaker_id == "pinnacle"
    assert opportunity.prediction_market_best_ask == Decimal("0.52")
    assert opportunity.edge_percentage_points > Decimal("5")


async def test_sportsbook_pricing_uses_one_bulk_request_for_multiple_events() -> None:
    now = datetime(2026, 7, 30, 16, tzinfo=UTC)
    sports = ConcurrentSportsConnector()

    await collect_live_snapshot(
        [MultiEventPredictionConnector()],
        sports,  # type: ignore[arg-type]
        Settings(
            enabled_bookmakers=["pinnacle"],
            provider_request_concurrency=2,
        ),
        now,
        now,
        now + timedelta(hours=8),
    )

    assert sports.bulk_odds_requests == 1


async def test_bulk_odds_failure_preserves_completed_event_matches() -> None:
    now = datetime(2026, 7, 30, 16, tzinfo=UTC)
    sports = FailingBulkSportsConnector()

    snapshot = await collect_live_snapshot(
        [FakePredictionConnector()],
        sports,  # type: ignore[arg-type]
        Settings(enabled_bookmakers=["pinnacle"]),
        now,
        now,
        now + timedelta(hours=8),
    )

    assert sports.bulk_odds_requests == 1
    assert any(audit.matched for audit in snapshot.event_matches)
    assert snapshot.sportsbooks == []
    assert snapshot.opportunities == []


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


async def test_fuzzy_event_above_threshold_is_approved_and_priced() -> None:
    now = datetime(2026, 7, 30, 16, tzinfo=UTC)
    connector = RenamedPredictionConnector("Inter Miamii vs Atlanta United")
    snapshot = await collect_live_snapshot(
        [connector],
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
    assert prediction_audit.rejection_reasons == []
    assert prediction_audit.normalized_participant_one == "inter miamii"
    assert prediction_audit.sportsbook_home_team == "Inter Miami"
    assert prediction_audit.sportsbook_away_team == "Atlanta United"
    assert prediction_audit.sportsbook_kickoff_time_utc == datetime(2026, 7, 30, 20, tzinfo=UTC)
    assert snapshot.predictions
    assert snapshot.opportunities
    assert connector.market_requests == 1


async def test_odds_are_not_requested_without_an_eligible_prediction_market() -> None:
    now = datetime(2026, 7, 30, 16, tzinfo=UTC)
    sports = FakeSportsConnector()

    snapshot = await collect_live_snapshot(
        [NoEligibleMarketsPredictionConnector()],
        sports,  # type: ignore[arg-type]
        Settings(enabled_bookmakers=["pinnacle"], edge_threshold_pp=Decimal("3")),
        now,
        now,
        now + timedelta(hours=8),
    )

    prediction_audit = next(
        item for item in snapshot.event_matches if item.provider == Provider.KALSHI
    )
    assert prediction_audit.matched
    assert sports.bulk_odds_requests == 0
    assert sports.odds_requests == 0
    assert snapshot.sportsbooks == []
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


def test_event_index_shortlists_before_fuzzy_scoring() -> None:
    kickoff = datetime(2026, 7, 30, 20, tzinfo=UTC)
    target = ProviderEvent(
        provider=Provider.ODDSPAPI,
        provider_event_id="target",
        title="Inter Miami vs Atlanta United",
        sport="soccer",
        competition="MLS",
        home_team="Inter Miami",
        away_team="Atlanta United",
        scheduled_start=kickoff,
        status="open",
    )
    unrelated = [
        ProviderEvent(
            provider=Provider.ODDSPAPI,
            provider_event_id=f"unrelated-{index}",
            title=f"Club {index} vs Other {index}",
            sport="soccer",
            competition="Other League",
            home_team=f"Club {index}",
            away_team=f"Other {index}",
            scheduled_start=kickoff + timedelta(hours=1 + index % 48),
            status="open",
        )
        for index in range(1495)
    ]
    prediction = ProviderEvent(
        provider=Provider.POLYMARKET,
        provider_event_id="prediction",
        title="Inter Miami CF vs Atlanta Utd",
        sport="soccer",
        competition="Major League Soccer",
        home_team="Inter Miami CF",
        away_team="Atlanta Utd",
        scheduled_start=kickoff,
        status="open",
    )

    index = SportsbookEventIndex([*unrelated, target], tolerance_minutes=15)
    candidates = index.candidates(prediction, tolerance_minutes=15)

    assert [event.provider_event_id for event in candidates] == ["target"]


def test_kalshi_lexington_uses_ordered_teams_and_usl_competition_alias() -> None:
    kickoff = datetime(2026, 7, 30, 20, tzinfo=UTC)
    prediction = ProviderEvent(
        provider=Provider.KALSHI,
        provider_event_id="kalshi-lexington",
        title="Lexington SC vs. Monterey Bay FC: Regulation Time Moneyline",
        sport="soccer",
        competition="USL Championship",
        home_team="Lexington SC",
        away_team="Monterey Bay FC",
        participant_one="Lexington SC",
        participant_two="Monterey Bay FC",
        orientation_known=True,
        extraction_source="event_title",
        scheduled_start=kickoff,
        status="open",
    )
    correct = ProviderEvent(
        provider=Provider.ODDSPAPI,
        provider_event_id="oddspapi-lexington",
        title="Lexington SC vs Monterey Bay FC",
        sport="soccer",
        competition="USA - USL Championship",
        home_team="Lexington SC",
        away_team="Monterey Bay FC",
        scheduled_start=kickoff,
        status="open",
    )
    wrong_nearby = ProviderEvent(
        provider=Provider.ODDSPAPI,
        provider_event_id="oddspapi-wrong",
        title="Charleston Battery vs Louisville City",
        sport="soccer",
        competition="USA - USL Championship",
        home_team="Charleston Battery",
        away_team="Louisville City",
        scheduled_start=kickoff,
        status="open",
    )

    candidates = SportsbookEventIndex(
        [wrong_nearby, correct],
        tolerance_minutes=15,
    ).candidates(prediction, tolerance_minutes=15)
    matched, audit = audit_prediction_event(prediction, candidates, 15)

    assert [event.provider_event_id for event in candidates] == ["oddspapi-lexington"]
    assert matched == correct
    assert audit.matched
    assert audit.sportsbook_home_team == "Lexington SC"
    assert audit.sportsbook_away_team == "Monterey Bay FC"


def test_kalshi_lexington_reports_kickoff_mismatch_against_correct_fixture() -> None:
    kalshi_kickoff = datetime(2026, 8, 2, 2, tzinfo=UTC)
    oddspapi_kickoff = kalshi_kickoff - timedelta(hours=3)
    prediction = ProviderEvent(
        provider=Provider.KALSHI,
        provider_event_id="kalshi-lexington",
        title="Lexington SC vs. Monterey Bay FC: Regulation Time Moneyline",
        sport="soccer",
        competition="USL Championship",
        home_team="Lexington SC",
        away_team="Monterey Bay FC",
        participant_one="Lexington SC",
        participant_two="Monterey Bay FC",
        orientation_known=True,
        extraction_source="event_title",
        scheduled_start=kalshi_kickoff,
        status="open",
    )
    correct = ProviderEvent(
        provider=Provider.ODDSPAPI,
        provider_event_id="oddspapi-lexington",
        title="Lexington SC vs Monterey Bay FC",
        sport="soccer",
        competition="USA - USL Championship",
        home_team="Lexington SC",
        away_team="Monterey Bay FC",
        scheduled_start=oddspapi_kickoff,
        status="open",
    )
    wrong_nearby = ProviderEvent(
        provider=Provider.ODDSPAPI,
        provider_event_id="oddspapi-wrong",
        title="Charleston Battery vs Louisville City",
        sport="soccer",
        competition="USA - USL Championship",
        home_team="Charleston Battery",
        away_team="Louisville City",
        scheduled_start=kalshi_kickoff,
        status="open",
    )

    candidates = SportsbookEventIndex(
        [wrong_nearby, correct],
        tolerance_minutes=15,
    ).candidates(prediction, tolerance_minutes=15)
    matched, audit = audit_prediction_event(prediction, candidates, 15)

    assert [event.provider_event_id for event in candidates] == ["oddspapi-lexington"]
    assert matched is None
    assert audit.sportsbook_event_id == "oddspapi-lexington"
    assert "kickoff_outside_tolerance" in audit.rejection_reasons
    assert "home_team_mismatch" not in audit.rejection_reasons
    assert "away_team_mismatch" not in audit.rejection_reasons

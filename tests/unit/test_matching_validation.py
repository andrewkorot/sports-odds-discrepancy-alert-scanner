from datetime import UTC, datetime, timedelta

from app.domain.enums import MarketStatus, MatchConfidence
from app.domain.models import PredictionMarketQuote, SportsbookQuote
from app.providers.mock.data import mock_snapshot
from app.services.event_matching import match_event
from app.services.market_validation import settlement_compatible


def quotes() -> tuple[PredictionMarketQuote, SportsbookQuote]:
    _, predictions, sportsbooks, _ = mock_snapshot(datetime(2026, 1, 1, tzinfo=UTC))
    return predictions[0], sportsbooks[0]


def test_team_alias_matching() -> None:
    prediction, sportsbook = quotes()
    prediction = prediction.model_copy(update={"home_team": "Inter Miami CF"})
    assert match_event(prediction, sportsbook).confidence == MatchConfidence.APPROVED_ALIAS


def test_reversed_home_away_rejected() -> None:
    prediction, sportsbook = quotes()
    sportsbook = sportsbook.model_copy(
        update={"home_team": sportsbook.away_team, "away_team": sportsbook.home_team}
    )
    assert match_event(prediction, sportsbook).confidence == MatchConfidence.REJECTED


def test_kickoff_tolerance_inclusive() -> None:
    prediction, sportsbook = quotes()
    sportsbook = sportsbook.model_copy(
        update={"kickoff_time_utc": sportsbook.kickoff_time_utc + timedelta(minutes=10)}
    )
    assert match_event(prediction, sportsbook).confidence == MatchConfidence.EXACT
    sportsbook = sportsbook.model_copy(
        update={"kickoff_time_utc": sportsbook.kickoff_time_utc + timedelta(seconds=1)}
    )
    assert match_event(prediction, sportsbook).confidence == MatchConfidence.REJECTED


def test_settlement_extra_time_rejected() -> None:
    prediction, sportsbook = quotes()
    assert settlement_compatible(prediction, sportsbook)
    assert not settlement_compatible(
        prediction.model_copy(update={"includes_extra_time": True}), sportsbook
    )


def test_live_market_will_not_be_open() -> None:
    prediction, _ = quotes()
    assert (
        prediction.model_copy(update={"market_status": MarketStatus.LIVE}).market_status == "live"
    )

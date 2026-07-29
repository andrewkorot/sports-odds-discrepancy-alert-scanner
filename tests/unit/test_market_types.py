from datetime import UTC, datetime
from decimal import Decimal

from app.domain.enums import MarketType, Period, Selection
from app.domain.models import PredictionMarketQuote, SportsbookQuote
from app.providers.mock.data import mock_snapshot
from app.services.market_validation import selection_rejections


def pair(
    market_type: MarketType, selection: Selection
) -> tuple[PredictionMarketQuote, SportsbookQuote]:
    _, predictions, sportsbooks, _ = mock_snapshot(datetime(2026, 7, 29, 16, tzinfo=UTC))
    prediction = next(
        q
        for q in predictions
        if q.provider == "kalshi" and q.market_type == market_type and q.selection == selection
    )
    sportsbook = next(
        q
        for q in sportsbooks
        if q.market_type == market_type
        and q.selection == selection
        and q.line == prediction.line
        and q.participant == prediction.participant
    )
    return prediction, sportsbook


def test_moneyline_home_and_draw_match() -> None:
    assert selection_rejections(*pair(MarketType.MONEYLINE, Selection.HOME)) == []
    assert selection_rejections(*pair(MarketType.MONEYLINE, Selection.DRAW)) == []


def test_total_side_line_and_period_matching() -> None:
    prediction, sportsbook = pair(MarketType.TOTAL, Selection.OVER)
    assert selection_rejections(prediction, sportsbook) == []
    assert "line_mismatch" in selection_rejections(
        prediction, sportsbook.model_copy(update={"line": Decimal("3.0")})
    )
    assert "side_mismatch" in selection_rejections(
        prediction, sportsbook.model_copy(update={"selection": Selection.UNDER})
    )
    assert "period_mismatch" in selection_rejections(
        prediction, sportsbook.model_copy(update={"period": Period.FIRST_HALF})
    )


def test_spread_team_line_and_ambiguous_line() -> None:
    prediction, sportsbook = pair(MarketType.SPREAD, Selection.HOME)
    assert selection_rejections(prediction, sportsbook) == []
    assert "line_mismatch" in selection_rejections(
        prediction, sportsbook.model_copy(update={"line": Decimal("-1.5")})
    )
    assert "team_mismatch" in selection_rejections(
        prediction, sportsbook.model_copy(update={"participant": "Atlanta United"})
    )
    assert "manual_review" in selection_rejections(
        prediction.model_copy(update={"line": Decimal("-0.25")}),
        sportsbook.model_copy(update={"line": Decimal("-0.25")}),
    )


def test_btts_outcome_and_extra_time() -> None:
    prediction, sportsbook = pair(MarketType.BTTS, Selection.YES)
    assert selection_rejections(prediction, sportsbook) == []
    assert "outcome_mismatch" in selection_rejections(
        prediction, sportsbook.model_copy(update={"selection": Selection.NO})
    )

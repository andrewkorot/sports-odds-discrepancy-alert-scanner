from datetime import datetime
from decimal import Decimal

from app.domain.enums import MarketStatus, MarketType, Period
from app.domain.models import PredictionMarketQuote, SportsbookQuote


def quote_age_seconds(source_timestamp: datetime, now: datetime) -> Decimal:
    return Decimal(str(max(0.0, (now - source_timestamp).total_seconds())))


def settlement_compatible(prediction: PredictionMarketQuote, sportsbook: SportsbookQuote) -> bool:
    return (
        prediction.market_type == sportsbook.market_type
        and prediction.period == sportsbook.period == Period.REGULATION
        and not prediction.includes_extra_time
        and not prediction.includes_penalties
        and prediction.settlement_rule == sportsbook.settlement_rule
    )


def selection_rejections(
    prediction: PredictionMarketQuote, sportsbook: SportsbookQuote
) -> list[str]:
    reasons: list[str] = []
    if prediction.period != sportsbook.period:
        reasons.append("period_mismatch")
    if prediction.settlement_rule != sportsbook.settlement_rule:
        reasons.append("settlement_mismatch")
    if prediction.selection != sportsbook.selection:
        reasons.append(
            "side_mismatch" if prediction.market_type == MarketType.TOTAL else "outcome_mismatch"
        )
    if prediction.line != sportsbook.line:
        reasons.append("line_mismatch")
    if prediction.participant != sportsbook.participant:
        reasons.append("team_mismatch")
    if prediction.market_type in {MarketType.TOTAL, MarketType.SPREAD}:
        line = prediction.line
        if line is None or abs(line) % 1 != Decimal("0.5"):
            reasons.append("manual_review")
    return list(dict.fromkeys(reasons))


def market_status_rejections(
    prediction: PredictionMarketQuote, sportsbook: SportsbookQuote
) -> list[str]:
    """Identify the inactive side instead of returning an ambiguous pair-level reason."""
    reasons: list[str] = []
    if prediction.market_status != MarketStatus.OPEN:
        reasons.append("prediction_market_inactive")
    if sportsbook.market_status != MarketStatus.OPEN:
        reasons.append("sportsbook_quote_inactive")
    return reasons

from datetime import datetime
from decimal import Decimal

from app.domain.enums import MarketStatus, MarketType, Period
from app.domain.models import PredictionMarketQuote, SportsbookQuote


def quote_age_seconds(source_timestamp: datetime, now: datetime) -> Decimal:
    return Decimal(str(max(0.0, (now - source_timestamp).total_seconds())))


def settlement_compatible(prediction: PredictionMarketQuote, sportsbook: SportsbookQuote) -> bool:
    return (
        prediction.market_type == sportsbook.market_type == MarketType.MATCH_WINNER
        and prediction.period == sportsbook.period == Period.REGULATION
        and not prediction.includes_extra_time
        and not prediction.includes_penalties
    )


def markets_open(prediction: PredictionMarketQuote, sportsbook: SportsbookQuote) -> bool:
    return (
        prediction.market_status == MarketStatus.OPEN
        and sportsbook.market_status == MarketStatus.OPEN
    )

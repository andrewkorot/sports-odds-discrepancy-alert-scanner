from datetime import datetime, timedelta

from app.core.config import Settings
from app.domain.enums import MatchConfidence, Provider
from app.domain.models import Bookmaker, Opportunity, PredictionMarketQuote, SportsbookQuote
from app.services.edge_calculator import calculate_edge_percentage_points
from app.services.event_matching import match_event
from app.services.market_validation import (
    markets_open,
    quote_age_seconds,
    settlement_compatible,
)


def detect_opportunities(
    predictions: list[PredictionMarketQuote],
    sportsbooks: list[SportsbookQuote],
    bookmakers: list[Bookmaker],
    settings: Settings,
    now: datetime,
) -> list[Opportunity]:
    bookmaker_by_id = {item.canonical_id: item for item in bookmakers}
    results: list[Opportunity] = []
    for prediction in predictions:
        for sportsbook in sportsbooks:
            book = bookmaker_by_id.get(sportsbook.bookmaker_id)
            if book is None or not book.enabled or book.availability_status != "available":
                continue
            matched = match_event(prediction, sportsbook)
            compatible = settlement_compatible(prediction, sportsbook)
            prediction_age = quote_age_seconds(prediction.source_timestamp, now)
            sportsbook_age = quote_age_seconds(sportsbook.source_timestamp, now)
            fresh = (
                prediction_age <= settings.max_prediction_price_age_seconds
                and sportsbook_age <= settings.max_sportsbook_price_age_seconds
            )
            minimum_size = (
                settings.min_kalshi_ask_size
                if prediction.provider == Provider.KALSHI
                else settings.min_polymarket_ask_size
            )
            liquid = prediction.best_ask_size >= minimum_size
            edge = calculate_edge_percentage_points(
                prediction.best_ask_probability, sportsbook.implied_probability
            )
            kickoff_delta = prediction.kickoff_time_utc - now
            in_window = (
                timedelta(minutes=settings.min_minutes_before_kickoff)
                <= kickoff_delta
                <= (timedelta(hours=settings.max_hours_before_kickoff))
            )
            if not (
                matched.confidence in {MatchConfidence.EXACT, MatchConfidence.APPROVED_ALIAS}
                and compatible
                and markets_open(prediction, sportsbook)
                and prediction.canonical_event_id == sportsbook.canonical_event_id
                and in_window
                and fresh
                and liquid
                and edge >= settings.edge_threshold_pp
            ):
                continue
            results.append(
                Opportunity(
                    canonical_event_id=prediction.canonical_event_id,
                    competition=prediction.competition,
                    home_team=prediction.home_team,
                    away_team=prediction.away_team,
                    kickoff_time_utc=prediction.kickoff_time_utc,
                    market_type=prediction.market_type,
                    selection=prediction.selection,
                    prediction_market_provider=prediction.provider,
                    prediction_market_id=prediction.provider_market_id,
                    prediction_market_best_bid=prediction.best_bid_probability,
                    prediction_market_best_ask=prediction.best_ask_probability,
                    prediction_market_bid_size=prediction.best_bid_size,
                    prediction_market_ask_size=prediction.best_ask_size,
                    prediction_market_direct_url=prediction.direct_url,
                    bookmaker_id=sportsbook.bookmaker_id,
                    bookmaker_display_name=sportsbook.bookmaker_display_name,
                    sportsbook_decimal_odds=sportsbook.decimal_odds,
                    sportsbook_implied_probability=sportsbook.implied_probability,
                    sportsbook_direct_url=sportsbook.direct_url,
                    edge_percentage_points=edge,
                    configured_threshold=settings.edge_threshold_pp,
                    prediction_quote_age_seconds=prediction_age,
                    sportsbook_quote_age_seconds=sportsbook_age,
                    liquidity_passed=liquid,
                    freshness_passed=fresh,
                    mapping_confidence=matched.confidence,
                    detected_at=now,
                )
            )
    return sorted(results, key=lambda item: item.edge_percentage_points, reverse=True)

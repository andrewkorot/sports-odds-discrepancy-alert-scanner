from datetime import datetime
from decimal import Decimal

from app.core.config import Settings
from app.domain.enums import MarketStatus, MatchConfidence, Provider, VolumeSource
from app.domain.models import (
    Bookmaker,
    MarketCandidate,
    MissingSportsbookOutcomeAudit,
    Opportunity,
    OrderBookSnapshot,
    PredictionMarketQuote,
    SportsbookQuote,
)
from app.services.edge_calculator import calculate_edge_percentage_points
from app.services.event_matching import match_event
from app.services.event_time import event_time_rejections
from app.services.liquidity import make_level, qualify_liquidity
from app.services.market_validation import (
    market_status_rejections,
    quote_age_seconds,
    selection_rejections,
    settlement_compatible,
)

type QuoteAlignmentKey = tuple[
    object,
    str,
    object,
    object,
    Decimal | None,
    str | None,
]
type SportsbookQuoteIdentity = tuple[
    object,
    str,
    object,
    object,
    Decimal | None,
    str | None,
    str,
]


def _alignment_key(quote: PredictionMarketQuote | SportsbookQuote) -> QuoteAlignmentKey:
    return (
        quote.canonical_event_id,
        quote.sport,
        quote.market_type,
        quote.selection,
        quote.line,
        quote.participant,
    )


def _sportsbook_identity(quote: SportsbookQuote) -> SportsbookQuoteIdentity:
    return (*_alignment_key(quote), quote.bookmaker_id)


def deduplicate_sportsbook_quotes(quotes: list[SportsbookQuote]) -> list[SportsbookQuote]:
    """Keep the newest open quote per exact bookmaker/outcome identity.

    If an identity has no open record, its newest closed record is retained so
    market-inactive diagnostics are not incorrectly converted into missing outcomes.
    """
    grouped: dict[SportsbookQuoteIdentity, list[SportsbookQuote]] = {}
    for quote in quotes:
        grouped.setdefault(_sportsbook_identity(quote), []).append(quote)
    result: list[SportsbookQuote] = []
    for values in grouped.values():
        open_values = [quote for quote in values if quote.market_status == MarketStatus.OPEN]
        eligible = open_values or values
        result.append(
            max(eligible, key=lambda quote: (quote.source_timestamp, quote.received_timestamp))
        )
    return result


def _fallback_book(quote: PredictionMarketQuote) -> OrderBookSnapshot:
    bid, ask = quote.best_bid_probability, quote.best_ask_probability
    return OrderBookSnapshot(
        provider=quote.provider,
        provider_market_id=quote.provider_market_id,
        outcome=quote.selection,
        bids=[make_level(bid, Decimal("5000"))],
        asks=[make_level(ask, Decimal("5000"))],
        best_bid=bid,
        best_ask=ask,
        midpoint=(bid + ask) / 2,
        spread=ask - bid,
        spread_cents=(ask - bid) * 100,
        source_timestamp=quote.source_timestamp,
        received_timestamp=quote.received_timestamp,
        trailing_24h_volume_usd=Decimal("10000"),
        volume_source=VolumeSource.PROVIDER_REPORTED,
    )


def evaluate_candidates(
    predictions: list[PredictionMarketQuote],
    sportsbooks: list[SportsbookQuote],
    bookmakers: list[Bookmaker],
    settings: Settings,
    now: datetime,
    order_books: dict[str, OrderBookSnapshot] | None = None,
) -> list[MarketCandidate]:
    bookmaker_by_id = {item.canonical_id: item for item in bookmakers}
    sportsbook_index: dict[QuoteAlignmentKey, list[SportsbookQuote]] = {}
    for sportsbook in deduplicate_sportsbook_quotes(sportsbooks):
        sportsbook_index.setdefault(_alignment_key(sportsbook), []).append(sportsbook)
    candidates: list[MarketCandidate] = []
    for prediction in predictions:
        if prediction.sport not in settings.enabled_sports:
            continue
        for sportsbook in sportsbook_index.get(_alignment_key(prediction), []):
            book = bookmaker_by_id.get(sportsbook.bookmaker_id)
            reasons: list[str] = []
            if book is None or not book.enabled or book.availability_status != "available":
                reasons.append("provider_inactive")
            reasons.extend(market_status_rejections(prediction, sportsbook))
            if prediction.market_type.value not in settings.enabled_market_types:
                reasons.append("market_type_disabled")
            reasons.extend(
                event_time_rejections(
                    prediction.kickoff_time_utc,
                    now,
                    settings.min_minutes_before_kickoff,
                )
            )
            matched = match_event(
                prediction,
                sportsbook,
                settings.event_match_kickoff_tolerance_minutes * 60,
            )
            if matched.confidence not in {MatchConfidence.EXACT, MatchConfidence.APPROVED_ALIAS}:
                reasons.append(matched.reason or "mapping_rejected")
            if not settlement_compatible(prediction, sportsbook):
                reasons.append("settlement_mismatch")
            # Freshness is based on when this scanner last retrieved the quote.
            # Provider source timestamps often mean "last price change", so an
            # unchanged price can have an old source timestamp despite having
            # been successfully verified during the current scan.
            prediction_age = quote_age_seconds(prediction.received_timestamp, now)
            sportsbook_age = quote_age_seconds(sportsbook.received_timestamp, now)
            if prediction_age > settings.max_prediction_price_age_seconds:
                reasons.append("stale_prediction_quote")
            if sportsbook_age > settings.max_sportsbook_price_age_seconds:
                reasons.append("stale_sportsbook_quote")
            snapshot = (order_books or {}).get(
                prediction.provider_market_id, _fallback_book(prediction)
            )
            liquidity = qualify_liquidity(snapshot, settings)
            reasons.extend(liquidity.rejection_reasons)
            minimum_size = (
                settings.min_kalshi_ask_size
                if prediction.provider == Provider.KALSHI
                else settings.min_polymarket_ask_size
            )
            if prediction.best_ask_size < minimum_size:
                reasons.append("insufficient_ask_size")
            reasons.extend(selection_rejections(prediction, sportsbook))
            edge = calculate_edge_percentage_points(
                prediction.best_ask_probability, sportsbook.implied_probability
            )
            if prediction.best_ask_probability <= sportsbook.implied_probability:
                reasons.append("prediction_probability_not_higher")
            if edge < settings.edge_threshold_pp:
                reasons.append("edge_below_threshold")
            reasons = list(dict.fromkeys(reasons))
            candidates.append(
                MarketCandidate(
                    prediction_quote=prediction,
                    sportsbook_quote=sportsbook,
                    order_book=snapshot,
                    liquidity=liquidity,
                    accepted=not reasons,
                    rejection_reasons=reasons,
                    edge_percentage_points=edge,
                    configured_threshold=settings.edge_threshold_pp,
                    evaluated_at=now,
                )
            )
    return candidates


def missing_sportsbook_outcomes(
    predictions: list[PredictionMarketQuote],
    sportsbooks: list[SportsbookQuote],
    bookmakers: list[Bookmaker],
    now: datetime,
) -> list[MissingSportsbookOutcomeAudit]:
    """Record absent exact outcomes without constructing a synthetic sportsbook quote."""
    available = {
        (
            quote.canonical_event_id,
            quote.bookmaker_id,
            quote.market_type,
            quote.selection,
            quote.line,
            quote.participant,
        )
        for quote in deduplicate_sportsbook_quotes(sportsbooks)
    }
    enabled_books = [book for book in bookmakers if book.enabled]
    audits: list[MissingSportsbookOutcomeAudit] = []
    seen: set[tuple[object, ...]] = set()
    for prediction in predictions:
        for book in enabled_books:
            key = (
                prediction.provider,
                prediction.provider_market_id,
                book.canonical_id,
                prediction.market_type,
                prediction.selection,
                prediction.line,
                prediction.participant,
            )
            if key in seen:
                continue
            seen.add(key)
            outcome_key = (
                prediction.canonical_event_id,
                book.canonical_id,
                prediction.market_type,
                prediction.selection,
                prediction.line,
                prediction.participant,
            )
            if outcome_key not in available:
                audits.append(
                    MissingSportsbookOutcomeAudit(
                        prediction_quote=prediction,
                        bookmaker_id=book.canonical_id,
                        bookmaker_display_name=book.display_name,
                        evaluated_at=now,
                    )
                )
    return audits


def opportunities_from_candidates(
    candidates: list[MarketCandidate], settings: Settings
) -> list[Opportunity]:
    results: list[Opportunity] = []
    for candidate in candidates:
        if not candidate.accepted:
            continue
        prediction = candidate.prediction_quote
        sportsbook = candidate.sportsbook_quote
        quality = candidate.liquidity
        assert quality.midpoint is not None
        assert quality.spread_cents is not None
        assert quality.trailing_24h_volume_usd is not None
        assert quality.volume_source is not None
        results.append(
            Opportunity(
                canonical_event_id=prediction.canonical_event_id,
                sport=prediction.sport,
                competition=prediction.competition,
                home_team=prediction.home_team,
                away_team=prediction.away_team,
                kickoff_time_utc=prediction.kickoff_time_utc,
                market_type=prediction.market_type,
                selection=prediction.selection,
                participant=prediction.participant,
                line=prediction.line,
                period=prediction.period,
                settlement_rule=prediction.settlement_rule,
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
                edge_percentage_points=candidate.edge_percentage_points,
                configured_threshold=settings.edge_threshold_pp,
                prediction_quote_age_seconds=quote_age_seconds(
                    prediction.received_timestamp, candidate.evaluated_at
                ),
                sportsbook_quote_age_seconds=quote_age_seconds(
                    sportsbook.received_timestamp, candidate.evaluated_at
                ),
                liquidity_passed=True,
                freshness_passed=True,
                mapping_confidence=match_event(
                    prediction,
                    sportsbook,
                    settings.event_match_kickoff_tolerance_minutes * 60,
                ).confidence,
                detected_at=candidate.evaluated_at,
                midpoint=quality.midpoint,
                spread_cents=quality.spread_cents,
                bid_depth_within_window_usd=quality.bid_depth_within_window_usd,
                ask_depth_within_window_usd=quality.ask_depth_within_window_usd,
                total_depth_within_window_usd=quality.total_depth_within_window_usd,
                trailing_24h_volume_usd=quality.trailing_24h_volume_usd,
                volume_source=quality.volume_source,
            )
        )
    return sorted(results, key=lambda item: item.edge_percentage_points, reverse=True)


def detect_opportunities(
    predictions: list[PredictionMarketQuote],
    sportsbooks: list[SportsbookQuote],
    bookmakers: list[Bookmaker],
    settings: Settings,
    now: datetime,
    order_books: dict[str, OrderBookSnapshot] | None = None,
) -> list[Opportunity]:
    return opportunities_from_candidates(
        evaluate_candidates(predictions, sportsbooks, bookmakers, settings, now, order_books),
        settings,
    )

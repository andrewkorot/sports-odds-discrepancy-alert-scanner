from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from rapidfuzz import fuzz

from app.core.config import Settings
from app.domain.enums import (
    AvailabilityStatus,
    MarketStatus,
    MarketType,
    MatchConfidence,
    Period,
    Provider,
    Selection,
    VolumeSource,
)
from app.domain.models import (
    Bookmaker,
    CanonicalEvent,
    EventMatchAudit,
    MarketCandidate,
    Opportunity,
    OrderBookSnapshot,
    PredictionMarketQuote,
    SportsbookQuote,
)
from app.providers.base import PredictionMarketConnector
from app.providers.oddspapi.connector import SportsOddsConnector
from app.providers.oddspapi.mapping import CANONICAL_BOOKMAKERS
from app.providers.records import (
    ProviderEvent,
    ProviderMarket,
    ProviderSportsbookQuote,
    ProviderTrade,
)
from app.services.edge_calculator import decimal_odds_to_implied_probability
from app.services.normalization import (
    competition_identity,
    normalize_competition,
    normalize_team,
    normalize_text,
    qualifiers_compatible,
    team_identity,
)
from app.services.opportunity_detector import evaluate_candidates, opportunities_from_candidates
from app.services.provider_normalization import normalize_order_book

logger = logging.getLogger("uvicorn.error")


def _http_error_context(exc: Exception) -> tuple[int | None, str | None]:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None, None
    return (
        exc.response.status_code,
        exc.response.headers.get("retry-after"),
    )


@dataclass(frozen=True)
class LiveScanSnapshot:
    events: list[CanonicalEvent]
    predictions: list[PredictionMarketQuote]
    sportsbooks: list[SportsbookQuote]
    bookmakers: list[Bookmaker]
    order_books: dict[str, OrderBookSnapshot]
    candidates: list[MarketCandidate]
    opportunities: list[Opportunity]
    event_matches: list[EventMatchAudit]


class SportsbookEventIndex:
    """Pre-normalized indexes that prevent all-to-all provider event matching."""

    def __init__(self, events: list[ProviderEvent], tolerance_minutes: int) -> None:
        self.by_id = {event.provider_event_id: event for event in events}
        self._bucket_seconds = max(60, tolerance_minutes * 60)
        self._ordered: dict[tuple[str, str, str, str], list[ProviderEvent]] = {}
        self._unordered: dict[tuple[str, str, str, str], list[ProviderEvent]] = {}
        self._ordered_teams: dict[tuple[str, str, str], list[ProviderEvent]] = {}
        self._unordered_teams: dict[tuple[str, str, str], list[ProviderEvent]] = {}
        self._by_time: dict[tuple[str, int], list[ProviderEvent]] = {}
        for event in events:
            if event.scheduled_start is None:
                continue
            sport = self._sport(event)
            competition = self._competition(event)
            home = normalize_team(event.home_team or "")
            away = normalize_team(event.away_team or "")
            if home and away:
                self._ordered.setdefault(
                    (sport, competition, home, away),
                    [],
                ).append(event)
                self._ordered_teams.setdefault((sport, home, away), []).append(event)
                first, second = sorted((home, away))
                self._unordered.setdefault(
                    (sport, competition, first, second),
                    [],
                ).append(event)
                self._unordered_teams.setdefault((sport, first, second), []).append(event)
            self._by_time.setdefault(
                (sport, self._time_bucket(event.scheduled_start)),
                [],
            ).append(event)

    def candidates(self, prediction: ProviderEvent, tolerance_minutes: int) -> list[ProviderEvent]:
        if prediction.scheduled_start is None:
            return []
        sport = self._sport(prediction)
        competition = self._competition(prediction)
        unordered = bool(
            prediction.participant_one
            and prediction.participant_two
            and not prediction.orientation_known
        )
        if unordered:
            first, second = sorted(
                (
                    normalize_team(prediction.participant_one or ""),
                    normalize_team(prediction.participant_two or ""),
                )
            )
            exact = self._unordered.get((sport, competition, first, second), [])
            same_teams = self._unordered_teams.get((sport, first, second), [])
        else:
            home = normalize_team(prediction.home_team or "")
            away = normalize_team(prediction.away_team or "")
            exact = self._ordered.get(
                (
                    sport,
                    competition,
                    home,
                    away,
                ),
                [],
            )
            same_teams = self._ordered_teams.get((sport, home, away), [])
        exact_in_window = self._within_window(
            exact,
            prediction.scheduled_start,
            tolerance_minutes,
        )
        if exact_in_window:
            return exact_in_window
        # Preserve home/away orientation and let the full audit validate the
        # competition. This prevents a provider league-label difference from
        # replacing the true fixture with an unrelated nearby kickoff.
        same_teams_in_window = self._within_window(
            same_teams,
            prediction.scheduled_start,
            tolerance_minutes,
        )
        if same_teams_in_window:
            return same_teams_in_window
        if same_teams:
            # Keep the correct fixture in the audit even when its kickoff is
            # outside tolerance. The full matcher will reject it with the true
            # kickoff reason instead of showing an unrelated nearby fixture as
            # the closest candidate.
            return same_teams

        bucket = self._time_bucket(prediction.scheduled_start)
        nearby: dict[str, ProviderEvent] = {}
        for offset in (-1, 0, 1):
            for event in self._by_time.get((sport, bucket + offset), []):
                nearby[event.provider_event_id] = event
        within_window = self._within_window(
            list(nearby.values()),
            prediction.scheduled_start,
            tolerance_minutes,
        )
        return [
            event
            for event in within_window
            if fuzz.ratio(competition, self._competition(event)) >= 70
        ]

    def _time_bucket(self, kickoff: datetime) -> int:
        return int(kickoff.timestamp()) // self._bucket_seconds

    @staticmethod
    def _sport(event: ProviderEvent) -> str:
        return (event.sport or "soccer").casefold()

    @staticmethod
    def _competition(event: ProviderEvent) -> str:
        return normalize_competition(event.competition or event.category or "")

    @staticmethod
    def _within_window(
        events: list[ProviderEvent],
        kickoff: datetime,
        tolerance_minutes: int,
    ) -> list[ProviderEvent]:
        tolerance_seconds = tolerance_minutes * 60
        return [
            event
            for event in events
            if event.scheduled_start is not None
            and abs((kickoff - event.scheduled_start).total_seconds()) <= tolerance_seconds
        ]


def canonical_event_id(event: ProviderEvent) -> UUID:
    key = "|".join(
        [
            event.sport or "soccer",
            normalize_competition(event.competition or event.category or ""),
            normalize_team(event.home_team or ""),
            normalize_team(event.away_team or ""),
            event.scheduled_start.astimezone(UTC).isoformat() if event.scheduled_start else "",
        ]
    )
    return uuid5(NAMESPACE_URL, key)


def _split_matchup(title: str) -> tuple[str, str] | None:
    parts = re.split(r"\s+(?:vs?\.?|at|@)\s+", title, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    home = parts[0].strip(" -:")
    away_text = parts[1].strip(" -:")
    away, separator, descriptor = away_text.partition(":")
    if not (
        separator
        and re.search(
            r"\b(regulation\s+time|90\s*minutes?|moneyline|match\s+winner)\b",
            descriptor,
            flags=re.IGNORECASE,
        )
    ):
        away = away_text
    away = away.strip(" -:")
    return (home, away) if home and away else None


def enrich_prediction_event(event: ProviderEvent) -> ProviderEvent | None:
    if event.home_team and event.away_team:
        return event
    if event.participant_one and event.participant_two:
        return event
    matchup = _split_matchup(event.title)
    if matchup is None:
        return None
    if event.provider == Provider.KALSHI:
        return event.model_copy(
            update={
                "sport": event.sport or "soccer",
                "competition": event.competition or event.category,
                "participant_one": matchup[0],
                "participant_two": matchup[1],
                "orientation_known": False,
                "extraction_source": event.extraction_source or "event_title",
            }
        )
    return event.model_copy(
        update={
            "sport": event.sport or "soccer",
            "competition": event.competition or event.category,
            "home_team": matchup[0],
            "away_team": matchup[1],
        }
    )


def match_prediction_event(
    prediction: ProviderEvent,
    sportsbook_events: list[ProviderEvent],
    tolerance_minutes: int,
) -> ProviderEvent | None:
    matched, _audit = audit_prediction_event(prediction, sportsbook_events, tolerance_minutes)
    return matched


def _similar(left: str, right: str) -> bool:
    """Flag plausible operator-review candidates; never auto-approve them."""

    return bool(left and right) and fuzz.ratio(left, right) >= 82


def _event_score(
    prediction: ProviderEvent,
    candidate: ProviderEvent,
    tolerance_minutes: int,
    *,
    unordered: bool,
) -> tuple[Decimal, dict[str, Decimal], list[str]]:
    prediction_names = (
        (prediction.participant_one or "", prediction.participant_two or "")
        if unordered
        else (prediction.home_team or "", prediction.away_team or "")
    )
    candidate_names = (candidate.home_team or "", candidate.away_team or "")
    prediction_teams = tuple(team_identity(name) for name in prediction_names)
    candidate_teams = tuple(team_identity(name) for name in candidate_names)
    direct_qualifiers = all(
        qualifiers_compatible(left, right)
        for left, right in zip(prediction_teams, candidate_teams, strict=True)
    )
    reversed_qualifiers = all(
        qualifiers_compatible(left, right)
        for left, right in zip(prediction_teams, reversed(candidate_teams), strict=True)
    )
    direct_scores = (
        Decimal(str(fuzz.ratio(prediction_teams[0].base_name, candidate_teams[0].base_name))),
        Decimal(str(fuzz.ratio(prediction_teams[1].base_name, candidate_teams[1].base_name))),
    )
    reversed_scores = (
        Decimal(str(fuzz.ratio(prediction_teams[0].base_name, candidate_teams[1].base_name))),
        Decimal(str(fuzz.ratio(prediction_teams[1].base_name, candidate_teams[0].base_name))),
    )
    use_reversed = unordered and sum(reversed_scores) > sum(direct_scores)
    team_scores = reversed_scores if use_reversed else direct_scores
    qualifier_match = reversed_qualifiers if use_reversed else direct_qualifiers
    if not qualifier_match:
        team_scores = (Decimal(), Decimal())

    prediction_competition = competition_identity(
        prediction.competition or prediction.category or "",
        country=prediction.competition_country,
        league_level=prediction.competition_league_level,
        gender=prediction.competition_gender,
        age_group=prediction.competition_age_group,
        season=prediction.competition_season,
        competition_type=prediction.competition_type,
    )
    candidate_competition = competition_identity(
        candidate.competition or candidate.category or "",
        country=candidate.competition_country,
        league_level=candidate.competition_league_level,
        gender=candidate.competition_gender,
        age_group=candidate.competition_age_group,
        season=candidate.competition_season,
        competition_type=candidate.competition_type,
    )
    competition_score = Decimal(
        str(
            fuzz.ratio(
                prediction_competition.canonical_name,
                candidate_competition.canonical_name,
            )
        )
    )
    metadata_pairs = (
        (prediction_competition.country, candidate_competition.country),
        (prediction_competition.gender, candidate_competition.gender),
        (prediction_competition.age_group, candidate_competition.age_group),
        (prediction_competition.league_level, candidate_competition.league_level),
    )
    metadata_conflict = any(
        left is not None and right is not None and left != right for left, right in metadata_pairs
    )
    if metadata_conflict:
        competition_score = Decimal()

    kickoff_score = Decimal()
    kickoff_outside = True
    if prediction.scheduled_start is not None and candidate.scheduled_start is not None:
        delta_seconds = Decimal(
            str(abs((prediction.scheduled_start - candidate.scheduled_start).total_seconds()))
        )
        tolerance_seconds = Decimal(tolerance_minutes * 60)
        kickoff_outside = delta_seconds > tolerance_seconds
        if tolerance_seconds:
            kickoff_score = max(
                Decimal(),
                Decimal("100") * (Decimal("1") - delta_seconds / tolerance_seconds),
            )
    sport_score = (
        Decimal("100")
        if (prediction.sport or "").casefold() == (candidate.sport or "").casefold()
        else Decimal()
    )
    breakdown = {
        "participant_one": team_scores[0],
        "participant_two": team_scores[1],
        "competition": competition_score,
        "kickoff": kickoff_score,
        "sport": sport_score,
    }
    weighted = (
        team_scores[0] * Decimal("0.30")
        + team_scores[1] * Decimal("0.30")
        + competition_score * Decimal("0.20")
        + kickoff_score * Decimal("0.15")
        + sport_score * Decimal("0.05")
    )
    blockers: list[str] = []
    if not qualifier_match:
        blockers.append("team_qualifier_mismatch")
    if metadata_conflict:
        blockers.append("competition_metadata_mismatch")
    if kickoff_outside:
        blockers.append("kickoff_outside_tolerance")
    if prediction.settlement_scope != candidate.settlement_scope:
        blockers.append("settlement_scope_mismatch")
    return weighted, breakdown, blockers


def audit_prediction_event(
    prediction: ProviderEvent,
    sportsbook_events: list[ProviderEvent],
    tolerance_minutes: int,
    fuzzy_min_score: Decimal = Decimal("80"),
    ambiguity_margin: Decimal = Decimal("5"),
) -> tuple[ProviderEvent | None, EventMatchAudit]:
    base: dict[str, object] = {
        "provider": prediction.provider,
        "provider_event_id": prediction.provider_event_id,
        "title": prediction.title,
        "competition": prediction.competition or prediction.category,
        "home_team": prediction.home_team,
        "away_team": prediction.away_team,
        "participant_one": prediction.participant_one,
        "participant_two": prediction.participant_two,
        "normalized_participant_one": normalize_team(prediction.participant_one or ""),
        "normalized_participant_two": normalize_team(prediction.participant_two or ""),
        "orientation_known": prediction.orientation_known,
        "extraction_source": prediction.extraction_source,
        "normalized_competition": normalize_competition(
            prediction.competition or prediction.category or ""
        ),
        "normalized_home_team": normalize_team(prediction.home_team or ""),
        "normalized_away_team": normalize_team(prediction.away_team or ""),
        "kickoff_time_utc": prediction.scheduled_start,
    }
    if prediction.scheduled_start is None:
        return None, EventMatchAudit(**base, rejection_reasons=["missing_kickoff"])
    has_ordered_teams = bool(prediction.home_team and prediction.away_team)
    has_unordered_participants = bool(prediction.participant_one and prediction.participant_two)
    if not has_ordered_teams and not has_unordered_participants:
        return None, EventMatchAudit(**base, rejection_reasons=["missing_teams"])
    if not sportsbook_events:
        return None, EventMatchAudit(**base, rejection_reasons=["no_sportsbook_events"])

    if has_unordered_participants and not prediction.orientation_known:
        return _audit_unordered_prediction(
            prediction,
            sportsbook_events,
            tolerance_minutes,
            base,
            fuzzy_min_score,
            ambiguity_margin,
        )

    prediction_home = normalize_team(prediction.home_team or "")
    prediction_away = normalize_team(prediction.away_team or "")
    prediction_competition = normalize_competition(
        prediction.competition or prediction.category or ""
    )
    scored: list[tuple[Decimal, ProviderEvent, dict[str, Decimal], list[str], list[str]]] = []

    for candidate in sportsbook_events:
        reasons: list[str] = []
        score, breakdown, blockers = _event_score(
            prediction,
            candidate,
            tolerance_minutes,
            unordered=False,
        )
        candidate_home = normalize_team(candidate.home_team or "")
        candidate_away = normalize_team(candidate.away_team or "")
        candidate_competition = normalize_competition(
            candidate.competition or candidate.category or ""
        )
        if (
            prediction_home == candidate_away
            and prediction_away == candidate_home
            and prediction_home
            and prediction_away
        ):
            reasons.append("home_away_reversed")
        else:
            if prediction_home != candidate_home:
                reasons.append("home_team_mismatch")
            if prediction_away != candidate_away:
                reasons.append("away_team_mismatch")
        if prediction_competition != candidate_competition:
            reasons.append("competition_mismatch")
        if (prediction.sport or "soccer").casefold() != (candidate.sport or "soccer").casefold():
            reasons.append("sport_mismatch")
        if prediction.settlement_scope != candidate.settlement_scope:
            reasons.append("settlement_scope_mismatch")
        reasons.extend(blockers)
        if candidate.scheduled_start is None:
            reasons.append("sportsbook_kickoff_missing")
        elif (
            abs((prediction.scheduled_start - candidate.scheduled_start).total_seconds())
            > tolerance_minutes * 60
        ):
            reasons.append("kickoff_outside_tolerance")

        if not reasons:
            raw_values_match = (
                normalize_text(prediction.home_team or "")
                == normalize_text(candidate.home_team or "")
                and normalize_text(prediction.away_team or "")
                == normalize_text(candidate.away_team or "")
                and normalize_text(prediction.competition or prediction.category or "")
                == normalize_text(candidate.competition or candidate.category or "")
            )
            confidence = (
                MatchConfidence.EXACT if raw_values_match else MatchConfidence.APPROVED_ALIAS
            )
            return candidate, EventMatchAudit(
                **base,
                matched=True,
                match_confidence=confidence,
                weighted_score=Decimal("100"),
                score_breakdown={
                    "participant_one": Decimal("100"),
                    "participant_two": Decimal("100"),
                    "competition": Decimal("100"),
                    "kickoff": Decimal("100"),
                    "sport": Decimal("100"),
                },
                sportsbook_event_id=candidate.provider_event_id,
                sportsbook_title=candidate.title,
                sportsbook_competition=candidate.competition or candidate.category,
                sportsbook_home_team=candidate.home_team,
                sportsbook_away_team=candidate.away_team,
                sportsbook_kickoff_time_utc=candidate.scheduled_start,
            )

        scored.append((score, candidate, breakdown, blockers, reasons))

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_candidate, breakdown, blockers, best_reasons = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else None
    fuzzy_name_reasons = {
        "home_team_mismatch",
        "away_team_mismatch",
        "competition_mismatch",
    }
    fuzzy_eligible = (
        best_score >= fuzzy_min_score
        and not blockers
        and set(best_reasons).issubset(fuzzy_name_reasons)
    )
    ambiguous = (
        fuzzy_eligible and runner_up is not None and best_score - runner_up < ambiguity_margin
    )
    if fuzzy_eligible and not ambiguous:
        return best_candidate, EventMatchAudit(
            **base,
            matched=True,
            match_confidence=MatchConfidence.APPROVED_ALIAS,
            weighted_score=best_score,
            runner_up_score=runner_up,
            score_breakdown=breakdown,
            sportsbook_event_id=best_candidate.provider_event_id,
            sportsbook_title=best_candidate.title,
            sportsbook_competition=best_candidate.competition or best_candidate.category,
            sportsbook_home_team=best_candidate.home_team,
            sportsbook_away_team=best_candidate.away_team,
            sportsbook_kickoff_time_utc=best_candidate.scheduled_start,
        )
    if fuzzy_eligible:
        best_reasons = [
            "ambiguous_candidate_margin",
            *best_reasons,
        ]
    return None, EventMatchAudit(
        **base,
        match_confidence=(
            MatchConfidence.MANUAL_REVIEW if fuzzy_eligible else MatchConfidence.REJECTED
        ),
        weighted_score=best_score,
        runner_up_score=runner_up,
        score_breakdown=breakdown,
        sportsbook_event_id=best_candidate.provider_event_id,
        sportsbook_title=best_candidate.title,
        sportsbook_competition=best_candidate.competition or best_candidate.category,
        sportsbook_home_team=best_candidate.home_team,
        sportsbook_away_team=best_candidate.away_team,
        sportsbook_kickoff_time_utc=best_candidate.scheduled_start,
        rejection_reasons=list(dict.fromkeys([*blockers, *best_reasons])),
    )


def _audit_unordered_prediction(
    prediction: ProviderEvent,
    sportsbook_events: list[ProviderEvent],
    tolerance_minutes: int,
    base: dict[str, object],
    fuzzy_min_score: Decimal,
    ambiguity_margin: Decimal,
) -> tuple[ProviderEvent | None, EventMatchAudit]:
    first = normalize_team(prediction.participant_one or "")
    second = normalize_team(prediction.participant_two or "")
    participant_pair = {first, second}
    competition = normalize_competition(prediction.competition or prediction.category or "")
    compatible: list[ProviderEvent] = []
    scored: list[tuple[Decimal, ProviderEvent, dict[str, Decimal], list[str], list[str]]] = []

    for candidate in sportsbook_events:
        candidate_home = normalize_team(candidate.home_team or "")
        candidate_away = normalize_team(candidate.away_team or "")
        reasons: list[str] = []
        score, breakdown, blockers = _event_score(
            prediction,
            candidate,
            tolerance_minutes,
            unordered=True,
        )
        if participant_pair != {candidate_home, candidate_away}:
            reasons.append("participant_pair_mismatch")
        if competition != normalize_competition(candidate.competition or candidate.category or ""):
            reasons.append("competition_mismatch")
        if (prediction.sport or "soccer").casefold() != (candidate.sport or "soccer").casefold():
            reasons.append("sport_mismatch")
        if prediction.settlement_scope != candidate.settlement_scope:
            reasons.append("settlement_scope_mismatch")
        reasons.extend(blockers)
        if candidate.scheduled_start is None:
            reasons.append("sportsbook_kickoff_missing")
        elif prediction.scheduled_start is None or (
            abs((prediction.scheduled_start - candidate.scheduled_start).total_seconds())
            > tolerance_minutes * 60
        ):
            reasons.append("kickoff_outside_tolerance")
        if not reasons:
            compatible.append(candidate)
            continue

        scored.append((score, candidate, breakdown, blockers, reasons))

    if len(compatible) == 1:
        candidate = compatible[0]
        return candidate, EventMatchAudit(
            **base,
            matched=True,
            match_confidence=MatchConfidence.APPROVED_ALIAS,
            weighted_score=Decimal("100"),
            sportsbook_event_id=candidate.provider_event_id,
            sportsbook_title=candidate.title,
            sportsbook_competition=candidate.competition or candidate.category,
            sportsbook_home_team=candidate.home_team,
            sportsbook_away_team=candidate.away_team,
            sportsbook_kickoff_time_utc=candidate.scheduled_start,
        )
    if len(compatible) > 1:
        candidate = compatible[0]
        return None, EventMatchAudit(
            **base,
            match_confidence=MatchConfidence.MANUAL_REVIEW,
            sportsbook_event_id=candidate.provider_event_id,
            sportsbook_title=candidate.title,
            sportsbook_competition=candidate.competition or candidate.category,
            sportsbook_home_team=candidate.home_team,
            sportsbook_away_team=candidate.away_team,
            sportsbook_kickoff_time_utc=candidate.scheduled_start,
            rejection_reasons=["ambiguous_orientation_match"],
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return None, EventMatchAudit(
            **base,
            rejection_reasons=["no_compatible_sportsbook_event"],
        )
    best_score, closest, breakdown, blockers, closest_reasons = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else None
    fuzzy_name_reasons = {"participant_pair_mismatch", "competition_mismatch"}
    fuzzy_eligible = (
        best_score >= fuzzy_min_score
        and not blockers
        and set(closest_reasons).issubset(fuzzy_name_reasons)
    )
    ambiguous = (
        fuzzy_eligible and runner_up is not None and best_score - runner_up < ambiguity_margin
    )
    if fuzzy_eligible and not ambiguous:
        return closest, EventMatchAudit(
            **base,
            matched=True,
            match_confidence=MatchConfidence.APPROVED_ALIAS,
            weighted_score=best_score,
            runner_up_score=runner_up,
            score_breakdown=breakdown,
            sportsbook_event_id=closest.provider_event_id,
            sportsbook_title=closest.title,
            sportsbook_competition=closest.competition or closest.category,
            sportsbook_home_team=closest.home_team,
            sportsbook_away_team=closest.away_team,
            sportsbook_kickoff_time_utc=closest.scheduled_start,
        )
    if fuzzy_eligible:
        closest_reasons = [
            "ambiguous_candidate_margin",
            *closest_reasons,
        ]
    return None, EventMatchAudit(
        **base,
        match_confidence=(
            MatchConfidence.MANUAL_REVIEW if fuzzy_eligible else MatchConfidence.REJECTED
        ),
        weighted_score=best_score,
        runner_up_score=runner_up,
        score_breakdown=breakdown,
        sportsbook_event_id=closest.provider_event_id,
        sportsbook_title=closest.title,
        sportsbook_competition=closest.competition or closest.category,
        sportsbook_home_team=closest.home_team,
        sportsbook_away_team=closest.away_team,
        sportsbook_kickoff_time_utc=closest.scheduled_start,
        rejection_reasons=list(dict.fromkeys([*blockers, *closest_reasons])),
    )


_FORBIDDEN_MARKET_TERMS = (
    "qualify",
    "advance",
    "extra time",
    "penalt",
    "first half",
    "double chance",
    "draw no bet",
    "handicap",
    "total",
    "over ",
    "under ",
)


def _selection_from_text(text: str, event: ProviderEvent) -> Selection | None:
    normalized = normalize_text(text)
    if any(term in normalized for term in _FORBIDDEN_MARKET_TERMS):
        return None
    if "draw" in normalized or " tie" in f" {normalized}":
        return Selection.DRAW
    home = normalize_team(event.home_team or "")
    away = normalize_team(event.away_team or "")
    if home and home in normalize_team(normalized):
        return Selection.HOME
    if away and away in normalize_team(normalized):
        return Selection.AWAY
    return None


def prediction_selections(
    market: ProviderMarket, event: ProviderEvent
) -> list[tuple[Selection, str]]:
    if market.status.casefold() not in {"open", "active"} or not market.order_book_enabled:
        return []
    title_selection = _selection_from_text(market.title, event)
    results: list[tuple[Selection, str]] = []
    for outcome in market.outcomes:
        outcome_selection = _selection_from_text(outcome.name, event)
        selection = outcome_selection or (
            title_selection if outcome.name.casefold() in {"yes", "1"} else None
        )
        if selection is None or outcome.name.casefold() in {"no"}:
            continue
        lookup_id = (
            outcome.token_id
            if market.provider == Provider.POLYMARKET
            else market.provider_market_id
        )
        if lookup_id:
            results.append((selection, lookup_id))
    return list(dict.fromkeys(results))


def _bookmakers(mapped: list[Bookmaker], enabled: list[str], now: datetime) -> list[Bookmaker]:
    by_id = {item.canonical_id: item for item in mapped}
    return [
        by_id.get(canonical_id)
        or Bookmaker(
            canonical_id=canonical_id,
            display_name=CANONICAL_BOOKMAKERS.get(canonical_id, canonical_id),
            enabled=True,
            availability_status=AvailabilityStatus.TEMPORARILY_MISSING,
            last_verified_at=now,
        )
        for canonical_id in enabled
    ]


async def collect_live_snapshot(
    prediction_connectors: list[PredictionMarketConnector],
    sports_connector: SportsOddsConnector,
    settings: Settings,
    now: datetime,
    start: datetime,
    end: datetime,
    approved_event_mappings: dict[tuple[str, str], str] | None = None,
) -> LiveScanSnapshot:
    scan_started = perf_counter()
    discovery_started = perf_counter()
    logger.info(
        "scan.discovery.start window_start=%s window_end=%s prediction_providers=%d",
        start.isoformat(),
        end.isoformat(),
        len(prediction_connectors),
    )
    prediction_tasks = [
        asyncio.create_task(connector.discover_events(start, end))
        for connector in prediction_connectors
    ]
    mapped_bookmakers, _unknown = await sports_connector.list_bookmakers()
    sports_connector.use_provider_bookmaker_ids(mapped_bookmakers, settings.enabled_bookmakers)
    sportsbook_events_task = asyncio.create_task(sports_connector.discover_events(start, end))
    discovery_results, sportsbook_events = await asyncio.gather(
        asyncio.gather(*prediction_tasks, return_exceptions=True),
        sportsbook_events_task,
    )
    prediction_event_batches = [
        (connector, result if isinstance(result, list) else [])
        for connector, result in zip(prediction_connectors, discovery_results, strict=True)
    ]
    logger.info(
        "scan.discovery.complete sportsbook_events=%d prediction_events=%d duration_seconds=%.3f",
        len(sportsbook_events),
        sum(len(events) for _connector, events in prediction_event_batches),
        perf_counter() - discovery_started,
    )

    bookmakers = _bookmakers(mapped_bookmakers, settings.enabled_bookmakers, now)
    canonical_events: dict[UUID, CanonicalEvent] = {}
    sportsbooks: list[SportsbookQuote] = []
    event_matches: list[EventMatchAudit] = []

    matched_sportsbook_event_ids: set[str] = set()
    sportsbook_match_confidence: dict[str, MatchConfidence] = {}
    matched_by_prediction: dict[tuple[Provider, str], ProviderEvent] = {}
    oriented_prediction_by_id: dict[tuple[Provider, str], ProviderEvent] = {}
    sportsbook_index = SportsbookEventIndex(
        sportsbook_events,
        settings.event_match_kickoff_tolerance_minutes,
    )
    matching_started = perf_counter()
    for _connector, prediction_events in prediction_event_batches:
        for raw_event in prediction_events:
            logger.info(
                "match.event.start provider=%s event_id=%s title=%r",
                raw_event.provider.value,
                raw_event.provider_event_id,
                raw_event.title,
            )
            prediction_event = enrich_prediction_event(raw_event)
            if prediction_event is None:
                logger.info(
                    "match.event.rejected provider=%s event_id=%s reason=matchup_unparseable",
                    raw_event.provider.value,
                    raw_event.provider_event_id,
                )
                event_matches.append(
                    EventMatchAudit(
                        provider=raw_event.provider,
                        provider_event_id=raw_event.provider_event_id,
                        title=raw_event.title,
                        competition=raw_event.competition or raw_event.category,
                        participant_one=raw_event.participant_one,
                        participant_two=raw_event.participant_two,
                        normalized_participant_one=normalize_team(raw_event.participant_one or ""),
                        normalized_participant_two=normalize_team(raw_event.participant_two or ""),
                        orientation_known=raw_event.orientation_known,
                        extraction_source=raw_event.extraction_source,
                        normalized_competition=normalize_competition(
                            raw_event.competition or raw_event.category or ""
                        ),
                        kickoff_time_utc=raw_event.scheduled_start,
                        rejection_reasons=["matchup_unparseable"],
                    )
                )
                continue
            mapped_event_id = (approved_event_mappings or {}).get(
                (
                    prediction_event.provider.value,
                    prediction_event.provider_event_id,
                )
            )
            mapped_event = (
                sportsbook_index.by_id.get(mapped_event_id) if mapped_event_id is not None else None
            )
            mapped_events = [mapped_event] if mapped_event is not None else []
            match_candidates = mapped_events or sportsbook_index.candidates(
                prediction_event,
                settings.event_match_kickoff_tolerance_minutes,
            )
            logger.info(
                "match.event.candidates provider=%s event_id=%s source=%s count=%d",
                prediction_event.provider.value,
                prediction_event.provider_event_id,
                "stored_mapping" if mapped_events else "event_index",
                len(match_candidates),
            )
            matched, audit = audit_prediction_event(
                prediction_event,
                match_candidates,
                settings.event_match_kickoff_tolerance_minutes,
                (Decimal("70") if mapped_events else settings.event_match_fuzzy_min_score),
                settings.event_match_ambiguity_margin,
            )
            if (
                mapped_events
                and matched is None
                and audit.match_confidence == MatchConfidence.MANUAL_REVIEW
                and "ambiguous_candidate_margin" not in audit.rejection_reasons
                and audit.score_breakdown.get("participant_one", Decimal()) >= Decimal("70")
                and audit.score_breakdown.get("participant_two", Decimal()) >= Decimal("70")
                and audit.score_breakdown.get("competition", Decimal()) >= Decimal("70")
            ):
                matched = mapped_events[0]
                audit = audit.model_copy(
                    update={
                        "matched": True,
                        "match_confidence": MatchConfidence.APPROVED_ALIAS,
                        "rejection_reasons": [],
                    }
                )
            event_matches.append(audit)
            logger.info(
                "match.event.result provider=%s event_id=%s matched=%s confidence=%s "
                "sportsbook_event_id=%s score=%s reasons=%s",
                prediction_event.provider.value,
                prediction_event.provider_event_id,
                audit.matched,
                audit.match_confidence.value,
                audit.sportsbook_event_id,
                audit.weighted_score,
                ",".join(audit.rejection_reasons) or "none",
            )
            if matched is not None:
                matched_sportsbook_event_ids.add(matched.provider_event_id)
                if (
                    sportsbook_match_confidence.get(matched.provider_event_id)
                    != MatchConfidence.EXACT
                ):
                    sportsbook_match_confidence[matched.provider_event_id] = audit.match_confidence
                matched_by_prediction[
                    (prediction_event.provider, prediction_event.provider_event_id)
                ] = matched
                oriented_prediction_by_id[
                    (prediction_event.provider, prediction_event.provider_event_id)
                ] = prediction_event.model_copy(
                    update={
                        "home_team": matched.home_team,
                        "away_team": matched.away_team,
                        "orientation_known": True,
                    }
                )

    for event in sportsbook_events:
        event_matches.append(
            EventMatchAudit(
                provider=Provider.ODDSPAPI,
                provider_event_id=event.provider_event_id,
                title=event.title,
                competition=event.competition or event.category,
                home_team=event.home_team,
                away_team=event.away_team,
                normalized_competition=normalize_competition(
                    event.competition or event.category or ""
                ),
                normalized_home_team=normalize_team(event.home_team or ""),
                normalized_away_team=normalize_team(event.away_team or ""),
                kickoff_time_utc=event.scheduled_start,
                matched=event.provider_event_id in matched_sportsbook_event_ids,
                match_confidence=(
                    sportsbook_match_confidence.get(
                        event.provider_event_id, MatchConfidence.REJECTED
                    )
                ),
                sportsbook_event_id=event.provider_event_id,
                sportsbook_title=event.title,
                sportsbook_competition=event.competition or event.category,
                sportsbook_home_team=event.home_team,
                sportsbook_away_team=event.away_team,
                sportsbook_kickoff_time_utc=event.scheduled_start,
                rejection_reasons=(
                    []
                    if event.provider_event_id in matched_sportsbook_event_ids
                    else ["no_prediction_market_match"]
                ),
            )
        )

    logger.info(
        "match.batch.complete matched_prediction_events=%d unmatched_audits=%d "
        "duration_seconds=%.3f",
        len(matched_by_prediction),
        sum(not audit.matched for audit in event_matches),
        perf_counter() - matching_started,
    )

    pricing_started = perf_counter()
    request_semaphore = asyncio.Semaphore(settings.provider_request_concurrency)
    matched_sportsbook_events = [
        event
        for event in sportsbook_events
        if event.provider_event_id in matched_sportsbook_event_ids
        and event.home_team
        and event.away_team
        and event.scheduled_start
    ]
    for event in matched_sportsbook_events:
        event_id = canonical_event_id(event)
        canonical_events[event_id] = CanonicalEvent(
            id=event_id,
            sport=event.sport or "soccer",
            competition=event.competition or event.category or "",
            home_team=event.home_team or "",
            away_team=event.away_team or "",
            kickoff_time_utc=event.scheduled_start,
        )

    async def fetch_event_odds(
        event: ProviderEvent,
    ) -> tuple[ProviderEvent, list[ProviderSportsbookQuote]]:
        try:
            logger.info(
                "pricing.sportsbook.start event_id=%s title=%r",
                event.provider_event_id,
                event.title,
            )
            async with request_semaphore:
                event_odds = await sports_connector.get_event_odds(event.provider_event_id)
            logger.info(
                "pricing.sportsbook.complete event_id=%s quotes=%d",
                event.provider_event_id,
                len(event_odds),
            )
        except Exception as exc:
            status_code, retry_after = _http_error_context(exc)
            logger.warning(
                "pricing.sportsbook.failed event_id=%s error_type=%s status_code=%s retry_after=%s",
                event.provider_event_id,
                type(exc).__name__,
                status_code,
                retry_after,
            )
            return event, []
        return event, event_odds

    predictions: list[PredictionMarketQuote] = []
    order_books: dict[str, OrderBookSnapshot] = {}
    prediction_jobs: list[tuple[PredictionMarketConnector, ProviderEvent, ProviderEvent]] = []
    for connector, prediction_events in prediction_event_batches:
        for raw_event in prediction_events:
            prediction_event = enrich_prediction_event(raw_event)
            if prediction_event is None:
                continue
            prediction_key = (
                prediction_event.provider,
                prediction_event.provider_event_id,
            )
            matched = matched_by_prediction.get(prediction_key)
            if matched is None or matched.scheduled_start is None:
                continue
            prediction_event = oriented_prediction_by_id[prediction_key]
            prediction_jobs.append((connector, prediction_event, matched))

    async def discover_prediction_markets(
        connector: PredictionMarketConnector,
        prediction_event: ProviderEvent,
        matched: ProviderEvent,
    ) -> tuple[
        PredictionMarketConnector,
        ProviderEvent,
        ProviderEvent,
        list[ProviderMarket],
    ]:
        logger.info(
            "pricing.prediction.markets.start provider=%s event_id=%s title=%r",
            prediction_event.provider.value,
            prediction_event.provider_event_id,
            prediction_event.title,
        )
        try:
            async with request_semaphore:
                markets = await connector.discover_markets(prediction_event.provider_event_id)
        except Exception as exc:
            status_code, retry_after = _http_error_context(exc)
            logger.warning(
                "pricing.prediction.markets.failed provider=%s event_id=%s "
                "error_type=%s status_code=%s retry_after=%s",
                prediction_event.provider.value,
                prediction_event.provider_event_id,
                type(exc).__name__,
                status_code,
                retry_after,
            )
            markets = []
        logger.info(
            "pricing.prediction.markets.complete provider=%s event_id=%s markets=%d",
            prediction_event.provider.value,
            prediction_event.provider_event_id,
            len(markets),
        )
        return connector, prediction_event, matched, markets

    market_results = await asyncio.gather(
        *(discover_prediction_markets(*job) for job in prediction_jobs)
    )
    priceable_sportsbook_event_ids = {
        matched.provider_event_id
        for _connector, prediction_event, matched, markets in market_results
        if any(prediction_selections(market, prediction_event) for market in markets)
    }
    priceable_sportsbook_events = [
        event
        for event in matched_sportsbook_events
        if event.provider_event_id in priceable_sportsbook_event_ids
    ]
    logger.info(
        "pricing.sportsbook.filtered matched_events=%d priceable_events=%d skipped=%d",
        len(matched_sportsbook_events),
        len(priceable_sportsbook_events),
        len(matched_sportsbook_events) - len(priceable_sportsbook_events),
    )
    odds_results = await asyncio.gather(
        *(fetch_event_odds(event) for event in priceable_sportsbook_events)
    )
    for event, event_odds in odds_results:
        event_id = canonical_event_id(event)
        for quote in event_odds:
            if quote.bookmaker_id not in settings.enabled_bookmakers:
                continue
            sportsbooks.append(
                SportsbookQuote(
                    provider_event_id=event.provider_event_id,
                    canonical_event_id=event_id,
                    bookmaker_id=quote.bookmaker_id,
                    bookmaker_display_name=CANONICAL_BOOKMAKERS.get(
                        quote.bookmaker_id, quote.bookmaker_id
                    ),
                    sport=event.sport or "soccer",
                    competition=event.competition or event.category or "",
                    home_team=event.home_team or "",
                    away_team=event.away_team or "",
                    kickoff_time_utc=event.scheduled_start,
                    market_type=MarketType(quote.market_type),
                    selection=Selection(quote.selection),
                    period=Period(quote.period),
                    decimal_odds=quote.decimal_odds,
                    implied_probability=decimal_odds_to_implied_probability(quote.decimal_odds),
                    source_timestamp=quote.changed_at,
                    received_timestamp=now,
                    market_status=(
                        MarketStatus.OPEN
                        if quote.active and quote.market_active
                        else MarketStatus.CLOSED
                    ),
                    direct_url=quote.direct_url,
                )
            )
    trade_tasks: dict[
        tuple[Provider, str],
        asyncio.Task[list[ProviderTrade]],
    ] = {}

    async def fetch_trades(
        connector: PredictionMarketConnector,
        market_id: str,
    ) -> list[ProviderTrade]:
        async with request_semaphore:
            return await connector.get_recent_trades(
                market_id,
                now - timedelta(hours=24),
            )

    async def price_selection(
        connector: PredictionMarketConnector,
        prediction_event: ProviderEvent,
        matched: ProviderEvent,
        market: ProviderMarket,
        selection: Selection,
        lookup_id: str,
    ) -> tuple[PredictionMarketQuote | None, OrderBookSnapshot | None]:
        trade_task: asyncio.Task[list[ProviderTrade]] | None = None
        if market.provider == Provider.KALSHI:
            trade_key = (market.provider, market.provider_market_id)
            trade_task = trade_tasks.get(trade_key)
            if trade_task is None:
                trade_task = asyncio.create_task(fetch_trades(connector, market.provider_market_id))
                trade_tasks[trade_key] = trade_task
        try:
            logger.info(
                "pricing.orderbook.start provider=%s market_id=%s selection=%s",
                market.provider.value,
                market.provider_market_id,
                selection.value,
            )
            async with request_semaphore:
                book = await connector.get_order_book(lookup_id)
        except Exception as exc:
            status_code, retry_after = _http_error_context(exc)
            logger.warning(
                "pricing.orderbook.failed provider=%s market_id=%s error_type=%s "
                "status_code=%s retry_after=%s",
                market.provider.value,
                market.provider_market_id,
                type(exc).__name__,
                status_code,
                retry_after,
            )
            return None, None
        synthetic_market_id = (
            market.provider_market_id
            if market.provider == Provider.KALSHI
            else f"{market.provider_market_id}:{lookup_id}"
        )
        volume = market.trailing_24h_volume_usd
        volume_source = VolumeSource.PROVIDER_REPORTED if volume is not None else None
        if trade_task is not None:
            try:
                trades = await trade_task
            except Exception:
                return None, None
            volume = sum((trade.price * trade.quantity for trade in trades), Decimal())
            volume_source = VolumeSource.CALCULATED_FROM_TRADES
        snapshot = normalize_order_book(
            book,
            selection,
            now,
            volume,
            volume_source,
        ).model_copy(update={"provider_market_id": synthetic_market_id})
        if snapshot.best_bid is None or snapshot.best_ask is None:
            return None, None
        quote = PredictionMarketQuote(
            provider=market.provider,
            provider_event_id=prediction_event.provider_event_id,
            provider_market_id=synthetic_market_id,
            canonical_event_id=canonical_event_id(matched),
            sport=matched.sport or "soccer",
            competition=matched.competition or matched.category or "",
            home_team=matched.home_team or "",
            away_team=matched.away_team or "",
            kickoff_time_utc=matched.scheduled_start,
            selection=selection,
            best_bid_probability=snapshot.best_bid,
            best_ask_probability=snapshot.best_ask,
            best_bid_size=max(
                (level.quantity for level in snapshot.bids if level.price == snapshot.best_bid),
                default=Decimal(),
            ),
            best_ask_size=min(
                (level.quantity for level in snapshot.asks if level.price == snapshot.best_ask),
                default=Decimal(),
            ),
            source_timestamp=snapshot.source_timestamp,
            received_timestamp=now,
            direct_url=None,
        )
        return quote, snapshot

    selection_jobs = [
        (
            connector,
            prediction_event,
            matched,
            market,
            selection,
            lookup_id,
        )
        for connector, prediction_event, matched, markets in market_results
        for market in markets
        for selection, lookup_id in prediction_selections(market, prediction_event)
    ]
    selection_results = await asyncio.gather(*(price_selection(*job) for job in selection_jobs))
    if trade_tasks:
        await asyncio.gather(*trade_tasks.values(), return_exceptions=True)
    logger.info(
        "scan.pricing.complete sportsbook_quotes=%d prediction_quotes=%d duration_seconds=%.3f",
        len(sportsbooks),
        sum(quote is not None for quote, _snapshot in selection_results),
        perf_counter() - pricing_started,
    )
    qualification_started = perf_counter()
    for prediction_quote, snapshot in selection_results:
        if prediction_quote is None or snapshot is None:
            continue
        predictions.append(prediction_quote)
        order_books[prediction_quote.provider_market_id] = snapshot
    candidates = evaluate_candidates(
        predictions, sportsbooks, bookmakers, settings, now, order_books
    )
    logger.info(
        "scan.qualification.complete predictions=%d sportsbook_quotes=%d "
        "candidates=%d opportunities=%d duration_seconds=%.3f total_seconds=%.3f",
        len(predictions),
        len(sportsbooks),
        len(candidates),
        sum(candidate.accepted for candidate in candidates),
        perf_counter() - qualification_started,
        perf_counter() - scan_started,
    )
    return LiveScanSnapshot(
        events=list(canonical_events.values()),
        predictions=predictions,
        sportsbooks=sportsbooks,
        bookmakers=bookmakers,
        order_books=order_books,
        candidates=candidates,
        opportunities=opportunities_from_candidates(candidates, settings),
        event_matches=event_matches,
    )

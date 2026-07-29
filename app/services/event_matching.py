from dataclasses import dataclass

from app.domain.enums import MatchConfidence
from app.domain.models import PredictionMarketQuote, SportsbookQuote
from app.services.normalization import normalize_competition, normalize_team


@dataclass(frozen=True)
class MatchResult:
    confidence: MatchConfidence
    reason: str | None = None


def match_event(
    prediction: PredictionMarketQuote,
    sportsbook: SportsbookQuote,
    kickoff_tolerance_seconds: int = 600,
) -> MatchResult:
    if prediction.sport != sportsbook.sport:
        return MatchResult(MatchConfidence.REJECTED, "sport mismatch")
    if normalize_competition(prediction.competition) != normalize_competition(
        sportsbook.competition
    ):
        return MatchResult(MatchConfidence.REJECTED, "competition mismatch")
    if normalize_team(prediction.home_team) != normalize_team(sportsbook.home_team):
        return MatchResult(MatchConfidence.REJECTED, "home team mismatch")
    if normalize_team(prediction.away_team) != normalize_team(sportsbook.away_team):
        return MatchResult(MatchConfidence.REJECTED, "away team mismatch")
    delta = abs((prediction.kickoff_time_utc - sportsbook.kickoff_time_utc).total_seconds())
    if delta > kickoff_tolerance_seconds:
        return MatchResult(MatchConfidence.REJECTED, "kickoff mismatch")
    exact = (
        prediction.competition.casefold() == sportsbook.competition.casefold()
        and prediction.home_team.casefold() == sportsbook.home_team.casefold()
        and prediction.away_team.casefold() == sportsbook.away_team.casefold()
    )
    return MatchResult(MatchConfidence.EXACT if exact else MatchConfidence.APPROVED_ALIAS)

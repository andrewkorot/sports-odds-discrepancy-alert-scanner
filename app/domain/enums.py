from enum import StrEnum


class Provider(StrEnum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"
    ODDSPAPI = "oddspapi"


class Selection(StrEnum):
    HOME = "home"
    DRAW = "draw"
    AWAY = "away"


class MarketType(StrEnum):
    MATCH_WINNER = "match_winner"


class Period(StrEnum):
    REGULATION = "regulation"


class MarketStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    LIVE = "live"


class MatchConfidence(StrEnum):
    EXACT = "exact"
    APPROVED_ALIAS = "approved_alias"
    MANUAL_REVIEW = "manual_review"
    REJECTED = "rejected"


class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNVERIFIED = "unverified"
    TEMPORARILY_MISSING = "temporarily_missing"

from enum import StrEnum


class Provider(StrEnum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"
    ODDSPAPI = "oddspapi"


class Selection(StrEnum):
    HOME = "home"
    DRAW = "draw"
    AWAY = "away"
    OVER = "over"
    UNDER = "under"
    YES = "yes"
    NO = "no"


class MarketType(StrEnum):
    MONEYLINE = "moneyline"
    TOTAL = "total"
    SPREAD = "spread"
    BTTS = "btts"


class Period(StrEnum):
    REGULATION = "regulation"
    FIRST_HALF = "first_half"


class VolumeSource(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    CALCULATED_FROM_TRADES = "calculated_from_trades"


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

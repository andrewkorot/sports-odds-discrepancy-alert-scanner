from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("frozen time must be timezone-aware")
        self._value = value.astimezone(UTC)

    def now(self) -> datetime:
        return self._value

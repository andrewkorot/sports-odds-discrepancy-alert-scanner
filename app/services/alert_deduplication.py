from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from redis.asyncio import Redis

from app.domain.models import Opportunity


def deduplication_key(opportunity: Opportunity) -> str:
    return ":".join(
        [
            str(opportunity.canonical_event_id),
            opportunity.market_type,
            opportunity.selection,
            opportunity.prediction_market_provider,
            opportunity.prediction_market_id,
            opportunity.bookmaker_id,
            str(opportunity.line or ""),
            str(opportunity.participant or ""),
        ]
    )


@dataclass
class AlertState:
    sent_at: datetime
    edge: Decimal
    active: bool = True


class MemoryAlertDeduplicator:
    """Deterministic stand-in for the Redis implementation used by tests/mock mode."""

    def __init__(self) -> None:
        self._states: dict[str, AlertState] = {}

    async def should_alert(
        self,
        opportunity: Opportunity,
        cooldown: timedelta,
        edge_increase: Decimal,
    ) -> bool:
        key = deduplication_key(opportunity)
        previous = self._states.get(key)
        allowed = (
            previous is None
            or not previous.active
            or opportunity.detected_at - previous.sent_at >= cooldown
            or opportunity.edge_percentage_points - previous.edge >= edge_increase
        )
        if allowed:
            self._states[key] = AlertState(
                opportunity.detected_at, opportunity.edge_percentage_points
            )
        return allowed

    async def mark_disappeared(self, key: str) -> None:
        if key in self._states:
            self._states[key].active = False

    async def sync_active(self, opportunities: Sequence[Opportunity]) -> None:
        active = {deduplication_key(item) for item in opportunities}
        for key, value in self._states.items():
            if key not in active:
                value.active = False

    async def aclose(self) -> None:
        return None


class RedisAlertDeduplicator:
    """Redis-backed alert state shared across processes and restarts."""

    def __init__(self, redis: Redis, namespace: str = "scanner:alerts") -> None:
        self._redis = redis
        self._namespace = namespace
        self._active_set = f"{namespace}:active"

    def _key(self, opportunity: Opportunity) -> str:
        return f"{self._namespace}:state:{deduplication_key(opportunity)}"

    async def should_alert(
        self,
        opportunity: Opportunity,
        cooldown: timedelta,
        edge_increase: Decimal,
    ) -> bool:
        redis_key = self._key(opportunity)
        raw = await self._redis.get(redis_key)
        allowed = raw is None
        if raw is not None:
            parsed = json.loads(raw)
            previous_time = datetime.fromisoformat(str(parsed["sent_at"]))
            previous_edge = Decimal(str(parsed["edge"]))
            allowed = (
                not bool(parsed.get("active", True))
                or opportunity.detected_at - previous_time >= cooldown
                or opportunity.edge_percentage_points - previous_edge >= edge_increase
            )
        if allowed:
            await self._redis.set(
                redis_key,
                json.dumps(
                    {
                        "sent_at": opportunity.detected_at.isoformat(),
                        "edge": str(opportunity.edge_percentage_points),
                        "active": True,
                    }
                ),
                ex=max(int(cooldown.total_seconds()) * 8, 86400),
            )
        await self._redis.sadd(  # type: ignore[misc]
            self._active_set, deduplication_key(opportunity)
        )
        return allowed

    async def sync_active(self, opportunities: Sequence[Opportunity]) -> None:
        current = {deduplication_key(item) for item in opportunities}
        previous_raw = await self._redis.smembers(self._active_set)  # type: ignore[misc]
        previous = {
            item.decode() if isinstance(item, bytes) else str(item) for item in previous_raw
        }
        for key in previous - current:
            redis_key = f"{self._namespace}:state:{key}"
            raw = await self._redis.get(redis_key)
            if raw:
                parsed = json.loads(raw)
                parsed["active"] = False
                ttl = await self._redis.ttl(redis_key)
                await self._redis.set(redis_key, json.dumps(parsed), ex=max(ttl, 60))
        if previous:
            await self._redis.delete(self._active_set)
        if current:
            await self._redis.sadd(self._active_set, *current)  # type: ignore[misc]

    async def aclose(self) -> None:
        await self._redis.aclose()

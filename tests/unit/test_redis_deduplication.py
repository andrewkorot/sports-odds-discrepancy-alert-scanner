from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from app.services.alert_deduplication import RedisAlertDeduplicator
from tests.unit.test_alerts import opportunity


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, **_: object) -> bool:
        self.values[key] = value
        return True

    async def sadd(self, key: str, *values: str) -> int:
        target = self.sets.setdefault(key, set())
        before = len(target)
        target.update(values)
        return len(target) - before

    async def smembers(self, key: str) -> set[str]:
        return self.sets.get(key, set()).copy()

    async def ttl(self, key: str) -> int:
        return 900

    async def delete(self, key: str) -> int:
        self.sets.pop(key, None)
        return 1

    async def aclose(self) -> None:
        return None


async def test_redis_dedup_suppresses_then_realerts_after_reappearance() -> None:
    item, _, _ = opportunity()
    redis = FakeRedis()
    dedup = RedisAlertDeduplicator(cast(Any, redis))

    cooldown = timedelta(minutes=10)
    assert await dedup.should_alert(item, cooldown, item.configured_threshold)
    assert not await dedup.should_alert(item, cooldown, item.configured_threshold)
    await dedup.sync_active([])
    assert await dedup.should_alert(item, cooldown, item.configured_threshold)

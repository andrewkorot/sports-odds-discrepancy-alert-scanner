from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import Provider
from app.providers.records import (
    ProviderBookLevel,
    ProviderEvent,
    ProviderHealthRecord,
    ProviderMarket,
    ProviderOrderBook,
    ProviderOutcome,
    ProviderTrade,
)


def _json_list(value: object) -> list[str]:
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError("expected JSON array")
        return [str(item) for item in parsed]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise ValueError("expected array")


class GammaMarketPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    conditionId: str
    question: str
    active: bool = True
    closed: bool = False
    enableOrderBook: bool = False
    clobTokenIds: list[str]
    outcomes: list[str]
    endDate: datetime | None = None
    volume24hr: Decimal | None = None

    @field_validator("clobTokenIds", "outcomes", mode="before")
    @classmethod
    def parse_arrays(cls, value: object) -> list[str]:
        return _json_list(value)


class GammaEventPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    title: str
    active: bool = True
    closed: bool = False
    startDate: datetime | None = None
    endDate: datetime | None = None
    markets: list[GammaMarketPayload] = Field(default_factory=list)
    tags: list[dict[str, Any]] = Field(default_factory=list)


class DataTradePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    conditionId: str
    asset: str
    size: Decimal
    price: Decimal
    timestamp: int
    transactionHash: str


class PolymarketConnector:
    """Public, read-only Gamma/CLOB/Data connector with no wallet or API credentials."""

    def __init__(
        self,
        gamma_base_url: str,
        clob_base_url: str,
        data_base_url: str,
        client: httpx.AsyncClient | None = None,
        mode: str = "live",
    ) -> None:
        self._gamma = gamma_base_url.rstrip("/")
        self._clob = clob_base_url.rstrip("/")
        self._data = data_base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=15)
        self._owns_client = client is None
        self._health = ProviderHealthRecord(
            provider=Provider.POLYMARKET, mode=mode, enabled=True, connected=False
        )

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        attempted = datetime.now(UTC)
        started = perf_counter()
        try:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
            self._health = self._health.model_copy(
                update={
                    "connected": True,
                    "last_attempt_at": attempted,
                    "last_success_at": datetime.now(UTC),
                    "consecutive_failures": 0,
                    "latency_ms": Decimal(str((perf_counter() - started) * 1000)),
                }
            )
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._health = self._health.model_copy(
                update={
                    "connected": False,
                    "last_attempt_at": attempted,
                    "last_failure_at": datetime.now(UTC),
                    "consecutive_failures": self._health.consecutive_failures + 1,
                    "latest_error_code": type(exc).__name__,
                    "sanitized_latest_error": str(exc)[:200],
                }
            )
            raise

    async def discover_events(
        self, start_time: datetime, end_time: datetime
    ) -> list[ProviderEvent]:
        cursor = ""
        records: list[ProviderEvent] = []
        while True:
            payload = cast(
                dict[str, Any],
                await self._get(
                    f"{self._gamma}/events/keyset",
                    {
                        "active": "true",
                        "closed": "false",
                        "start_time_min": start_time.isoformat(),
                        "start_time_max": end_time.isoformat(),
                        "limit": 100,
                        **({"after_cursor": cursor} if cursor else {}),
                    },
                ),
            )
            for raw in payload.get("events", []):
                event = GammaEventPayload.model_validate(raw)
                searchable = " ".join(
                    [event.title, *(str(tag.get("label", "")) for tag in event.tags)]
                ).casefold()
                if not any(term in searchable for term in ("soccer", "mls", "premier league")):
                    continue
                scheduled = event.startDate or event.endDate
                if scheduled and not start_time <= scheduled <= end_time:
                    continue
                records.append(
                    ProviderEvent(
                        provider=Provider.POLYMARKET,
                        provider_event_id=event.id,
                        title=event.title,
                        category="soccer",
                        scheduled_start=scheduled,
                        status="open" if event.active and not event.closed else "closed",
                        raw_market_ids=[market.id for market in event.markets],
                    )
                )
            cursor = str(payload.get("next_cursor") or "")
            if not cursor:
                break
        self._health = self._health.model_copy(update={"events_discovered": len(records)})
        return records

    async def discover_markets(self, event_id: str) -> list[ProviderMarket]:
        raw = cast(dict[str, Any], await self._get(f"{self._gamma}/events/{event_id}"))
        event = GammaEventPayload.model_validate(raw)
        records = [self.parse_market(event.id, market) for market in event.markets]
        self._health = self._health.model_copy(update={"markets_discovered": len(records)})
        return records

    @staticmethod
    def parse_market(event_id: str, raw: GammaMarketPayload | dict[str, Any]) -> ProviderMarket:
        market = (
            raw if isinstance(raw, GammaMarketPayload) else GammaMarketPayload.model_validate(raw)
        )
        if len(market.outcomes) != len(market.clobTokenIds):
            raise ValueError("Polymarket outcome/token relationship is incomplete")
        return ProviderMarket(
            provider=Provider.POLYMARKET,
            provider_event_id=event_id,
            provider_market_id=market.id,
            condition_id=market.conditionId,
            title=market.question,
            status="open" if market.active and not market.closed else "closed",
            order_book_enabled=market.enableOrderBook,
            outcomes=[
                ProviderOutcome(name=name, selection_id=str(index), token_id=token)
                for index, (name, token) in enumerate(
                    zip(market.outcomes, market.clobTokenIds, strict=True)
                )
            ],
            close_time=market.endDate,
            # Gamma's documented volume24hr is mapped explicitly when present.
            trailing_24h_volume_usd=market.volume24hr,
        )

    async def get_order_book(self, market_or_token_id: str) -> ProviderOrderBook:
        payload = cast(
            dict[str, Any],
            await self._get(f"{self._clob}/book", {"token_id": market_or_token_id}),
        )
        timestamp_raw = payload.get("timestamp")
        if timestamp_raw is None:
            raise ValueError("Polymarket order book timestamp is missing")
        timestamp_text = str(timestamp_raw)
        timestamp = (
            datetime.fromtimestamp(int(timestamp_text) / 1000, UTC)
            if timestamp_text.isdigit()
            else datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
        )
        result = ProviderOrderBook(
            provider=Provider.POLYMARKET,
            provider_market_id=str(payload.get("market") or ""),
            selection_id=str(payload.get("asset_id") or market_or_token_id),
            bids=sorted(
                (
                    ProviderBookLevel(
                        price=Decimal(str(item["price"])),
                        quantity=Decimal(str(item["size"])),
                    )
                    for item in payload.get("bids", [])
                ),
                key=lambda level: level.price,
                reverse=True,
            ),
            asks=sorted(
                (
                    ProviderBookLevel(
                        price=Decimal(str(item["price"])),
                        quantity=Decimal(str(item["size"])),
                    )
                    for item in payload.get("asks", [])
                ),
                key=lambda level: level.price,
            ),
            source_timestamp=timestamp,
        )
        self._health = self._health.model_copy(
            update={
                "books_updated": self._health.books_updated + 1,
                "last_order_book_timestamp": timestamp,
            }
        )
        return result

    async def get_recent_trades(
        self, market_or_token_id: str, since: datetime
    ) -> list[ProviderTrade]:
        offset = 0
        records: list[ProviderTrade] = []
        while True:
            page = cast(
                list[dict[str, Any]],
                await self._get(
                    f"{self._data}/trades",
                    {"market": market_or_token_id, "limit": 1000, "offset": offset},
                ),
            )
            parsed = [DataTradePayload.model_validate(item) for item in page]
            records.extend(
                ProviderTrade(
                    provider=Provider.POLYMARKET,
                    provider_market_id=trade.conditionId,
                    trade_id=trade.transactionHash,
                    price=trade.price,
                    quantity=trade.size,
                    executed_at=datetime.fromtimestamp(trade.timestamp, UTC),
                )
                for trade in parsed
                if datetime.fromtimestamp(trade.timestamp, UTC) > since
            )
            if len(page) < 1000:
                break
            offset += len(page)
        self._health = self._health.model_copy(update={"trades_processed": len(records)})
        return records

    async def health(self) -> ProviderHealthRecord:
        return self._health

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

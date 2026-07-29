import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.domain.enums import Selection, VolumeSource
from app.providers.kalshi.connector import KalshiConnector
from app.providers.polymarket.connector import PolymarketConnector
from app.services.provider_normalization import normalize_order_book

FIXTURES = Path(__file__).parents[1] / "fixtures"


class FakeSigner:
    def headers(self, method: str, path: str) -> dict[str, str]:
        return {"KALSHI-ACCESS-KEY": "sanitized"}


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.asyncio
async def test_kalshi_orderbook_derives_yes_ask_and_preserves_no_quantity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/markets/KXMLS/orderbook")
        return httpx.Response(200, json=fixture("kalshi_orderbook.json"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = KalshiConnector(
        "https://external-api.kalshi.com/trade-api/v2", FakeSigner(), client
    )
    book = await connector.get_order_book("KXMLS")
    assert book.bids[0].price == Decimal("0.51")
    assert book.asks[0].price == Decimal("0.52")
    assert book.asks[0].quantity == Decimal("850")
    assert book.source_no_price == Decimal("0.48")
    assert book.ask_derived
    await client.aclose()


def test_polymarket_market_preserves_outcome_token_relationship() -> None:
    raw = fixture("polymarket_market.json")
    assert isinstance(raw, dict)
    market = PolymarketConnector.parse_market("event-1", raw)
    assert market.condition_id == "0xcondition"
    assert [(item.name, item.token_id) for item in market.outcomes] == [
        ("Yes", "yes-token"),
        ("No", "no-token"),
    ]
    assert market.trailing_24h_volume_usd == Decimal("8200.50")


def test_polymarket_rejects_mismatched_outcome_tokens() -> None:
    raw = fixture("polymarket_market.json")
    assert isinstance(raw, dict)
    raw["clobTokenIds"] = '["yes-token"]'
    with pytest.raises(ValueError, match="relationship"):
        PolymarketConnector.parse_market("event-1", raw)


@pytest.mark.asyncio
async def test_polymarket_public_orderbook_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "POLY_API_KEY" not in request.headers
        return httpx.Response(200, json=fixture("polymarket_orderbook.json"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = PolymarketConnector(
        "https://gamma-api.polymarket.com",
        "https://clob.polymarket.com",
        "https://data-api.polymarket.com",
        client,
    )
    book = await connector.get_order_book("yes-token")
    assert book.selection_id == "yes-token"
    assert book.bids[0].quantity == Decimal("4100")
    assert book.asks[0].price == Decimal("0.54")
    normalized = normalize_order_book(
        book,
        Selection.YES,
        book.source_timestamp,
        Decimal("8200"),
        VolumeSource.PROVIDER_REPORTED,
    )
    assert normalized.best_ask == Decimal("0.54")
    assert normalized.asks[0].notional_usd == Decimal("1836.00")
    await client.aclose()


def test_malformed_provider_payload_is_rejected() -> None:
    with pytest.raises((ValidationError, ValueError)):
        PolymarketConnector.parse_market("event-1", {"id": "incomplete"})

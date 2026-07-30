import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.domain.enums import Selection, VolumeSource
from app.providers.kalshi.connector import KalshiConnector
from app.providers.oddspapi.connector import SportsOddsConnector
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


@pytest.mark.asyncio
async def test_oddspapi_v4_fixture_discovery_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v4/fixtures"
        assert request.url.params["apiKey"] == "secret"
        assert request.url.params["sportId"] == "10"
        assert request.url.params["statusId"] == "0"
        assert request.url.params["hasOdds"] == "true"
        assert "from" in request.url.params and "to" in request.url.params
        return httpx.Response(
            200,
            json=[
                {
                    "fixtureId": "fixture-1",
                    "participant1Name": "Inter Miami CF",
                    "participant2Name": "Atlanta United",
                    "tournamentName": "MLS",
                    "startTime": "2026-07-30T20:00:00.000Z",
                    "statusId": 0,
                    "statusName": "Pre-Game",
                    "sportName": "Soccer",
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = SportsOddsConnector("secret", "https://api.oddspapi.io/v4", ["pinnacle"], client)
    start = datetime(2026, 7, 30, 12, tzinfo=UTC)
    events = await connector.discover_events(start, start + timedelta(hours=24))
    assert events[0].provider_event_id == "fixture-1"
    assert events[0].title == "Inter Miami CF vs Atlanta United"
    assert events[0].scheduled_start == datetime(2026, 7, 30, 20, tzinfo=UTC)
    await client.aclose()


@pytest.mark.asyncio
async def test_oddspapi_v4_odds_contract_and_nested_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v4/odds"
        assert request.url.params["fixtureId"] == "fixture-1"
        return httpx.Response(
            200,
            json={
                "bookmakerOdds": {
                    "pinnacle": {
                        "markets": {
                            "101": {
                                "marketActive": True,
                                "outcomes": {
                                    "102": {
                                        "players": {
                                            "0": {
                                                "active": True,
                                                "bookmakerOutcomeId": "home",
                                                "changedAt": "2026-07-30T12:00:00.000Z",
                                                "price": 2.15,
                                                "mainLine": True,
                                            }
                                        }
                                    }
                                },
                            }
                        }
                    }
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = SportsOddsConnector("secret", "https://api.oddspapi.io/v4", ["pinnacle"], client)
    quotes = await connector.get_event_odds("fixture-1")
    assert quotes[0].market_id == 101
    assert quotes[0].provider_outcome_id == 102
    assert quotes[0].decimal_odds == Decimal("2.15")
    assert quotes[0].changed_at == datetime(2026, 7, 30, 12, tzinfo=UTC)
    await client.aclose()

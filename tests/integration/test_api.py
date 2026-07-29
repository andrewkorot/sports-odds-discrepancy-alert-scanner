from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(Settings())
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
            yield value


@pytest.mark.asyncio
async def test_health_and_seeded_end_to_end(client: AsyncClient) -> None:
    health = await client.get("/health")
    assert health.status_code == 200
    assert health.json()["application_status"] == "ok"
    opportunities = (await client.get("/opportunities")).json()
    assert len(opportunities) == 108
    pinnacle = next(
        x
        for x in opportunities
        if x["bookmaker_id"] == "pinnacle" and x["prediction_market_provider"] == "kalshi"
    )
    assert Decimal(pinnacle["sportsbook_implied_probability"]).quantize(
        Decimal("0.0001")
    ) == Decimal("0.4651")
    assert Decimal(pinnacle["edge_percentage_points"]).quantize(Decimal("0.01")) == Decimal("5.49")


@pytest.mark.asyncio
async def test_rest_filters(client: AsyncClient) -> None:
    response = await client.get(
        "/opportunities", params={"bookmaker": "pinnacle", "minimum_edge": "5"}
    )
    assert response.status_code == 200
    assert response.json()
    assert all(x["bookmaker_id"] == "pinnacle" for x in response.json())
    assert all(Decimal(x["edge_percentage_points"]) >= 5 for x in response.json())


@pytest.mark.asyncio
async def test_bookmakers_endpoint(client: AsyncClient) -> None:
    response = await client.get("/bookmakers")
    assert response.status_code == 200
    assert {x["canonical_id"] for x in response.json()} == {
        "bookmaker_eu",
        "stake",
        "cloudbet",
        "betus",
        "pinnacle",
        "coolbet",
    }


@pytest.mark.asyncio
async def test_market_type_and_candidate_filters(client: AsyncClient) -> None:
    totals = (await client.get("/opportunities", params={"market_type": "total"})).json()
    assert totals and all(item["market_type"] == "total" for item in totals)
    assert {
        "midpoint",
        "spread_cents",
        "total_depth_within_window_usd",
        "trailing_24h_volume_usd",
        "qualification_status",
    } <= set(totals[0])

    accepted = (await client.get("/market-candidates", params={"accepted": "true"})).json()
    rejected = (await client.get("/market-candidates", params={"accepted": "false"})).json()
    assert accepted and all(item["accepted"] for item in accepted)
    assert rejected and all(not item["accepted"] for item in rejected)
    mismatch = (
        await client.get("/market-candidates", params={"rejection_reason": "line_mismatch"})
    ).json()
    assert mismatch and all("line_mismatch" in item["rejection_reasons"] for item in mismatch)
    detail = await client.get(f"/market-candidates/{rejected[0]['id']}")
    assert detail.status_code == 200
    assert detail.json()["liquidity"]["overall_passed"]

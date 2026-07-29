from decimal import Decimal
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.domain.models import Bookmaker, CanonicalEvent, ConnectorHealth, Opportunity
from app.services.scanner import ScannerState

router = APIRouter()


def state(request: Request) -> ScannerState:
    return cast(ScannerState, request.app.state.scanner)


class HealthResponse(BaseModel):
    application_status: str
    database_status: str
    redis_status: str
    provider_statuses: dict[str, str]
    required_bookmaker_coverage: dict[str, str]
    last_successful_update: str | None


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    scanner = state(request)
    coverage = {b.canonical_id: b.availability_status.value for b in scanner.bookmakers}
    return HealthResponse(
        application_status="ok",
        database_status="mock" if scanner.settings.mock_mode else "configured",
        redis_status="mock" if scanner.settings.mock_mode else "configured",
        provider_statuses={"kalshi": "mock", "polymarket": "mock", "oddspapi": "mock"},
        required_bookmaker_coverage=coverage,
        last_successful_update=scanner.last_updated.isoformat() if scanner.last_updated else None,
    )


@router.get("/bookmakers", response_model=list[Bookmaker])
async def bookmakers(request: Request) -> list[Bookmaker]:
    return state(request).bookmakers


@router.get("/events", response_model=list[CanonicalEvent])
async def events(request: Request) -> list[CanonicalEvent]:
    return state(request).events


@router.get("/opportunities", response_model=list[Opportunity])
async def opportunities(
    request: Request,
    bookmaker: str | None = None,
    prediction_market: str | None = None,
    competition: str | None = None,
    minimum_edge: Annotated[Decimal | None, Query(ge=0)] = None,
    active_only: bool = True,
) -> list[Opportunity]:
    values = state(request).opportunities
    return [
        item
        for item in values
        if (bookmaker is None or item.bookmaker_id == bookmaker)
        and (
            prediction_market is None or item.prediction_market_provider.value == prediction_market
        )
        and (competition is None or item.competition.casefold() == competition.casefold())
        and (minimum_edge is None or item.edge_percentage_points >= minimum_edge)
        and (not active_only or item.active)
    ]


@router.get("/opportunities/{opportunity_id}", response_model=Opportunity)
async def opportunity(request: Request, opportunity_id: UUID) -> Opportunity:
    for item in state(request).opportunities:
        if item.id == opportunity_id:
            return item
    raise HTTPException(status_code=404, detail="Opportunity not found")


@router.get("/settings")
async def settings(request: Request) -> dict[str, object]:
    current = state(request).settings
    return {
        "app_env": current.app_env,
        "mock_mode": current.mock_mode,
        "enabled_bookmakers": current.enabled_bookmakers,
        "edge_threshold_pp": current.edge_threshold_pp,
        "max_prediction_price_age_seconds": current.max_prediction_price_age_seconds,
        "max_sportsbook_price_age_seconds": current.max_sportsbook_price_age_seconds,
        "min_kalshi_ask_size": current.min_kalshi_ask_size,
        "min_polymarket_ask_size": current.min_polymarket_ask_size,
        "min_minutes_before_kickoff": current.min_minutes_before_kickoff,
        "max_hours_before_kickoff": current.max_hours_before_kickoff,
        "alert_cooldown_minutes": current.alert_cooldown_minutes,
        "realert_edge_increase_pp": current.realert_edge_increase_pp,
        "oddspapi_poll_interval_seconds": current.oddspapi_poll_interval_seconds,
    }


@router.get("/connector-health", response_model=list[ConnectorHealth])
async def connector_health(request: Request) -> list[ConnectorHealth]:
    scanner = state(request)
    missing = [b.canonical_id for b in scanner.bookmakers if b.availability_status != "available"]
    return [
        ConnectorHealth(
            provider=provider,
            connection_status="mock_healthy",
            last_successful_request=scanner.last_updated,
            last_data_timestamp=scanner.last_updated,
            missing_required_bookmakers=missing if provider == "oddspapi" else [],
        )
        for provider in ("kalshi", "polymarket", "oddspapi")
    ]

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from app.api.routes import router
from app.core.config import Settings, get_settings
from app.services.orchestration import ScanOrchestrator
from app.services.scanner import ScannerState


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or get_settings()
    static_dir = Path(__file__).parent / "static"
    dashboard_prefix = "dashboard" if configured.dashboard_ui == "full" else "simple-dashboard"
    dashboard_html = (static_dir / f"{dashboard_prefix}.html").read_text()
    dashboard_css = (static_dir / f"{dashboard_prefix}.css").read_text()
    dashboard_js = (static_dir / f"{dashboard_prefix}.js").read_text()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scanner = ScannerState(configured)
        app.state.scanner = scanner
        orchestrator = ScanOrchestrator(configured, scanner)
        app.state.orchestrator = orchestrator
        await orchestrator.start()
        try:
            yield
        finally:
            await orchestrator.stop()

    app = FastAPI(
        title="Sports Price Discrepancy Scanner",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/", include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(dashboard_html)

    @app.get("/static/dashboard.css", include_in_schema=False)
    async def dashboard_styles() -> Response:
        return Response(dashboard_css, media_type="text/css")

    @app.get("/static/dashboard.js", include_in_schema=False)
    async def dashboard_script() -> Response:
        return Response(dashboard_js, media_type="text/javascript")

    app.include_router(router)
    return app


app = create_app()

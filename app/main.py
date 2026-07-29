from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import Settings, get_settings
from app.services.orchestration import ScanOrchestrator
from app.services.scanner import ScannerState


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or get_settings()
    static_dir = Path(__file__).parent / "static"

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
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(static_dir / "dashboard.html")

    app.include_router(router)
    return app


app = create_app()

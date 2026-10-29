"""Independent ASGI factory: uvicorn demo_v2.app:create_app --factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from .api.router import router
from .config import DemoConfig
from .health import ComponentHealth, HealthRegistry, HealthState
from .persistence.database import DemoDatabase
from .providers.contracts import Providers
from .streams import DecisionStream, TelemetryBuffer


def create_app(config: DemoConfig | None = None, *, providers: Providers | None = None) -> FastAPI:
    settings = config if config is not None else DemoConfig.from_env()
    database = DemoDatabase(settings)
    registry = HealthRegistry()
    registry.record(ComponentHealth("database", HealthState.NOT_READY, "NOT_STARTED"))
    # Presence of a provider object is not evidence that its data is healthy.
    # No network/provider loops or rule engine are started in Phase 1.
    for component in ("discovery", "market_data", "resolution", "fees", "underlying", "auth", "rule_engine"):
        registry.record(ComponentHealth(component, HealthState.NOT_READY, "NOT_IMPLEMENTED"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not database.probe():
            raise RuntimeError("DEMO database probe failed")
        registry.record(ComponentHealth("database", HealthState.READY))
        try:
            yield
        finally:
            registry.record(ComponentHealth("database", HealthState.NOT_READY, "STOPPED"))

    app = FastAPI(title="Polymarket DEMO V2", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.config = settings
    app.state.database = database
    app.state.health = registry
    app.state.providers = providers if providers is not None else Providers()
    app.state.decisions = DecisionStream(
        capacity=settings.decision_queue_capacity, max_age_seconds=settings.observation_max_age_seconds,
    )
    app.state.telemetry = TelemetryBuffer(settings.telemetry_capacity)
    app.include_router(router)

    @app.get("/health")
    async def liveness() -> dict[str, str | bool]:
        return {"status": "alive", "mode": "SIMULATION", "trading_enabled": False}

    @app.get("/ready")
    async def readiness(request: Request, response: Response) -> dict[str, str | bool]:
        snapshot = request.app.state.health.snapshot()
        if snapshot.state != HealthState.READY:
            response.status_code = 503
        return {"state": snapshot.state.value, "entries_allowed": snapshot.entries_allowed}

    return app

"""Independent ASGI factory: uvicorn demo_v2.app:create_app --factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import sqlite3

from .api.router import router, auth_router
from .auth import DemoAuth
from .config import DemoConfig
from .health import ComponentHealth, HealthRegistry, HealthState
from .persistence.database import DemoDatabase
from .persistence.repository import DemoRepository
from .providers.contracts import Providers
from .streams import DecisionStream, TelemetryBuffer
from .runtime import DemoRuntime


def create_app(config: DemoConfig | None = None, *, providers: Providers | None = None) -> FastAPI:
    settings = config if config is not None else DemoConfig.from_env()
    database = DemoDatabase(settings)
    registry = HealthRegistry()
    registry.record(ComponentHealth("database", HealthState.NOT_READY, "NOT_STARTED"))
    repository=DemoRepository(database)
    auth=DemoAuth(settings,database)
    registry.record(ComponentHealth("auth",HealthState.READY if auth.configured() else HealthState.NOT_READY,
                                    "" if auth.configured() else "DEMO_AUTH_NOT_CONFIGURED"))
    decisions=DecisionStream(capacity=settings.decision_queue_capacity,max_age_seconds=settings.observation_max_age_seconds)
    telemetry=TelemetryBuffer(settings.telemetry_capacity)
    runtime=DemoRuntime(settings,repository,registry,decisions,telemetry,providers)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await runtime.start()
            auth.initialize()
            yield
        finally:
            await runtime.close()
            registry.record(ComponentHealth("database", HealthState.NOT_READY, "STOPPED"))

    app = FastAPI(title="Polymarket DEMO V2", version="0.8.0", lifespan=lifespan, docs_url=None, redoc_url=None,
        description="Isolated simulated trading only. Cookie auth and X-Demo-CSRF on writes. Financial decimals are JSON strings.")
    app.state.config = settings
    app.state.database = database
    app.state.health = registry
    app.state.providers = providers if providers is not None else Providers()
    app.state.repository=repository
    app.state.auth=auth
    app.state.runtime=runtime
    app.state.decisions=decisions
    app.state.telemetry=telemetry
    app.include_router(auth_router)
    app.include_router(router)

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        response=await call_next(request)
        response.headers["Cache-Control"]="no-store"
        return response

    @app.exception_handler(KeyError)
    async def missing(request,exc):
        return JSONResponse({"detail":{"code":"DEMO_NOT_FOUND"}},status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request,exc):
        return JSONResponse({"detail":{"code":"DEMO_CONFLICT_OR_INVALID_VALUE"}},status_code=409)

    @app.exception_handler(RequestValidationError)
    async def validation(request,exc):
        return JSONResponse({"detail":{"code":"DEMO_VALIDATION_ERROR","errors":[
            {"loc":list(error["loc"]),"type":error["type"],"msg":error["msg"]} for error in exc.errors()]}},status_code=422)

    @app.exception_handler(sqlite3.Error)
    async def database_failure(request,exc):
        runtime.database_ok=False
        runtime.issue("database","DATABASE_UNAVAILABLE")
        return JSONResponse({"detail":{"code":"DEMO_DATABASE_UNAVAILABLE"}},status_code=503)

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

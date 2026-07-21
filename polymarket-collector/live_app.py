from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from live.config import LiveConfig
from live.router import configure, router


app = FastAPI(title="Polymarket LIVE Control Center")
app.include_router(router)


@app.on_event("startup")
async def startup() -> None:
    config = LiveConfig.from_env()
    configure(Path(config.live_db_path), config)


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/live", status_code=307)


@app.get("/health")
def public_health() -> dict[str, str]:
    try:
        config = LiveConfig.from_env()
        if config.validation_errors():
            return {"status": "degraded"}
        return {"status": "ok"}
    except Exception:
        return {"status": "degraded"}

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from live.config import LiveConfig
from live.router import configure, router, services
from live.market_discovery import refresh_btc_5m_markets


app = FastAPI(title="Polymarket LIVE Control Center")
app.include_router(router)


@app.on_event("startup")
async def startup() -> None:
    config = LiveConfig.from_env()
    configure(Path(config.live_db_path), config)
    repo = services()[1]
    if config.user_ws_enabled:
        await refresh_btc_5m_markets(repo)
        await services()[7].start(config.user_ws_url)


@app.on_event("shutdown")
async def shutdown() -> None:
    try:
        await services()[7].stop()
    except RuntimeError:
        pass

@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/live", status_code=307)


@app.get("/login")
def login_alias() -> RedirectResponse:
    return RedirectResponse("/live/login", status_code=307)


@app.get("/health")
def public_health() -> dict[str, str]:
    try:
        config = LiveConfig.from_env()
        if config.validation_errors():
            return {"status": "degraded"}
        try:
            if services()[7].health().get("status") in {"AUTH_FAILED", "ERROR", "STALE"}:
                return {"status": "degraded"}
        except RuntimeError:
            pass
        return {"status": "ok"}
    except Exception:
        return {"status": "degraded"}

from __future__ import annotations

from pathlib import Path
import asyncio

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover
    truststore = None

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from live.config import LiveConfig
from live.router import configure, router, services
from live.market_discovery import refresh_btc_5m_markets


app = FastAPI(title="Polymarket LIVE Control Center")
app.include_router(router)
_discovery_task: asyncio.Task[None] | None = None


async def market_discovery_loop(config: LiveConfig) -> None:
    while True:
        try:
            await refresh_btc_5m_markets(services()[1])
        except Exception as exc:
            services()[1].audit(
                "market_discovery", "market_discovery_refresh", "error",
                f"{type(exc).__name__}: {exc}"[:500],
            )
        await asyncio.sleep(max(1, config.market_discovery_interval_seconds))


@app.on_event("startup")
async def startup() -> None:
    global _discovery_task
    config = LiveConfig.from_env()
    configure(Path(config.live_db_path), config)
    await refresh_btc_5m_markets(services()[1])
    _discovery_task = asyncio.create_task(
        market_discovery_loop(config), name="live-market-discovery"
    )
    if config.market_ws_enabled:
        await services()[6].start(config.market_ws_url)
    if config.user_ws_enabled:
        await services()[7].start(config.user_ws_url)


@app.on_event("shutdown")
async def shutdown() -> None:
    global _discovery_task
    if _discovery_task is not None:
        _discovery_task.cancel()
        try:
            await _discovery_task
        except asyncio.CancelledError:
            pass
        _discovery_task = None
    try:
        await services()[6].stop()
    except RuntimeError:
        pass
    try:
        await services()[7].stop()
    except RuntimeError:
        pass

@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/live", status_code=307)


@app.get("/health")
def public_health() -> dict[str, str]:
    try:
        config = LiveConfig.from_env()
        if config.validation_errors():
            return {"status": "degraded"}
        try:
            if config.market_ws_enabled and services()[6].health().get("status") in {
                "DISCONNECTED", "ERROR", "STALE", "STOPPED"
            }:
                return {"status": "degraded"}
            if services()[7].health().get("status") in {"AUTH_FAILED", "ERROR", "STALE"}:
                return {"status": "degraded"}
        except RuntimeError:
            pass
        return {"status": "ok"}
    except Exception:
        return {"status": "degraded"}

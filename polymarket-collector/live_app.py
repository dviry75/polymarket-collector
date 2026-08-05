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
from live.router import configure, router, services, strategy_services
from live.market_discovery import refresh_btc_5m_markets
from live.archive import SnapshotArchiveManager
from live.geographic import geographic_preflight


app = FastAPI(title="Polymarket LIVE Control Center")
app.include_router(router)
_discovery_task: asyncio.Task[None] | None = None
_reconciliation_task: asyncio.Task[None] | None = None
_metrics_task: asyncio.Task[None] | None = None


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


async def reconciliation_loop(config: LiveConfig) -> None:
    while True:
        strategy_repo, _runtime = strategy_services()
        active = bool(strategy_repo.unresolved_intents() or strategy_repo.active_positions())
        interval = (
            config.reconciliation_active_interval_seconds
            if active else config.reconciliation_interval_seconds
        )
        await asyncio.sleep(max(1, interval))
        await services()[5].run_once(actor="periodic_reconciliation")


async def metrics_loop(config: LiveConfig) -> None:
    manager = SnapshotArchiveManager(config, services()[1], strategy_services()[0])
    while True:
        manager.sample_db_growth()
        await asyncio.sleep(300)


@app.on_event("startup")
async def startup() -> None:
    global _discovery_task, _reconciliation_task, _metrics_task
    config = LiveConfig.from_env()
    configure(Path(config.live_db_path), config)
    repo = services()[1]
    strategy_repo, runtime = strategy_services()
    strategy_repo.set_pause_entries(True, "startup", "STARTUP_RECONCILIATION_REQUIRED")
    geo = await geographic_preflight()
    repo.set_state("geographic_availability", geo["status"], "startup")
    repo.set_state("geographic_country", geo.get("country") or "", "startup")
    repo.set_state("geographic_region", geo.get("region") or "", "startup")
    if geo["status"] != "ALLOWED":
        strategy_repo.alert(
            alert_type="GEOGRAPHIC", severity="CRITICAL",
            reason_code="GEOGRAPHIC_AVAILABILITY_FAILED",
            message="Official geographic preflight did not allow new orders",
        )
    await refresh_btc_5m_markets(repo)
    if strategy_repo.unresolved_intents() or strategy_repo.active_positions():
        strategy_repo.alert(
            alert_type="SERVICE_RESTART", severity="CRITICAL",
            reason_code="RESTART_WITH_OPEN_STATE",
            message="Service restarted with unresolved order/position state; entries paused",
        )
    await services()[5].run_once(actor="startup_reconciliation")
    _discovery_task = asyncio.create_task(
        market_discovery_loop(config), name="live-market-discovery"
    )
    _reconciliation_task = asyncio.create_task(
        reconciliation_loop(config), name="live-reconciliation"
    )
    _metrics_task = asyncio.create_task(metrics_loop(config), name="live-db-metrics")
    if config.market_ws_enabled:
        await services()[6].start(config.market_ws_url)
    if config.user_ws_enabled:
        await services()[7].start(config.user_ws_url)
    await runtime.start_heartbeat()


@app.on_event("shutdown")
async def shutdown() -> None:
    global _discovery_task, _reconciliation_task, _metrics_task
    for task in (_discovery_task, _reconciliation_task, _metrics_task):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                # Background-loop failures are already persisted by the worker.
                # They must not prevent the remaining resources from closing.
                pass
    _discovery_task = _reconciliation_task = _metrics_task = None
    try:
        await strategy_services()[1].stop()
    except Exception:
        pass
    try:
        await services()[6].stop()
    except Exception:
        pass
    try:
        await services()[7].stop()
    except Exception:
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

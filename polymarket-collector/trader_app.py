from __future__ import annotations

import asyncio
from pathlib import Path

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover
    truststore = None

from fastapi import FastAPI

from live.archive import SnapshotArchiveManager
from live.config import LiveConfig
from live.geographic import geographic_preflight
from live.ipc import TraderIPCServer
from live.market_discovery import refresh_btc_5m_markets
from live.router import configure, services, strategy_services
from live.trader_commands import TraderCommandHandler


app = FastAPI(title="Polymarket Trading Core", docs_url=None, redoc_url=None)
_discovery_task: asyncio.Task[None] | None = None
_reconciliation_task: asyncio.Task[None] | None = None
_metrics_task: asyncio.Task[None] | None = None
_ipc_server: TraderIPCServer | None = None


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
    global _discovery_task, _reconciliation_task, _metrics_task, _ipc_server
    config = LiveConfig.from_env()
    configure(Path(config.live_db_path), config)
    repo = services()[1]
    strategy_repo, runtime = strategy_services()
    strategy_repo.set_pause_entries(True, "startup", "STARTUP_RECONCILIATION_REQUIRED")

    _ipc_server = TraderIPCServer(config.trader_socket_path, TraderCommandHandler())
    await _ipc_server.start()

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
            message="Trader restarted with unresolved order/position state; entries paused",
        )
    await services()[5].run_once(actor="startup_reconciliation")

    _discovery_task = asyncio.create_task(market_discovery_loop(config), name="live-market-discovery")
    _reconciliation_task = asyncio.create_task(reconciliation_loop(config), name="live-reconciliation")
    _metrics_task = asyncio.create_task(metrics_loop(config), name="live-db-metrics")
    if config.market_ws_enabled:
        await services()[6].start(config.market_ws_url)
    if config.user_ws_enabled:
        await services()[7].start(config.user_ws_url)
    await runtime.start_heartbeat()


@app.on_event("shutdown")
async def shutdown() -> None:
    global _discovery_task, _reconciliation_task, _metrics_task, _ipc_server
    for task in (_discovery_task, _reconciliation_task, _metrics_task):
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
    _discovery_task = _reconciliation_task = _metrics_task = None
    for stop in (
        strategy_services()[1].stop,
        services()[6].stop,
        services()[7].stop,
    ):
        try:
            await asyncio.wait_for(stop(), 10)
        except (asyncio.TimeoutError, Exception):
            pass
    if _ipc_server is not None:
        await _ipc_server.stop()
        _ipc_server = None


@app.get("/health")
def health() -> dict[str, object]:
    try:
        config = services()[0]
        strategy = strategy_services()[1].health()
        return {
            "status": "ok" if not config.validation_errors() else "degraded",
            "strategy_readiness": strategy.get("readiness"),
        }
    except Exception:
        return {"status": "degraded"}

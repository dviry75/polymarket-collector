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
from live.pause_recovery import PauseRecoveryCoordinator
from live.reconciliation_coordinator import ReconciliationCadencePolicy
from live.repository import now_iso
from live.router import (
    configure, reconciliation_coordinator, services, strategy_services,
)
from live.trader_commands import TraderCommandHandler


app = FastAPI(title="Polymarket Trading Core", docs_url=None, redoc_url=None)
_discovery_task: asyncio.Task[None] | None = None
_reconciliation_task: asyncio.Task[None] | None = None
_metrics_task: asyncio.Task[None] | None = None
_pause_recovery_task: asyncio.Task[None] | None = None
_geographic_task: asyncio.Task[None] | None = None
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
    cadence = ReconciliationCadencePolicy(
        config.reconciliation_active_interval_seconds,
        config.reconciliation_interval_seconds,
    )
    while True:
        strategy_repo, _runtime = strategy_services()
        active_work = [
            {
                "kind": "intent",
                "id": intent.get("intent_id"),
                "state": intent.get("state"),
                "updated_at": intent.get("updated_at"),
            }
            for intent in strategy_repo.fast_reconciliation_intents()
        ]
        active_work.extend({
            "kind": "position",
            "id": position.get("position_id"),
            "state": position.get("state"),
            "updated_at": position.get("updated_at"),
        } for position in strategy_repo.fast_reconciliation_positions())
        await asyncio.sleep(cadence.interval(active_work))
        await reconciliation_coordinator().request(
            actor="periodic_reconciliation"
        )


async def pause_recovery_loop(coordinator: PauseRecoveryCoordinator) -> None:
    while True:
        await asyncio.sleep(0.5)
        try:
            await asyncio.to_thread(coordinator.tick)
        except Exception as exc:
            await asyncio.to_thread(coordinator.mark_degraded, exc)
            services()[1].audit(
                "pause_recovery", "pause_recovery_tick", "error",
                f"{type(exc).__name__}: {exc}"[:500],
            )


async def metrics_loop(config: LiveConfig) -> None:
    manager = SnapshotArchiveManager(config, services()[1], strategy_services()[0])
    while True:
        manager.sample_db_growth()
        await asyncio.sleep(300)


async def geographic_recovery_loop(config: LiveConfig) -> None:
    interval = max(60, int(config.geographic_preflight_ttl_seconds / 2))
    while True:
        await asyncio.sleep(interval)
        try:
            geo = await geographic_preflight()
            services()[1].set_states({
                "geographic_availability": geo["status"],
                "geographic_country": geo.get("country") or "",
                "geographic_region": geo.get("region") or "",
                "geographic_checked_at": now_iso(),
                "geographic_last_error": "",
            }, "geographic_recovery")
            strategy_repo = strategy_services()[0]
            if geo["status"] == "ALLOWED":
                strategy_repo.resolve_alert(
                    alert_type="GEOGRAPHIC",
                    reason_code="GEOGRAPHIC_AVAILABILITY_FAILED",
                    actor="geographic_recovery",
                    resolution_reason="GEOGRAPHIC_PREFLIGHT_ALLOWED",
                )
            else:
                strategy_repo.alert(
                    alert_type="GEOGRAPHIC",
                    severity="CRITICAL",
                    reason_code="GEOGRAPHIC_AVAILABILITY_FAILED",
                    message=(
                        "Official geographic preflight did not allow "
                        "new orders"
                    ),
                )
        except Exception as exc:
            services()[1].set_state(
                "geographic_last_error",
                f"{type(exc).__name__}: {exc}"[:500],
                "geographic_recovery",
            )


@app.on_event("startup")
async def startup() -> None:
    global _discovery_task, _reconciliation_task, _metrics_task, _pause_recovery_task, _geographic_task, _ipc_server
    config = LiveConfig.from_env()
    configure(Path(config.live_db_path), config)
    repo = services()[1]
    strategy_repo, runtime = strategy_services()
    repo.finalize_orphaned_reconciliations(actor="startup")
    strategy_repo.acquire_pause(
        actor="startup",
        reason="SAFETY_STARTUP_HOLD",
        owner="MACHINE",
    )
    if config.validation_errors():
        strategy_repo.acquire_pause(
            actor="startup",
            reason="CONFIG_INVALID",
            owner="MACHINE",
        )

    _ipc_server = TraderIPCServer(config.trader_socket_path, TraderCommandHandler())
    await _ipc_server.start()

    geo = await geographic_preflight()
    repo.set_states({
        "geographic_availability": geo["status"],
        "geographic_country": geo.get("country") or "",
        "geographic_region": geo.get("region") or "",
        "geographic_checked_at": now_iso(),
    }, "startup")
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
    await reconciliation_coordinator().start()
    startup_reconciliation = await reconciliation_coordinator().request(
        actor="startup_reconciliation", force=True
    )
    if startup_reconciliation.get("status") == "ok":
        repaired_dust = strategy_repo.repair_terminal_dust_slots(
            actor="startup_reconciliation"
        )
        if repaired_dust:
            await reconciliation_coordinator().request(
                actor="post_dust_repair_reconciliation",
                evidence_changed=True,
                force=True,
            )

    _discovery_task = asyncio.create_task(market_discovery_loop(config), name="live-market-discovery")
    _reconciliation_task = asyncio.create_task(reconciliation_loop(config), name="live-reconciliation")
    _metrics_task = asyncio.create_task(metrics_loop(config), name="live-db-metrics")
    _geographic_task = asyncio.create_task(
        geographic_recovery_loop(config), name="live-geographic-recovery"
    )
    if config.market_ws_enabled:
        await services()[6].start(config.market_ws_url)
    if config.user_ws_enabled:
        await services()[7].start(config.user_ws_url)
    await runtime.start_heartbeat()
    coordinator = PauseRecoveryCoordinator(
        repo,
        strategy_repo,
        services()[6],
        services()[7],
        config=config,
    )
    _pause_recovery_task = asyncio.create_task(
        pause_recovery_loop(coordinator), name="live-pause-recovery"
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    global _discovery_task, _reconciliation_task, _metrics_task, _pause_recovery_task, _geographic_task, _ipc_server
    for task in (
        _discovery_task, _reconciliation_task, _metrics_task,
        _pause_recovery_task, _geographic_task,
    ):
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
    _discovery_task = _reconciliation_task = _metrics_task = None
    _pause_recovery_task = _geographic_task = None
    for stop in (
        strategy_services()[1].stop,
        services()[6].stop,
        services()[7].stop,
    ):
        try:
            await asyncio.wait_for(stop(), 10)
        except (asyncio.TimeoutError, Exception):
            pass
    try:
        await reconciliation_coordinator().stop()
    except RuntimeError:
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

import asyncio
import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from live.ipc import TraderIPCClient, TraderIPCServer
from live.market_websocket import MarketWebSocketManager, UserWebSocketManager
from live.repository import LiveRepository


def build_repo(tmp: str) -> LiveRepository:
    repo = LiveRepository(Path(tmp) / "live.sqlite3")
    repo.migrate()
    return repo


def test_query_only_repository_enforces_sqlite_boundary():
    with tempfile.TemporaryDirectory() as tmp:
        writer = build_repo(tmp)
        writer.set_state("boundary", "safe")
        reader = LiveRepository(writer.db_path, query_only=True)
        assert reader.get_state("boundary") == "safe"
        with pytest.raises(sqlite3.OperationalError):
            reader.set_state("boundary", "unsafe")
        with pytest.raises(RuntimeError):
            reader.migrate()


def test_unix_ipc_round_trip_and_socket_permissions():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "trader.sock"

            async def handler(command, payload):
                assert command == "STATUS"
                return {"echo": payload["value"]}

            server = TraderIPCServer(socket_path, handler)
            await server.start()
            try:
                client = TraderIPCClient(socket_path)
                result = await client.call_async("STATUS", {"value": 7})
                assert result == {"echo": 7}
                assert socket_path.stat().st_mode & 0o777 == 0o660
            finally:
                await server.stop()
            assert not socket_path.exists()

    asyncio.run(scenario())


def test_market_cache_sqlite_refresh_does_not_hold_publish_lock():
    with tempfile.TemporaryDirectory() as tmp:
        repo = build_repo(tmp)
        repo.upsert_market({
            "condition_id": "condition-a", "event_id": "event-a",
            "yes_token_id": "yes-a", "no_token_id": "no-a",
            "accepting_orders": True,
        })
        manager = MarketWebSocketManager(repo)
        entered = threading.Event()
        release = threading.Event()
        original = repo.market_for_asset

        def slow_lookup(asset_id):
            entered.set()
            release.wait(2)
            return original(asset_id)

        with patch.object(repo, "market_for_asset", side_effect=slow_lookup):
            refresh = threading.Thread(
                target=manager._refresh_market_cache, args=(["yes-a"],)
            )
            refresh.start()
            assert entered.wait(1)
            started = time.monotonic()
            manager._cache_market({
                "condition_id": "condition-b", "event_id": "event-b",
                "yes_token_id": "yes-b", "no_token_id": "no-b",
            })
            elapsed = time.monotonic() - started
            release.set()
            refresh.join(2)
        assert elapsed < 0.1


def test_market_writer_discards_failed_connection():
    with tempfile.TemporaryDirectory() as tmp:
        repo = build_repo(tmp)
        manager = MarketWebSocketManager(repo)
        with patch.object(
            repo, "set_states_on_connection", side_effect=sqlite3.OperationalError("disk I/O")
        ):
            with pytest.raises(sqlite3.OperationalError):
                manager._persistence_batch_sync([], {"market_ws_status": "CONNECTED"})
        assert manager._persistence_connection is None
        assert manager.persistence_failures == 1


def test_user_ws_receive_queues_before_slow_persistence():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            repo = build_repo(tmp)
            market = {
                "condition_id": "condition", "yes_token_id": "yes", "no_token_id": "no"
            }
            manager = UserWebSocketManager(
                repo,
                condition_ids_provider=lambda: ["condition"],
                market_provider=lambda _condition: market,
                event_queue_capacity=64,
            )
            manager._event_queue = asyncio.Queue(maxsize=64)
            manager._event_worker_task = asyncio.create_task(manager._event_worker())
            original = repo.store_ws_event

            def slow_store(*args, **kwargs):
                time.sleep(0.05)
                return original(*args, **kwargs)

            with patch.object(repo, "store_ws_event", side_effect=slow_store):
                started = time.monotonic()
                for index in range(20):
                    await manager._receive(json.dumps({
                        "event_type": "trade", "status": "MATCHED",
                        "id": f"trade-{index}", "market": "condition",
                        "asset_id": "yes",
                    }))
                enqueue_elapsed = time.monotonic() - started
                await asyncio.wait_for(manager._event_queue.join(), 3)
            manager._stop.set()
            await manager._event_worker_task
            assert enqueue_elapsed < 0.1
            assert manager.trade_events_received == 20
            assert manager.event_queue_dropped == 0

    asyncio.run(scenario())


def test_dashboard_service_has_no_trading_secret_environment_file():
    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy" / "polymarket-dashboard.service").read_text()
    env = (root / "deploy" / "dashboard.env.example").read_text()
    assert "dashboard.env" in unit
    assert "dashboard_app:app" in unit
    for forbidden in (
        "POLYMARKET_PRIVATE_KEY=", "POLYMARKET_API_KEY=",
        "POLYMARKET_API_SECRET=", "POLYMARKET_API_PASSPHRASE=",
    ):
        assert forbidden not in env


def test_trader_command_owner_changes_state_over_ipc_only():
    from live.config import LiveConfig
    from live.router import configure
    from live.trader_commands import TraderCommandHandler

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live.sqlite3"
            socket_path = Path(tmp) / "trader.sock"
            config = LiveConfig(
                live_db_path=str(db), trader_socket_path=str(socket_path),
                live_adapter="mock", execution_mode="READ_ONLY",
            )
            configure(db, config)
            server = TraderIPCServer(socket_path, TraderCommandHandler())
            await server.start()
            try:
                client = TraderIPCClient(socket_path)
                paused = await client.call_async("PAUSE_ENTRIES")
                assert paused == {"ok": True, "pause_entries": True}
                reader = LiveRepository(db, query_only=True)
                assert reader.get_state("pause_entries") == "true"
                with pytest.raises(sqlite3.OperationalError):
                    reader.set_state("pause_entries", "false")
            finally:
                await server.stop()

    asyncio.run(scenario())


def test_continuous_resume_does_not_require_canary_but_keeps_other_gates():
    from live.config import LiveConfig
    from live.router import configure, services, strategy_services
    from live.trader_commands import TraderCommandHandler

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live.sqlite3"
            config = LiveConfig(
                live_db_path=str(db), live_adapter="mock",
                execution_mode="REAL_TRADING", continuous_trading_enabled=True,
            )
            configure(db, config)
            repo = services()[1]
            repo.set_state("canary_armed", "false")
            repo.set_state("order_heartbeat_status", "OK")
            with patch.object(
                strategy_services()[1], "health",
                return_value={
                    "market_data_readiness": "READY",
                    "reconciliation_readiness": "READY",
                },
            ), patch.object(
                services()[7], "health", return_value={"status": "CONNECTED"}
            ):
                result = await TraderCommandHandler()("RESUME_ENTRIES", {})
            assert result == {"ok": True, "pause_entries": False}
            assert repo.get_state("pause_entries") == "false"

    asyncio.run(scenario())


def test_event_loop_block_detector_captures_task_stacks():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            manager = MarketWebSocketManager(build_repo(tmp))
            watchdog = asyncio.create_task(
                manager._event_loop_watchdog(), name="watchdog-test"
            )
            await asyncio.sleep(0.02)
            time.sleep(0.08)
            await asyncio.sleep(0.03)
            manager._stop.set()
            await watchdog
            assert manager._event_loop_stalls
            assert manager._event_loop_stalls[-1]["lag_ms"] >= 50
            assert manager._event_loop_stalls[-1]["tasks"]

    asyncio.run(scenario())


def test_crash_before_submit_survives_restart_and_fails_closed():
    from live.adapters.mock import MockTradingAdapter
    from live.reconciliation import ReconciliationWorker
    from live.strategy_repository import StrategyRepository

    with tempfile.TemporaryDirectory() as tmp:
        repo = build_repo(tmp)
        strategy = StrategyRepository(repo)
        strategy.migrate()
        reserved = strategy.reserve_event_entry(
            event_id="crash-before", condition_id="condition-before",
            token_id="yes-before", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        restarted = StrategyRepository(LiveRepository(repo.db_path))
        duplicate = restarted.reserve_event_entry(
            event_id="crash-before", condition_id="condition-before",
            token_id="yes-before", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        assert duplicate["_duplicate"]
        result = asyncio.run(
            ReconciliationWorker(repo, MockTradingAdapter(), restarted).run_once("crash-before")
        )
        assert result["status"] == "gaps"
        assert any(gap["type"] == "durable_intent_without_remote_id" for gap in result["gaps"])
        assert repo.get_state("pause_entries") == "true"
        assert restarted.intent(reserved["entry_intent_id"])["state"] == "RESERVED"


def test_crash_after_submit_before_response_detects_both_sides_and_never_retries():
    from live.adapters.mock import MockTradingAdapter
    from live.reconciliation import ReconciliationWorker
    from live.strategy_repository import StrategyRepository

    with tempfile.TemporaryDirectory() as tmp:
        repo = build_repo(tmp)
        strategy = StrategyRepository(repo)
        strategy.migrate()
        reserved = strategy.reserve_event_entry(
            event_id="crash-after", condition_id="condition-after",
            token_id="yes-after", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        strategy.update_intent(reserved["entry_intent_id"], state="SUBMITTING")
        adapter = MockTradingAdapter()
        adapter.orders["remote-after"] = {
            "polymarket_order_id": "remote-after", "status": "live",
            "condition_id": "condition-after", "token_id": "yes-after",
        }
        restarted = StrategyRepository(LiveRepository(repo.db_path))
        result = asyncio.run(
            ReconciliationWorker(repo, adapter, restarted).run_once("crash-after")
        )
        gap_types = {gap["type"] for gap in result["gaps"]}
        assert result["status"] == "gaps"
        assert "durable_intent_without_remote_id" in gap_types
        assert "remote_order_missing_local" in gap_types
        assert repo.get_state("pause_entries") == "true"
        assert restarted.intent(reserved["entry_intent_id"])["state"] == "SUBMITTING"
        assert len(adapter.orders) == 1


def _dashboard_read_once(db_path: str) -> int:
    reader = LiveRepository(db_path, query_only=True)
    return len(reader.list_table("live_audit_log", 200)) + len(
        reader.list_table("live_markets", 100)
    )


def test_fifty_dashboard_refreshes_do_not_stall_trader_event_loop():
    from concurrent.futures import ProcessPoolExecutor

    async def scenario(db_path: str):
        loop = asyncio.get_running_loop()
        lag_samples = []
        stop = asyncio.Event()

        async def heartbeat():
            interval = 0.01
            expected = loop.time() + interval
            while not stop.is_set():
                await asyncio.sleep(interval)
                now = loop.time()
                lag_samples.append(max(0.0, (now - expected) * 1000))
                expected = now + interval

        task = asyncio.create_task(heartbeat())
        with ProcessPoolExecutor(max_workers=4) as pool:
            results = await asyncio.gather(*[
                loop.run_in_executor(pool, _dashboard_read_once, db_path)
                for _ in range(50)
            ])
        stop.set()
        await task
        assert all(result >= 0 for result in results)
        assert max(lag_samples or [0]) < 100

    with tempfile.TemporaryDirectory() as tmp:
        repo = build_repo(tmp)
        for index in range(10):
            repo.set_state(f"stress-{index}", str(index))
        asyncio.run(scenario(str(repo.db_path)))

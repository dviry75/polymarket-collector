import asyncio
import inspect
import tempfile
import time
from pathlib import Path

from live.market_websocket import MarketWebSocketManager
from live.reconciliation import (
    RECONCILIATION_TELEMETRY_CAPACITY, ReconciliationWorker,
)
from live.repository import LiveRepository


NOW_MS = 1_800_000_000_000


def make_repo():
    temporary = tempfile.TemporaryDirectory()
    repo = LiveRepository(Path(temporary.name) / "live.sqlite3")
    repo.migrate()
    repo.upsert_market({
        "event_id": "btc-updown-5m-1800000000",
        "condition_id": "condition",
        "yes_token_id": "yes",
        "no_token_id": "no",
        "token_mapping_status": "verified",
        "accepting_orders": True,
        "min_order_size": 1,
        "min_tick_size": 0.01,
        "taker_base_fee": 0,
        "raw_market_info": {
            "scope_verified": True,
            "slug": "btc-updown-5m-1800000000",
        },
    })
    return temporary, repo


def book(timestamp: int, ask: str = "0.74") -> dict:
    return {
        "event_type": "book",
        "asset_id": "yes",
        "timestamp": timestamp,
        "bids": [{"price": "0.73", "size": "10"}],
        "asks": [{"price": ask, "size": "10"}],
    }


def test_disconnect_classification_distinguishes_local_and_remote():
    class FakeSocket:
        close_code = None
        close_reason = None

        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    temporary, repo = make_repo()
    try:
        local = MarketWebSocketManager(repo)
        local._connection_generation = 7
        local._connection_started_monotonic = time.monotonic() - 2
        socket = FakeSocket()
        asyncio.run(local._close_websocket(
            socket, "LOCAL_CLOSE_BEST_PRICE_MISMATCH"
        ))
        local.mark_disconnect(
            "ConnectionError: MARKET_WS_BOOK_RESYNC:BEST_PRICE_MISMATCH",
            exc=ConnectionError(
                "MARKET_WS_BOOK_RESYNC:BEST_PRICE_MISMATCH"
            ),
            ws=socket,
        )
        classified = local.telemetry_snapshot()["last_disconnect"]
        assert socket.closed
        assert classified["local_close_initiated"] is True
        assert (
            classified["local_close_reason"]
            == "LOCAL_CLOSE_BEST_PRICE_MISMATCH"
        )
        assert classified["exception_class"] == "ConnectionError"

        remote = MarketWebSocketManager(repo)
        remote._connection_generation = 8
        remote.mark_disconnect(
            "ConnectionError: no close frame received",
            exc=ConnectionError("no close frame received"),
            ws=FakeSocket(),
        )
        classified = remote.telemetry_snapshot()["last_disconnect"]
        assert classified["local_close_initiated"] is False
        assert classified["local_close_reason"] == ""
    finally:
        temporary.cleanup()


def test_stale_messages_are_bucketed_by_generation_age():
    temporary, repo = make_repo()
    try:
        manager = MarketWebSocketManager(
            repo, stale_after_seconds=1, clock_ms=lambda: NOW_MS
        )
        manager.subscribed_asset_ids = ["yes"]
        manager._refresh_market_cache(["yes"])
        ages = (0.5, 1.0, 5.0, 15.0, 30.0, 60.0)
        expected = {
            "0_1s": 1,
            "1_5s": 1,
            "5_15s": 1,
            "15_30s": 1,
            "30_60s": 1,
            "gt_60s": 1,
        }
        for index, age in enumerate(ages):
            receive_ms = NOW_MS - 2_000 + index
            timing = {
                "generation_age_seconds": age,
                "socket_receive_wall_ms": receive_ms,
                "receive_wall_ns": receive_ms * 1_000_000,
            }
            assert not manager.process_message(
                book(NOW_MS - 3_000 + index, ask=f"0.7{index}"),
                timing=timing,
            )
        telemetry = manager.telemetry_snapshot()
        assert telemetry["messages_total_by_reconnect_age_bucket"] == expected
        assert telemetry["stale_total_by_reconnect_age_bucket"] == expected
    finally:
        temporary.cleanup()


def test_event_loop_lag_statistics_and_buckets_are_deterministic():
    temporary, repo = make_repo()
    try:
        manager = MarketWebSocketManager(repo)
        for lag_ms in (50, 101, 501, 1_001, 5_001, 20_001):
            manager._record_event_loop_lag(lag_ms)
        telemetry = manager.telemetry_snapshot()
        lag = telemetry["event_loop_lag_ms"]
        assert lag["current"] == 20_001
        assert lag["max"] == 20_001
        assert lag["p50"] == 751
        assert lag["p95"] == 5_001
        assert lag["p99"] == 5_001
        assert telemetry["event_loop_lag_buckets"] == {
            "gt_100ms": 5,
            "gt_500ms": 4,
            "gt_1000ms": 3,
            "gt_5000ms": 2,
            "gt_20000ms": 1,
        }
    finally:
        temporary.cleanup()


def test_message_timing_boundaries_and_negative_queue_wait_clamp():
    class FakeSocket:
        async def close(self):
            return None

    temporary, repo = make_repo()
    try:
        manager = MarketWebSocketManager(
            repo, stale_after_seconds=5, clock_ms=lambda: NOW_MS
        )
        manager.subscribed_asset_ids = ["yes"]
        manager._refresh_market_cache(["yes"])
        timing = {
            "generation_age_seconds": 2,
            "connection_generation": 1,
            "ingress_enqueued_monotonic": time.perf_counter() + 1,
            "socket_receive_wall_ms": NOW_MS - 50,
            "receive_wall_ns": (NOW_MS - 50) * 1_000_000,
        }
        queue = asyncio.Queue()
        queue.put_nowait((book(NOW_MS - 100), timing))
        reader_done = asyncio.Event()
        reader_done.set()

        asyncio.run(manager._market_frame_processor(
            FakeSocket(), queue, reader_done, asyncio.Event()
        ))

        assert timing["reader_recv_timestamp_ns"] == (
            NOW_MS - 50
        ) * 1_000_000
        assert timing["processor_start_timestamp_ns"] > 0
        assert timing["processor_finish_timestamp_ns"] >= (
            timing["processor_start_timestamp_ns"]
        )
        assert timing["ingress_queue_wait_ms"] == 0
        assert timing["market_processing_ms"] >= 0
        assert timing["exchange_age_at_reader_ms"] == 50
        assert timing["exchange_age_at_processing_ms"] == 100
        telemetry = manager.telemetry_snapshot()
        assert telemetry["ingress_queue_wait_ms"]["current"] == 0
        assert telemetry["market_processing_ms"]["current"] >= 0
    finally:
        temporary.cleanup()


def test_reader_reconciliation_order_and_market_semantics_are_unchanged():
    source = inspect.getsource(MarketWebSocketManager.run)
    assert source.index("await self.on_reconnect()") < source.index(
        "await self._run_ingress_pipeline(ws)"
    )

    temporary, repo = make_repo()
    try:
        manager = MarketWebSocketManager(
            repo, stale_after_seconds=1, clock_ms=lambda: NOW_MS
        )
        manager.subscribed_asset_ids = ["yes"]
        manager._refresh_market_cache(["yes"])
        assert manager.process_message(book(NOW_MS - 100))
        ready, reason = manager.order_books.event_ready(
            ["yes"], now_ms=NOW_MS, max_age_ms=1_000
        )
        assert ready is True
        assert reason == "READY"
        assert not manager.process_message(book(NOW_MS - 1_001))
        assert manager.rejection_reasons == {
            "STALE_EXCHANGE_TIMESTAMP": 1
        }
    finally:
        temporary.cleanup()


def test_reconciliation_duration_counts_success_failure_and_is_bounded():
    temporary, repo = make_repo()
    try:
        worker = ReconciliationWorker(repo, object())
        outcomes = iter(("ok", "gaps"))

        async def fake_run(_actor):
            return {"status": next(outcomes)}

        worker._run_once_serialized = fake_run
        assert asyncio.run(worker.run_once())["status"] == "ok"
        assert asyncio.run(worker.run_once())["status"] == "gaps"
        telemetry = worker.health()
        success = telemetry["reconciliation_duration_ms"]["success"]
        failure = telemetry["reconciliation_duration_ms"]["failure"]
        assert success["count"] == 1
        assert failure["count"] == 1
        assert success["current"] >= 0
        assert failure["current"] >= 0
        assert telemetry["reconciliation_started_at"]
        assert telemetry["reconciliation_finished_at"]

        metric = worker._reconciliation_duration_ms["success"]
        for value in range(RECONCILIATION_TELEMETRY_CAPACITY + 5):
            metric.observe(value)
        bounded = metric.snapshot()
        assert bounded["count"] == RECONCILIATION_TELEMETRY_CAPACITY
        assert bounded["current"] == RECONCILIATION_TELEMETRY_CAPACITY + 4
        assert bounded["max"] == RECONCILIATION_TELEMETRY_CAPACITY + 4
        assert bounded["p50"] == 516.5
        assert bounded["p95"] == 976
        assert bounded["p99"] == 1_017
    finally:
        temporary.cleanup()


def test_reconciliation_unexpected_exception_is_timed_as_failure():
    temporary, repo = make_repo()
    try:
        worker = ReconciliationWorker(repo, object())

        async def fail(_actor):
            raise RuntimeError("synthetic")

        worker._run_once_serialized = fail
        try:
            asyncio.run(worker.run_once())
        except RuntimeError as exc:
            assert str(exc) == "synthetic"
        else:
            raise AssertionError("expected original exception")
        assert worker.health()["reconciliation_duration_ms"]["failure"]["count"] == 1
    finally:
        temporary.cleanup()


def test_generation_first_book_timing_resets_and_rejects_old_generation():
    temporary, repo = make_repo()
    try:
        manager = MarketWebSocketManager(repo)
        manager._connection_generation = 1
        manager._connection_started_monotonic = 100.0
        manager._reset_generation_timing(["yes", "no"])
        manager._record_generation_first_book(
            {"event_type": "book", "asset_id": "yes"},
            {"connection_generation": 1}, 100.1,
        )
        first = manager.telemetry_snapshot()["generation_timing"]
        assert first["first_book_observed_count"] == 1
        assert first["generation_to_first_book_slot_1_ms"]["current"] == 100
        assert first["generation_to_first_book_slot_2_ms"]["current"] is None
        assert first["generation_to_first_required_books_ms"]["current"] is None

        manager._record_generation_first_book(
            {"event_type": "book", "asset_id": "no"},
            {"connection_generation": 1}, 100.25,
        )
        first = manager.telemetry_snapshot()["generation_timing"]
        assert first["generation_to_first_book_slot_2_ms"]["current"] == 250
        assert first["generation_to_first_required_books_ms"]["current"] == 250

        manager._connection_generation = 2
        manager._connection_started_monotonic = 200.0
        manager._reset_generation_timing(["yes", "no"])
        manager._record_generation_first_book(
            {"event_type": "book", "asset_id": "yes"},
            {"connection_generation": 1}, 200.1,
        )
        reset = manager.telemetry_snapshot()["generation_timing"]
        assert reset["generation"] == 2
        assert reset["first_book_observed_count"] == 0
        assert reset["first_book_slots"] == {}
        assert reset["generation_to_first_required_books_ms"]["sample_count"] == 1
        assert reset["generation_to_market_data_ready_ms"] == (
            "NOT_MEASURABLE_WITH_CURRENT_SEMANTICS"
        )
    finally:
        temporary.cleanup()


def test_websockets_keepalive_metric_name_and_disabled_clarification():
    temporary, repo = make_repo()
    try:
        telemetry = MarketWebSocketManager(repo).telemetry_snapshot()
        assert "heartbeat_timeout_disconnects" not in telemetry
        assert telemetry["websockets_keepalive_timeout_disconnects"] == 0
        assert telemetry["automatic_websockets_keepalive_enabled"] is False
        assert "ping_interval=None" in telemetry["websockets_keepalive_metric_note"]
    finally:
        temporary.cleanup()

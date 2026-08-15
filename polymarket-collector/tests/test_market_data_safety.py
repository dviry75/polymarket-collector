import asyncio
import csv
import json
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import time

from live.adapters.mock import MockTradingAdapter
from live.config import LiveConfig
from live.market_websocket import MarketWebSocketManager
from live.market_ws_latency_csv import (
    CSV_FIELDS, MarketWsLatencyCsvDiagnostic, normalize_exchange_timestamp,
)
from live.order_book import OrderBookSet
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository
from live.strategy_runtime import LiveStrategyRuntime


NOW_MS = 1_800_000_000_000


def book(asset_id: str, timestamp, ask: str = "0.74") -> dict:
    message = {
        "event_type": "book",
        "asset_id": asset_id,
        "bids": [{"price": "0.73", "size": "10"}],
        "asks": [{"price": ask, "size": "10"}],
    }
    if timestamp is not ...:
        message["timestamp"] = timestamp
    return message


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


def test_exchange_timestamp_rejections_and_fresh_acceptance():
    books = OrderBookSet(["yes", "no"])
    stale = books.apply(
        book("yes", NOW_MS - 113_000), now_ms=NOW_MS, max_age_ms=1_000,
        future_tolerance_ms=1_000,
    )
    assert stale.rejected_reason == "STALE_EXCHANGE_TIMESTAMP"
    assert stale.exchange_age_ms == 113_000
    assert stale.updates == ()

    fresh = books.apply(
        book("yes", NOW_MS - 250), now_ms=NOW_MS, max_age_ms=1_000,
        future_tolerance_ms=1_000,
    )
    assert not fresh.rejected_reason
    assert fresh.updates[0]["exchange_age_ms"] == 250

    missing = books.apply(
        book("no", ...), now_ms=NOW_MS, max_age_ms=5_000,
        future_tolerance_ms=1_000,
    )
    invalid = books.apply(
        book("no", "not-a-time"), now_ms=NOW_MS, max_age_ms=5_000,
        future_tolerance_ms=1_000,
    )
    future = books.apply(
        book("no", NOW_MS + 1_001), now_ms=NOW_MS, max_age_ms=5_000,
        future_tolerance_ms=1_000,
    )
    assert missing.rejected_reason == "MISSING_EXCHANGE_TIMESTAMP"
    assert invalid.rejected_reason == "INVALID_EXCHANGE_TIMESTAMP"
    assert future.rejected_reason == "FUTURE_EXCHANGE_TIMESTAMP"


def test_one_second_exchange_freshness_boundary():
    books = OrderBookSet(["yes", "no"])
    stale = books.apply(
        book("yes", NOW_MS - 1_001), now_ms=NOW_MS, max_age_ms=1_000,
    )
    assert stale.rejected_reason == "STALE_EXCHANGE_TIMESTAMP"
    assert stale.updates == ()

    fresh = books.apply(
        book("yes", NOW_MS - 1_000), now_ms=NOW_MS, max_age_ms=1_000,
    )
    assert not fresh.rejected_reason
    assert fresh.updates[0]["exchange_age_ms"] == 1_000


def test_unchanged_connected_book_does_not_expire_after_one_second():
    books = OrderBookSet(["yes", "no"])
    books.apply(book("yes", NOW_MS - 100), now_ms=NOW_MS, max_age_ms=1_000)
    books.apply(book("no", NOW_MS - 90, "0.26"), now_ms=NOW_MS, max_age_ms=1_000)

    assert books.event_ready(
        ["yes", "no"], now_ms=NOW_MS + 10_000, max_age_ms=1_000
    ) == (True, "READY")


def test_stale_initial_snapshot_is_structural_only_until_fresh_delta():
    books = OrderBookSet(["yes"])
    stale = books.apply(
        book("yes", NOW_MS - 113_000), now_ms=NOW_MS, max_age_ms=1_000
    )
    assert stale.rejected_reason == "STALE_EXCHANGE_TIMESTAMP"
    assert stale.updates == ()
    assert books.books["yes"].snapshot_loaded
    assert not books.books["yes"].ready

    fresh_delta = books.apply({
        "event_type": "price_change",
        "timestamp": NOW_MS - 100,
        "price_changes": [{
            "asset_id": "yes", "side": "SELL", "price": "0.74",
            "size": "11", "best_bid": "0.73", "best_ask": "0.74",
        }],
    }, now_ms=NOW_MS, max_age_ms=1_000)
    assert not fresh_delta.rejected_reason
    assert fresh_delta.updates[0]["book_ready"]
    assert books.event_ready(
        ["yes"], now_ms=NOW_MS, max_age_ms=1_000
    ) == (True, "READY")


def test_empty_book_side_sentinels_do_not_cause_false_mismatch():
    books = OrderBookSet(["yes", "no"])
    books.apply({
        "event_type": "book", "asset_id": "yes", "timestamp": NOW_MS - 100,
        "bids": [{"price": "0.99", "size": "5"}], "asks": [],
    }, now_ms=NOW_MS, max_age_ms=1_000)
    books.apply({
        "event_type": "book", "asset_id": "no", "timestamp": NOW_MS - 100,
        "bids": [], "asks": [{"price": "0.01", "size": "5"}],
    }, now_ms=NOW_MS, max_age_ms=1_000)

    yes = books.apply({
        "event_type": "best_bid_ask", "asset_id": "yes",
        "timestamp": NOW_MS - 50, "best_bid": "0.99", "best_ask": "1",
    }, now_ms=NOW_MS, max_age_ms=1_000)
    no = books.apply({
        "event_type": "best_bid_ask", "asset_id": "no",
        "timestamp": NOW_MS - 50, "best_bid": "0", "best_ask": "0.01",
    }, now_ms=NOW_MS, max_age_ms=1_000)

    assert yes.updates[0]["book_ready"]
    assert no.updates[0]["book_ready"]
    assert books.event_ready(
        ["yes", "no"], now_ms=NOW_MS, max_age_ms=1_000
    ) == (True, "READY")


def test_best_update_may_arrive_before_delta_without_advancing_book_timestamp():
    books = OrderBookSet(["yes"])
    books.apply({
        "event_type": "book", "asset_id": "yes", "timestamp": NOW_MS - 200,
        "bids": [{"price": "0.35", "size": "5"}],
        "asks": [{"price": "0.36", "size": "5"}],
    }, now_ms=NOW_MS, max_age_ms=1_000)
    prior_depth_timestamp = books.books["yes"].last_timestamp

    best_first = books.apply({
        "event_type": "best_bid_ask", "asset_id": "yes",
        "timestamp": NOW_MS - 100, "best_bid": "0.35", "best_ask": "0.37",
    }, now_ms=NOW_MS, max_age_ms=1_000)
    assert not best_first.updates[0]["book_ready"]
    assert best_first.updates[0]["readiness_reason"] == "BEST_PRICE_PENDING_DEPTH"
    assert books.books["yes"].last_timestamp == prior_depth_timestamp

    repaired = books.apply({
        # Real captures show the matching depth may carry a 1 ms older
        # exchange timestamp even though it follows on the wire.
        "event_type": "price_change", "timestamp": NOW_MS - 101,
        "price_changes": [
            {"asset_id": "yes", "side": "SELL", "price": "0.36", "size": "0"},
            {"asset_id": "yes", "side": "SELL", "price": "0.37", "size": "5",
             "best_bid": "0.35", "best_ask": "0.37"},
        ],
    }, now_ms=NOW_MS, max_age_ms=1_000)
    assert repaired.updates[0]["book_ready"]
    assert repaired.updates[0]["best_ask"] == "0.37"


def test_stale_ordered_delta_preserves_structure_but_cannot_be_ready():
    books = OrderBookSet(["yes"])
    books.apply({
        "event_type": "book", "asset_id": "yes", "timestamp": NOW_MS - 2_000,
        "bids": [{"price": "0.06", "size": "5"}],
        "asks": [{"price": "0.07", "size": "5"}],
    }, now_ms=NOW_MS, max_age_ms=1_000)
    stale_delta = books.apply({
        "event_type": "price_change", "timestamp": NOW_MS - 1_500,
        "price_changes": [
            {"asset_id": "yes", "side": "BUY", "price": "0.06", "size": "0"},
            {"asset_id": "yes", "side": "BUY", "price": "0.04", "size": "5",
             "best_bid": "0.04", "best_ask": "0.07"},
        ],
    }, now_ms=NOW_MS, max_age_ms=1_000)
    assert stale_delta.rejected_reason == "STALE_EXCHANGE_TIMESTAMP"
    assert stale_delta.updates == ()
    assert books.books["yes"].best_bid == Decimal("0.04")
    assert not books.books["yes"].ready

    fresh = books.apply({
        "event_type": "price_change", "timestamp": NOW_MS - 100,
        "price_changes": [{
            "asset_id": "yes", "side": "BUY", "price": "0.04", "size": "6",
            "best_bid": "0.04", "best_ask": "0.07",
        }],
    }, now_ms=NOW_MS, max_age_ms=1_000)
    assert fresh.updates[0]["book_ready"]


def test_price_change_best_never_prunes_levels_without_explicit_zero_delta():
    books = OrderBookSet(["yes"])
    books.apply({
        "event_type": "book", "asset_id": "yes", "timestamp": NOW_MS - 200,
        "bids": [
            {"price": "0.06", "size": "5"},
            {"price": "0.04", "size": "4"},
        ],
        "asks": [
            {"price": "0.07", "size": "5"},
            {"price": "0.09", "size": "4"},
        ],
    }, now_ms=NOW_MS, max_age_ms=1_000)
    moved = books.apply({
        "event_type": "price_change", "timestamp": NOW_MS - 100,
        "price_changes": [{
            "asset_id": "yes", "side": "BUY", "price": "0.04", "size": "6",
            "best_bid": "0.04", "best_ask": "0.09",
        }],
    }, now_ms=NOW_MS, max_age_ms=1_000)
    assert not moved.updates[0]["book_ready"]
    assert moved.updates[0]["readiness_reason"] == "BEST_PRICE_MISMATCH"
    assert moved.updates[0]["best_bid"] == "0.06"
    assert moved.updates[0]["best_ask"] == "0.07"
    assert Decimal("0.06") in books.books["yes"].bids
    assert Decimal("0.07") in books.books["yes"].asks



def test_exact_transition_survives_final_best_price_mismatch_fail_closed():
    books = OrderBookSet(["yes"])
    books.apply({
        "event_type": "book", "asset_id": "yes", "timestamp": NOW_MS - 200,
        "bids": [{"price": "0.70", "size": "5"}],
        "asks": [
            {"price": "0.73", "size": "5"},
            {"price": "0.74", "size": "5"},
            {"price": "0.76", "size": "5"},
        ],
    }, now_ms=NOW_MS, max_age_ms=1_000)

    frame = books.apply({
        "event_type": "price_change", "timestamp": NOW_MS - 100,
        "price_changes": [
            {
                "asset_id": "yes", "side": "SELL", "price": "0.73", "size": "0",
                "best_bid": "0.70", "best_ask": "0.74",
            },
            {
                "asset_id": "yes", "side": "SELL", "price": "0.74", "size": "0",
                "best_bid": "0.70", "best_ask": "0.75",
            },
        ],
    }, now_ms=NOW_MS, max_age_ms=1_000)

    assert frame.updates[0]["readiness_reason"] == "BEST_PRICE_MISMATCH"
    assert not frame.updates[0]["book_ready"]
    assert [item["best_ask"] for item in frame.top_transitions] == ["0.74", "0.75"]


def test_market_ws_transport_queue_is_bounded_but_burst_tolerant(monkeypatch):
    temp, repo = make_repo()
    try:
        monkeypatch.delenv("LIVE_MARKET_WS_LIBRARY_QUEUE_HIGH_WATER", raising=False)
        monkeypatch.delenv("LIVE_MARKET_WS_LIBRARY_QUEUE_LOW_WATER", raising=False)
        manager = MarketWebSocketManager(repo)
        assert manager.library_queue_high_water == 256
        assert manager.library_queue_low_water == 64
        monkeypatch.setenv("LIVE_MARKET_WS_LIBRARY_QUEUE_HIGH_WATER", "80")
        monkeypatch.setenv("LIVE_MARKET_WS_LIBRARY_QUEUE_LOW_WATER", "20")
        configured = MarketWebSocketManager(repo)
        assert configured.library_queue_high_water == 80
        assert configured.library_queue_low_water == 20
    finally:
        temp.cleanup()


def test_diagnostic_ring_discards_tokens_removed_by_rollover():
    temp, repo = make_repo()
    try:
        manager = MarketWebSocketManager(repo)
        manager._book_event_history = {
            "old-token": deque([{"wire_sequence": 1}], maxlen=64),
            "new-token": deque([{"wire_sequence": 2}], maxlen=64),
        }
        manager._prune_book_event_history(["new-token"])
        assert set(manager._book_event_history) == {"new-token"}
    finally:
        temp.cleanup()


def test_out_of_order_one_side_and_reconnect_warmup():
    books = OrderBookSet(["yes", "no"])
    books.apply(book("yes", NOW_MS - 100), now_ms=NOW_MS, max_age_ms=5_000)
    assert books.event_ready(
        ["yes", "no"], now_ms=NOW_MS, max_age_ms=5_000
    ) == (False, "AWAITING_SNAPSHOT")

    books.apply(book("no", NOW_MS - 90, "0.26"), now_ms=NOW_MS, max_age_ms=5_000)
    assert books.event_ready(
        ["yes", "no"], now_ms=NOW_MS, max_age_ms=5_000
    ) == (True, "READY")
    current_hash = books.books["yes"].last_message_hash
    old = books.apply(
        book("yes", NOW_MS - 200, "0.70"), now_ms=NOW_MS, max_age_ms=5_000
    )
    assert old.rejected_reason == "OUT_OF_ORDER_EXCHANGE_TIMESTAMP"
    assert books.books["yes"].best_ask == Decimal("0.74")
    assert books.books["yes"].last_message_hash == current_hash

    books.mark_not_ready("RECONNECT_AWAITING_FRESH_BOOKS")
    assert books.event_ready(
        ["yes", "no"], now_ms=NOW_MS, max_age_ms=5_000
    ) == (False, "RECONNECT_AWAITING_FRESH_BOOKS")
    books.apply(book("yes", NOW_MS - 50), now_ms=NOW_MS, max_age_ms=5_000)
    assert not books.event_ready(
        ["yes", "no"], now_ms=NOW_MS, max_age_ms=5_000
    )[0]
    books.apply(book("no", NOW_MS - 40, "0.26"), now_ms=NOW_MS, max_age_ms=5_000)
    assert books.event_ready(
        ["yes", "no"], now_ms=NOW_MS, max_age_ms=5_000
    ) == (True, "READY")


def test_level_views_are_cached_and_invalidated_per_changed_side():
    books = OrderBookSet(["yes"])
    snapshot = {
        "event_type": "book", "asset_id": "yes", "timestamp": NOW_MS - 100,
        "bids": [{"price": "0.72", "size": "5"}, {"price": "0.73", "size": "10"}],
        "asks": [{"price": "0.74", "size": "10"}, {"price": "0.75", "size": "5"}],
    }
    first = books.apply(snapshot, now_ms=NOW_MS, max_age_ms=5_000).updates[0]
    second = books.apply({
        "event_type": "price_change", "timestamp": NOW_MS - 50,
        "price_changes": [{
            "asset_id": "yes", "side": "BUY", "price": "0.72", "size": "6",
            "best_bid": "0.73", "best_ask": "0.74",
        }],
    }, now_ms=NOW_MS, max_age_ms=5_000).updates[0]
    assert second["asks"] is first["asks"]
    assert second["bids"] is not first["bids"]
    assert second["bids"][1] == {"price": "0.72", "size": "6"}


def test_113_second_frame_never_reaches_intent_callback():
    temp, repo = make_repo()
    contexts = []
    try:
        manager = MarketWebSocketManager(
            repo, stale_after_seconds=5, on_atomic_frame=contexts.append,
            clock_ms=lambda: NOW_MS,
        )
        manager.subscribed_asset_ids = ["yes", "no"]
        manager.status.status = "CONNECTED"
        manager._refresh_market_cache(["yes", "no"])
        assert not manager.process_message(book("yes", NOW_MS - 113_000))
        assert contexts == []
        assert manager.health()["rejection_reasons"] == {
            "STALE_EXCHANGE_TIMESTAMP": 1
        }
        assert repo.get_state("strategy_block_reason") == "STALE_EXCHANGE_TIMESTAMP"
    finally:
        temp.cleanup()


def test_pre_submission_recheck_blocks_aged_data_without_adapter_call():
    class RecordingAdapter(MockTradingAdapter):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def create_order(self, order):
            self.calls.append(order)
            return await super().create_order(order)

    temp, base = make_repo()
    strategy = StrategyRepository(base)
    strategy.migrate()
    adapter = RecordingAdapter()
    try:
        event_id = f"btc-updown-5m-{int(datetime.now(timezone.utc).timestamp()) - 180}"
        base.upsert_market({
            "event_id": event_id, "condition_id": "condition",
            "yes_token_id": "yes", "no_token_id": "no",
            "token_mapping_status": "verified", "accepting_orders": True,
            "min_order_size": 1, "min_tick_size": 0.01, "taker_base_fee": 0,
            "raw_market_info": {"scope_verified": True, "slug": event_id},
        })
        base.set_states({
            "kill_switch": "false", "canary_armed": "true",
            "canary_consumed": "false", "pause_entries": "false",
        }, "operator")
        strategy.set_pause_entries(False, "operator", "TEST_READY")
        runtime = LiveStrategyRuntime(
            LiveConfig(execution_mode="REAL_TRADING"), base, strategy, adapter
        )
        runtime.entry_schedule_status = lambda: {
            "allowed": True, "reason": "ENTRY_SCHEDULE_ACTIVE",
            "timezone": "Asia/Jerusalem", "local_time": "test",
        }
        freshness_calls = 0

        def freshness(_condition):
            nonlocal freshness_calls
            freshness_calls += 1
            if freshness_calls == 1:
                return {
                    "ready": True, "reason": "READY",
                    "exchange_age_ms": {"yes": 100, "no": 100},
                    "book_versions": {
                        "yes": {"generation": 1, "update_number": 1}
                    },
                }
            return {
                "ready": False, "reason": "STALE_EXCHANGE_TIMESTAMP",
                "exchange_age_ms": {"yes": 5_001, "no": 100},
            }

        runtime.set_market_freshness_provider(freshness)
        market = base.latest_market("condition")
        update = {
            "asset_id": "yes", "outcome": "YES", "best_ask": "0.74",
            "asks": [{"price": "0.74", "size": "10"}],
            "generation": 1, "update_number": 1,
        }
        asyncio.run(runtime._process_event(
            market=market, updates=[update], event_ready=True,
            readiness_reason="READY",
            received_at=datetime.now(timezone.utc).isoformat(),
            frame_hash="fresh-then-stale",
        ))
        intents = base.list_table("live_strategy_intents", 10)
        assert freshness_calls == 2
        assert len(intents) == 1
        assert intents[0]["state"] == "REJECTED"
        assert intents[0]["reason_code"] == "STALE_EXCHANGE_TIMESTAMP"
        assert adapter.calls == []
        assert strategy.pause_entries()
        assert base.get_state("canary_armed") == "false"
    finally:
        temp.cleanup()


def test_read_only_logs_exact_074_blocker_without_creating_intent(caplog):
    temp, base = make_repo()
    strategy = StrategyRepository(base)
    strategy.migrate()
    runtime = LiveStrategyRuntime(
        LiveConfig(execution_mode="READ_ONLY"), base, strategy, MockTradingAdapter()
    )
    try:
        runtime.entry_schedule_status = lambda: {
            "allowed": True, "reason": "TEST_SCHEDULE_ACTIVE",
            "local_time": "fixed-test-time",
        }
        with caplog.at_level("WARNING", logger="live.strategy_runtime"):
            runtime.schedule_frame({
                "event_type": "price_change",
                "message_hash": "read-only-trigger",
                "updates": [{
                    "condition_id": "condition", "asset_id": "yes",
                    "outcome": "YES", "best_ask": "0.74",
                }],
                "event_readiness": {
                    "condition": {"ready": True, "reason": "READY"}
                },
            })

        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "ENTRY_074" in message
            and "outcome=BLOCKED" in message
            and "reason=PAUSE_ENTRIES" in message
            for message in messages
        )
        assert base.list_table("live_strategy_intents", 10) == []
    finally:
        temp.cleanup()


def test_slow_sqlite_writer_does_not_delay_memory_book_or_grow_unbounded():
    temp, repo = make_repo()
    original = repo.store_market_snapshots

    def slow_store(items):
        time.sleep(0.05)
        return original(items)

    repo.store_market_snapshots = slow_store
    manager = MarketWebSocketManager(
        repo, stale_after_seconds=5, snapshot_min_interval_seconds=0,
        persistence_queue_capacity=2, clock_ms=lambda: NOW_MS,
    )
    manager.subscribed_asset_ids = ["yes", "no"]
    manager.status.status = "CONNECTED"
    manager._refresh_market_cache(["yes", "no"])

    async def scenario():
        started = time.perf_counter()
        for index in range(1_000):
            manager.process_message(
                book("yes", NOW_MS - 1_000 + index, str(Decimal("0.70") + Decimal(index % 5) / 100))
            )
        hot_path_seconds = time.perf_counter() - started
        latest_timestamp = manager.order_books.books["yes"].last_exchange_timestamp_ms
        queue_depth = len(manager._pending_snapshots)
        await asyncio.sleep(0.2)
        manager._stop.set()
        manager._persistence_event.set()
        if manager._persistence_task:
            await asyncio.wait_for(manager._persistence_task, 2)
        return hot_path_seconds, latest_timestamp, queue_depth

    try:
        elapsed, latest, depth = asyncio.run(scenario())
        assert elapsed < 0.5
        assert latest == NOW_MS - 1
        assert depth <= 2
        assert manager.max_persistence_queue_depth <= 2
        assert manager.snapshots_coalesced > 900
        assert manager.last_receive_latency_ms == 1
        assert len(repo.list_table("live_market_snapshots", 100)) <= 2
    finally:
        temp.cleanup()


def test_snapshot_queue_is_separate_from_critical_audit_path():
    temp, repo = make_repo()
    try:
        manager = MarketWebSocketManager(
            repo, persistence_queue_capacity=2, clock_ms=lambda: NOW_MS
        )
        assert set(manager._pending_snapshots) == set()
        repo.audit(
            "test", "critical_order_fill_audit", "ok",
            details={"order_id": "order-1", "fill_id": "fill-1"},
        )
        rows = repo.list_table("live_audit_log", 10)
        assert any(row["action"] == "critical_order_fill_audit" for row in rows)
        assert set(manager._pending_snapshots) == set()
    finally:
        temp.cleanup()


def test_disconnect_state_cannot_be_overwritten_by_inflight_ready():
    temp, repo = make_repo()
    original = repo.set_states

    def slow_states(values, actor="system"):
        time.sleep(0.03)
        return original(values, actor)

    repo.set_states = slow_states
    manager = MarketWebSocketManager(
        repo, stale_after_seconds=5, clock_ms=lambda: NOW_MS
    )
    manager.subscribed_asset_ids = ["yes", "no"]
    manager.status.status = "CONNECTED"
    manager._refresh_market_cache(["yes", "no"])

    async def scenario():
        manager.process_message(book("yes", NOW_MS - 100))
        manager.process_message(book("no", NOW_MS - 90, "0.26"))
        await asyncio.sleep(0)
        manager.mark_disconnect("test disconnect")
        await asyncio.sleep(0.15)
        manager._stop.set()
        manager._persistence_event.set()
        if manager._persistence_task:
            await asyncio.wait_for(manager._persistence_task, 2)

    try:
        asyncio.run(scenario())
        assert repo.get_state("market_ws_status") == "DISCONNECTED"
        assert repo.get_state("strategy_readiness") == "NOT_READY"
        assert repo.get_state("strategy_block_reason") == "WS_DISCONNECTED"
    finally:
        temp.cleanup()


def test_market_subscription_updates_match_official_protocol():
    temp, repo = make_repo()
    try:
        manager = MarketWebSocketManager(repo)
        assert manager.dynamic_subscription_message(["yes"], "subscribe") == {
            "operation": "subscribe",
            "assets_ids": ["yes"],
            "custom_feature_enabled": True,
        }
        assert manager.dynamic_subscription_message(["yes"], "unsubscribe") == {
            "operation": "unsubscribe",
            "assets_ids": ["yes"],
        }
    finally:
        temp.cleanup()


def test_ingress_saturation_fails_closed_and_preserves_resolution():
    temp, repo = make_repo()
    try:
        manager = MarketWebSocketManager(
            repo, ingress_queue_capacity=2, clock_ms=lambda: NOW_MS
        )
        manager.order_books.ensure_assets(["yes", "no"])
        queue = asyncio.Queue(maxsize=2)
        timing = {"ingress_enqueued_monotonic": time.perf_counter()}
        queue.put_nowait((book("yes", NOW_MS - 10), dict(timing)))
        queue.put_nowait(({
            "event_type": "market_resolved",
            "condition_id": "condition",
            "winning_asset_id": "yes",
            "timestamp": NOW_MS,
        }, dict(timing)))
        resync = asyncio.Event()

        manager._handle_ingress_saturation(
            queue, (book("no", NOW_MS - 5, "0.26"), dict(timing)), resync
        )

        assert resync.is_set()
        assert manager.ingress_queue_saturations == 1
        assert manager.ingress_resyncs == 1
        assert manager.ingress_market_frames_discarded == 2
        assert manager.ingress_critical_frames_preserved == 1
        assert queue.qsize() == 1
        retained, _ = queue.get_nowait()
        assert retained["event_type"] == "market_resolved"
        assert repo.get_state("strategy_readiness") == "NOT_READY"
        assert repo.get_state("strategy_block_reason") == (
            "INGRESS_QUEUE_SATURATED_RESYNC"
        )
    finally:
        temp.cleanup()


def test_ingress_pipeline_preserves_order_and_ignores_removed_tokens():
    temp, repo = make_repo()

    class FakeSocket:
        def __init__(self, messages):
            self.messages = iter(messages)
            self.closed = False

        async def recv(self):
            await asyncio.sleep(0)
            try:
                return next(self.messages)
            except StopIteration:
                raise ConnectionError("fixture complete")

        async def close(self):
            self.closed = True

    manager = MarketWebSocketManager(
        repo, stale_after_seconds=5, ingress_queue_capacity=8,
        clock_ms=lambda: NOW_MS,
    )
    manager.subscribed_asset_ids = ["yes", "no"]
    manager.status.status = "CONNECTED"
    manager._refresh_market_cache(["yes", "no"])
    payloads = [json.dumps(book("removed", NOW_MS - 100))]
    payloads.extend(
        json.dumps(book("yes", NOW_MS - 90 + index, "0.74"))
        for index in range(50)
    )

    async def scenario():
        try:
            await manager._run_ingress_pipeline(FakeSocket(payloads))
        except ConnectionError as exc:
            assert str(exc) == "fixture complete"
        manager._stop.set()
        manager._persistence_event.set()
        if manager._persistence_task:
            await asyncio.wait_for(manager._persistence_task, 2)

    try:
        asyncio.run(scenario())
        assert manager.ingress_queue_saturations == 0
        assert manager.unsubscribed_market_frames_ignored == 1
        assert manager.unsubscribed_asset_counts == {"removed": 1}
        assert manager.order_books.books["yes"].last_exchange_timestamp_ms == (
            NOW_MS - 41
        )
        assert manager.ingress_frames_enqueued == 51
        assert manager.ingress_frames_dequeued == 50
    finally:
        temp.cleanup()


def test_delta_before_snapshot_closes_socket_and_requires_resync_warmup():
    temp, repo = make_repo()

    class FakeSocket:
        def __init__(self):
            self.sent = False
            self.closed = asyncio.Event()

        async def recv(self):
            if not self.sent:
                self.sent = True
                return json.dumps({
                    "event_type": "price_change", "timestamp": NOW_MS - 10,
                    "price_changes": [{
                        "asset_id": "yes", "side": "BUY", "price": "0.73",
                        "size": "11", "best_bid": "0.73", "best_ask": "0.74",
                    }],
                })
            await self.closed.wait()
            raise ConnectionError("socket closed for resync")

        async def close(self):
            self.closed.set()

    manager = MarketWebSocketManager(
        repo, stale_after_seconds=5, clock_ms=lambda: NOW_MS
    )
    manager.subscribed_asset_ids = ["yes", "no"]
    manager.status.status = "CONNECTED"
    manager._refresh_market_cache(["yes", "no"])

    async def scenario():
        try:
            await manager._run_ingress_pipeline(FakeSocket())
        except ConnectionError as exc:
            assert "MARKET_WS_BOOK_RESYNC:DELTA_BEFORE_SNAPSHOT" in str(exc)
        manager._stop.set()
        manager._persistence_event.set()
        if manager._persistence_task:
            await asyncio.wait_for(manager._persistence_task, 2)

    try:
        asyncio.run(scenario())
        assert manager.ingress_resyncs == 1
        assert manager.ingress_resync_reasons == {"DELTA_BEFORE_SNAPSHOT": 1}
        assert not manager.order_books.books["yes"].ready
        assert repo.get_state("strategy_readiness") == "NOT_READY"
    finally:
        temp.cleanup()


def test_latency_csv_detects_timestamp_units_and_iso8601():
    receive_ns = NOW_MS * 1_000_000
    cases = (
        (NOW_MS // 1_000, "unix_seconds"),
        (NOW_MS, "unix_milliseconds"),
        (NOW_MS * 1_000, "unix_microseconds"),
        ("2027-01-15T08:00:00+00:00", "iso8601"),
    )
    for raw, expected_unit in cases:
        raw_text, unit, normalized, value_ns, note = normalize_exchange_timestamp(
            raw, receive_wall_ns=receive_ns
        )
        assert raw_text == str(raw)
        assert unit == expected_unit
        assert normalized
        assert value_ns is not None
        assert "validated" in note


def test_latency_csv_is_bounded_and_stratified():
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "latency.csv"
        diagnostic = MarketWsLatencyCsvDiagnostic(
            path, duration_seconds=300, max_rows=6, stale_quota=3
        )
        diagnostic.start()
        for _index in range(10):
            diagnostic.submit({"exact_block_reason": "STALE"}, stale=True)
            diagnostic.submit({"exact_block_reason": "READY"}, stale=False)
        diagnostic.close()
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 6
        assert tuple(rows[0]) == CSV_FIELDS
        assert sum(row["exact_block_reason"] == "STALE" for row in rows) == 3
        assert sum(row["exact_block_reason"] == "READY" for row in rows) == 3


def test_market_ws_records_receive_and_processing_boundaries(monkeypatch):
    temp, repo = make_repo()
    csv_path = Path(temp.name) / "market-ws-latency.csv"
    monkeypatch.setenv("LIVE_MARKET_WS_LATENCY_CSV_PATH", str(csv_path))
    manager = MarketWebSocketManager(
        repo, stale_after_seconds=1, clock_ms=lambda: NOW_MS
    )
    manager.subscribed_asset_ids = ["yes", "no"]
    manager.status.status = "CONNECTED"
    manager._refresh_market_cache(["yes", "no"])
    assert manager._latency_csv is not None
    manager._latency_csv.start()
    timing = {
        "receive_wall_ns": (NOW_MS - 50) * 1_000_000,
        "receive_monotonic_ns": time.monotonic_ns(),
        "socket_receive_wall_ms": NOW_MS - 50,
        "frame_size_bytes": 512,
        "connection_id": "market-ws-test",
        "connection_generation": 1,
        "batch_index": 0,
        "batch_size": 1,
        "ingress_queue_depth": 2,
        "ws_internal_queue_depth": 3,
        "tcp_recv_q_bytes": 4,
        "occurred_after_reconnect": True,
        "occurred_after_resubscribe": True,
    }
    try:
        assert manager.process_message(book("yes", NOW_MS - 100), timing=timing)
        manager._latency_csv.close()
        with csv_path.open(newline="", encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        assert row["raw_exchange_timestamp"] == str(NOW_MS - 100)
        assert row["detected_timestamp_unit"] == "unix_milliseconds"
        assert float(row["transport_latency_ms"]) == 50.0
        assert float(row["queue_wait_ms"]) >= 0
        assert row["frame_size_bytes"] == "512"
        assert row["occurred_after_reconnect"] == "true"
    finally:
        manager._latency_csv.close()
        temp.cleanup()

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from decimal import Decimal

from live.order_book import OrderBookSet
from live.strategy import StrategyPolicy
from live.strategy_runtime import LiveStrategyRuntime


NOW_MS = 2_000_000_000_000


def snapshot(asset: str, timestamp: int, bid: str, ask: str) -> dict:
    return {
        "event_type": "book",
        "asset_id": asset,
        "timestamp": timestamp,
        "bids": [{"price": bid, "size": "10"}],
        "asks": [{"price": ask, "size": "10"}],
    }


def apply_fresh(books: OrderBookSet, message: dict, **kwargs):
    return books.apply(
        message, now_ms=NOW_MS, max_age_ms=1_000,
        future_tolerance_ms=0, **kwargs,
    )


def test_snapshot_delta_advertised_best_match_stays_ready():
    books = OrderBookSet(["token"])
    apply_fresh(books, snapshot("token", NOW_MS - 300, "0.72", "0.75"))
    frame = apply_fresh(books, {
        "event_type": "price_change",
        "timestamp": NOW_MS - 200,
        "price_changes": [{
            "asset_id": "token", "side": "SELL", "price": "0.74",
            "size": "8", "best_bid": "0.72", "best_ask": "0.74",
        }],
    })
    advertised = apply_fresh(books, {
        "event_type": "best_bid_ask", "asset_id": "token",
        "timestamp": NOW_MS - 199, "best_bid": "0.72", "best_ask": "0.74",
    })
    assert frame.updates[0]["readiness_reason"] == "READY"
    assert advertised.updates[0]["readiness_reason"] == "READY"
    assert books.event_ready(["token"]) == (True, "READY")


def test_073_074_075_preserves_exact_entry_transition():
    books = OrderBookSet(["token"])
    apply_fresh(books, {
        "event_type": "book", "asset_id": "token", "timestamp": NOW_MS - 300,
        "bids": [{"price": "0.72", "size": "10"}],
        "asks": [
            {"price": "0.73", "size": "10"},
            {"price": "0.74", "size": "10"},
            {"price": "0.75", "size": "10"},
        ],
    })
    frame = apply_fresh(books, {
        "event_type": "price_change", "timestamp": NOW_MS - 200,
        "price_changes": [
            {"asset_id": "token", "side": "SELL", "price": "0.73", "size": "0",
             "best_bid": "0.72", "best_ask": "0.74"},
            {"asset_id": "token", "side": "SELL", "price": "0.74", "size": "0",
             "best_bid": "0.72", "best_ask": "0.75"},
        ],
    })
    assert [item["best_ask"] for item in frame.top_transitions] == ["0.74", "0.75"]
    assert frame.updates[0]["best_ask"] == "0.75"
    assert frame.updates[0]["book_ready"]


def test_advertised_best_can_precede_older_timestamp_depth_without_false_mismatch():
    books = OrderBookSet(["token"])
    apply_fresh(books, snapshot("token", NOW_MS - 400, "0.35", "0.36"))
    pending = apply_fresh(books, {
        "event_type": "best_bid_ask", "asset_id": "token",
        "timestamp": NOW_MS - 200, "best_bid": "0.35", "best_ask": "0.37",
    })
    aligned = apply_fresh(books, {
        "event_type": "price_change", "timestamp": NOW_MS - 201,
        "price_changes": [
            {"asset_id": "token", "side": "SELL", "price": "0.36", "size": "0"},
            {"asset_id": "token", "side": "SELL", "price": "0.37", "size": "5",
             "best_bid": "0.35", "best_ask": "0.37"},
        ],
    })
    assert pending.updates[0]["readiness_reason"] == "BEST_PRICE_PENDING_DEPTH"
    assert aligned.updates[0]["readiness_reason"] == "READY"
    assert not books.books["token"].alignment_pending


def test_missing_depth_update_mismatch_no_order_then_resync_ready():
    books = OrderBookSet(["token"])
    apply_fresh(books, snapshot("token", NOW_MS - 400, "0.35", "0.36"))
    apply_fresh(books, {
        "event_type": "best_bid_ask", "asset_id": "token",
        "timestamp": NOW_MS - 200, "best_bid": "0.35", "best_ask": "0.37",
    })
    mismatch = apply_fresh(books, {
        "event_type": "price_change", "timestamp": NOW_MS - 199,
        "price_changes": [{
            "asset_id": "token", "side": "BUY", "price": "0.35", "size": "9",
        }],
    })
    assert mismatch.updates[0]["readiness_reason"] == "BEST_PRICE_MISMATCH"
    assert books.event_ready(["token"]) == (False, "BEST_PRICE_MISMATCH")
    books.mark_not_ready("RESYNC", source_generation=2)
    recovered = apply_fresh(
        books, snapshot("token", NOW_MS - 100, "0.35", "0.37"),
        source_generation=2,
    )
    assert recovered.updates[0]["book_ready"]
    assert books.event_ready(["token"]) == (True, "READY")


def test_size_zero_removes_best_and_recomputes_next_level():
    books = OrderBookSet(["token"])
    apply_fresh(books, {
        "event_type": "book", "asset_id": "token", "timestamp": NOW_MS - 300,
        "bids": [{"price": "0.70", "size": "3"}, {"price": "0.69", "size": "4"}],
        "asks": [{"price": "0.74", "size": "3"}, {"price": "0.75", "size": "4"}],
    })
    removed = apply_fresh(books, {
        "event_type": "price_change", "timestamp": NOW_MS - 200,
        "price_changes": [{
            "asset_id": "token", "side": "SELL", "price": "0.74", "size": "0",
            "best_bid": "0.70", "best_ask": "0.75",
        }],
    })
    assert removed.updates[0]["best_ask"] == "0.75"
    assert Decimal("0.74") not in books.books["token"].asks


def test_reconnect_generation_rejects_late_old_frame():
    books = OrderBookSet(["token"])
    books.mark_not_ready("CONNECT", source_generation=1)
    apply_fresh(
        books, snapshot("token", NOW_MS - 300, "0.70", "0.74"),
        source_generation=1, wire_sequence=1,
    )
    books.mark_not_ready("RECONNECT", source_generation=2)
    apply_fresh(
        books, snapshot("token", NOW_MS - 200, "0.71", "0.75"),
        source_generation=2, wire_sequence=2,
    )
    late = apply_fresh(books, {
        "event_type": "price_change", "timestamp": NOW_MS - 100,
        "price_changes": [{
            "asset_id": "token", "side": "SELL", "price": "0.60", "size": "1",
            "best_bid": "0.70", "best_ask": "0.60",
        }],
    }, source_generation=1, wire_sequence=3)
    assert late.rejected_reason == "STALE_CONNECTION_GENERATION"
    assert books.books["token"].best_ask == Decimal("0.75")


def test_subscription_rollover_removes_previous_market_levels():
    books = OrderBookSet(["old"])
    apply_fresh(books, snapshot("old", NOW_MS - 300, "0.70", "0.74"))
    books.ensure_assets(["new"])
    assert "old" not in books.books
    assert books.books["new"].bids == {}
    assert books.books["new"].asks == {}
    assert not books.books["new"].snapshot_loaded


def test_out_of_order_depth_frame_is_rejected():
    books = OrderBookSet(["token"])
    apply_fresh(books, snapshot("token", NOW_MS - 200, "0.70", "0.74"))
    frame = apply_fresh(books, {
        "event_type": "price_change", "timestamp": NOW_MS - 201,
        "price_changes": [{
            "asset_id": "token", "side": "SELL", "price": "0.73", "size": "1",
            "best_bid": "0.70", "best_ask": "0.73",
        }],
    })
    assert frame.out_of_order
    assert frame.rejected_reason == "OUT_OF_ORDER_EXCHANGE_TIMESTAMP"
    assert books.books["token"].best_ask == Decimal("0.74")


def test_duplicate_frame_is_ignored_without_mutation():
    books = OrderBookSet(["token"])
    message = snapshot("token", NOW_MS - 200, "0.70", "0.74")
    first = apply_fresh(books, message)
    update_number = books.books["token"].update_number
    duplicate = apply_fresh(books, message)
    assert first.updates
    assert duplicate.duplicate
    assert duplicate.updates == ()
    assert books.books["token"].update_number == update_number


def test_rapid_burst_of_hundreds_of_updates_preserves_every_mutation():
    books = OrderBookSet(["token"])
    apply_fresh(books, snapshot("token", NOW_MS - 900, "0.70", "0.74"))
    for index in range(500):
        frame = apply_fresh(books, {
            "event_type": "price_change", "timestamp": NOW_MS - 899 + index,
            "price_changes": [{
                "asset_id": "token", "side": "BUY", "price": "0.70",
                "size": str(index + 1), "best_bid": "0.70", "best_ask": "0.74",
            }],
        })
        assert frame.updates[0]["book_ready"]
    assert books.books["token"].bids[Decimal("0.70")] == Decimal("500")
    assert books.books["token"].update_number == 501


class ActiveTask:
    def done(self) -> bool:
        return False


def runtime_fixture() -> LiveStrategyRuntime:
    runtime = LiveStrategyRuntime.__new__(LiveStrategyRuntime)
    runtime.policy = StrategyPolicy()
    runtime._pending_frames = OrderedDict()
    runtime._critical_frames = deque()
    runtime._critical_price_state = {}
    runtime._critical_observed_price_state = {}
    runtime._frame_queue_capacity = 32
    runtime._frame_task = ActiveTask()
    runtime._frame_event = asyncio.Event()
    runtime.frames_coalesced = 0
    runtime.frames_dropped = 0
    runtime.critical_triggers_queued = 0
    runtime.critical_triggers_processed = 0
    runtime.critical_triggers_dropped = 0
    runtime.max_critical_queue_depth = 0
    runtime.enabled = lambda: True
    runtime._observe_entry_trigger = lambda _context: None
    return runtime


def strategy_frame(*, ask: str, bid: str, ready: bool, reason: str, number: int,
                   correlation_id: str) -> dict:
    return {
        "event_type": "best_bid_ask", "message_hash": f"frame-{number}",
        "received_at": "2033-05-18T03:33:20+00:00",
        "updates": [{
            "condition_id": "condition", "event_id": "event", "asset_id": "token",
            "outcome": "YES", "best_ask": ask, "best_bid": bid,
            "exchange_timestamp_ms": NOW_MS - 100, "update_number": number,
            "correlation_id": correlation_id,
        }],
        "event_readiness": {"condition": {"ready": ready, "reason": reason}},
    }


def test_entry_074_during_resync_is_blocked_then_actionable_when_ready():
    async def scenario():
        runtime = runtime_fixture()
        runtime.schedule_frame(strategy_frame(
            ask="0.73", bid="0.72", ready=True, reason="READY", number=1,
            correlation_id="baseline",
        ))
        runtime.schedule_frame(strategy_frame(
            ask="0.74", bid="0.72", ready=False,
            reason="BEST_PRICE_PENDING_DEPTH", number=2, correlation_id="sync-074",
        ))
        runtime.schedule_frame(strategy_frame(
            ask="0.74", bid="0.72", ready=True, reason="READY", number=3,
            correlation_id="sync-074",
        ))
        critical = list(runtime._critical_frames)
        assert len(critical) == 2
        assert not critical[0]["event_readiness"]["condition"]["ready"]
        assert critical[1]["event_readiness"]["condition"]["ready"]
        assert [item["_critical_trigger_id"] for item in (
            critical[0]["updates"][0], critical[1]["updates"][0]
        )] == ["sync-074", "sync-074"]
    asyncio.run(scenario())


def test_stop_066_during_mismatch_remains_fail_closed_then_rearms_ready():
    async def scenario():
        runtime = runtime_fixture()
        runtime.schedule_frame(strategy_frame(
            ask="0.75", bid="0.67", ready=True, reason="READY", number=1,
            correlation_id="baseline-stop",
        ))
        runtime.schedule_frame(strategy_frame(
            ask="0.75", bid="0.66", ready=False, reason="BEST_PRICE_MISMATCH",
            number=2, correlation_id="stop-sync",
        ))
        runtime.schedule_frame(strategy_frame(
            ask="0.75", bid="0.66", ready=True, reason="READY", number=3,
            correlation_id="stop-sync",
        ))
        critical = list(runtime._critical_frames)
        assert [item["event_readiness"]["condition"]["ready"] for item in critical] == [
            False, True,
        ]
        assert all("STOP_066" in item["_critical_trigger_types"] for item in critical)
    asyncio.run(scenario())


def test_emergency_060_during_mismatch_remains_fail_closed_then_rearms_ready():
    async def scenario():
        runtime = runtime_fixture()
        runtime.schedule_frame(strategy_frame(
            ask="0.75", bid="0.67", ready=True, reason="READY", number=1,
            correlation_id="baseline-emergency",
        ))
        runtime.schedule_frame(strategy_frame(
            ask="0.75", bid="0.60", ready=False, reason="BEST_PRICE_MISMATCH",
            number=2, correlation_id="emergency-sync",
        ))
        runtime.schedule_frame(strategy_frame(
            ask="0.75", bid="0.60", ready=True, reason="READY", number=3,
            correlation_id="emergency-sync",
        ))
        critical = list(runtime._critical_frames)
        assert [item["event_readiness"]["condition"]["ready"] for item in critical] == [
            False, True,
        ]
        assert all("EMERGENCY_060" in item["_critical_trigger_types"] for item in critical)
    asyncio.run(scenario())


def test_freshness_over_1000ms_blocks_exact_entry():
    books = OrderBookSet(["token"])
    stale = apply_fresh(
        books, snapshot("token", NOW_MS - 1_001, "0.73", "0.74")
    )
    assert stale.rejected_reason == "STALE_EXCHANGE_TIMESTAMP"
    assert books.event_ready(["token"]) == (False, "STALE_EXCHANGE_TIMESTAMP")


def test_freshness_at_or_below_1000ms_ready_book_can_continue():
    books = OrderBookSet(["token"])
    fresh = apply_fresh(
        books, snapshot("token", NOW_MS - 1_000, "0.73", "0.74")
    )
    assert fresh.updates[0]["book_ready"]
    assert fresh.assets_at_exact_ask(Decimal("0.74")) == ("token",)
    assert books.event_ready(["token"]) == (True, "READY")

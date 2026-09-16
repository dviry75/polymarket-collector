from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

from live.adapters.mock import MockTradingAdapter
from live.config import LiveConfig
from live.repository import LiveRepository
from live.stop_obligations import StopObligation
from live.strategy_repository import StrategyRepository
from live.strategy_runtime import LiveStrategyRuntime


def _runtime(tmp_path: Path) -> tuple[StrategyRepository, LiveStrategyRuntime, dict]:
    base = LiveRepository(tmp_path / "live.sqlite3")
    base.migrate()
    repo = StrategyRepository(base)
    repo.migrate()
    event_id = "event-stop-mailbox"
    condition_id = "condition-stop-mailbox"
    token_id = "token-stop-mailbox"
    base.upsert_market({
        "event_id": event_id,
        "condition_id": condition_id,
        "yes_token_id": token_id,
        "no_token_id": "other-token",
        "token_mapping_status": "verified",
        "accepting_orders": True,
        "min_order_size": "1",
        "min_tick_size": "0.01",
        "taker_base_fee": 0,
    })
    repo.reserve_event_entry(
        event_id=event_id,
        condition_id=condition_id,
        token_id=token_id,
        side="YES",
        simultaneous=False,
        reason_code="ENTRY_PRICE_EXACT",
    )
    position = repo.open_position(
        event_id=event_id,
        condition_id=condition_id,
        token_id=token_id,
        outcome="YES",
        shares=Decimal("8"),
        average_price=Decimal("0.74"),
        cost_all_in=Decimal("5.92"),
        fees=Decimal("0"),
        sellable_shares=Decimal("8"),
        min_sellable=Decimal("1"),
    )
    config = LiveConfig(
        live_module_enabled=True,
        execution_mode="PAPER_TRADING",
        paper_trading_enabled=True,
        stop_forensic_db_path=str(tmp_path / "forensics.sqlite3"),
    )
    runtime = LiveStrategyRuntime(
        config, base, repo, MockTradingAdapter()
    )
    return repo, runtime, position


def _update(position: dict, *, generation: int = 4, number: int = 7) -> dict:
    return {
        "event_id": position["event_id"],
        "condition_id": position["condition_id"],
        "asset_id": position["token_id"],
        "best_bid": "0.66",
        "best_bid_size": "8",
        "book_ready": True,
        "generation": generation,
        "update_number": number,
        "exchange_timestamp_ms": 1_800_000_000_000,
        "_critical_trigger_id": "trigger-stop-mailbox",
    }


def test_obligation_requires_ready_integral_monotonic_book() -> None:
    position = {
        "position_id": "p",
        "event_id": "e",
        "condition_id": "c",
        "token_id": "t",
    }
    update = {
        "asset_id": "t",
        "best_bid": "0.66",
        "generation": 1,
        "update_number": 1,
        "book_ready": True,
    }
    assert StopObligation.create(
        position=position,
        update=update,
        readiness={"ready": True, "reason": "READY"},
        frame_hash="f",
        stop_price=Decimal("0.66"),
    ) is not None
    assert StopObligation.create(
        position=position,
        update=update,
        readiness={"ready": False, "reason": "BOOK_MISMATCH"},
        frame_hash="f",
        stop_price=Decimal("0.66"),
    ) is None
    assert StopObligation.create(
        position=position,
        update={**update, "generation": 0},
        readiness={"ready": True, "reason": "READY"},
        frame_hash="f",
        stop_price=Decimal("0.66"),
    ) is None


def test_mailbox_latches_while_event_lock_is_held(tmp_path) -> None:
    repo, runtime, position = _runtime(tmp_path)

    async def scenario() -> None:
        await runtime._refresh_hot_state_once()
        event_lock = runtime._event_locks.setdefault(
            position["event_id"], asyncio.Lock()
        )
        await event_lock.acquire()
        try:
            assert runtime._enqueue_stop_obligation(
                position,
                _update(position),
                {"ready": True, "reason": "READY"},
                frame_hash="frame-7",
            )
            for _ in range(100):
                current = repo.position_for_token(position["token_id"])
                if (
                    int(current["stop_stage"]) >= 1
                    and runtime.stop_obligations_latched == 1
                ):
                    break
                await asyncio.sleep(0.01)
            assert int(current["stop_stage"]) == 1
            assert runtime.stop_obligations_latched == 1
            assert event_lock.locked()
        finally:
            event_lock.release()
            await runtime.stop()

    asyncio.run(scenario())


def test_mailbox_coalesces_newer_per_position_and_latch_is_idempotent(tmp_path) -> None:
    repo, runtime, position = _runtime(tmp_path)

    async def scenario() -> None:
        await runtime._refresh_hot_state_once()
        # Prevent lazy worker startup until both records are in the mailbox.
        runtime._stop_obligation_task = asyncio.current_task()
        assert runtime._enqueue_stop_obligation(
            position,
            _update(position, number=7),
            {"ready": True, "reason": "READY"},
            frame_hash="frame-7",
        )
        assert runtime._enqueue_stop_obligation(
            position,
            _update(position, number=8),
            {"ready": True, "reason": "READY"},
            frame_hash="frame-8",
        )
        assert len(runtime._stop_obligations) == 1
        assert runtime.stop_obligations_coalesced == 1
        runtime._stop_obligation_task = asyncio.create_task(
            runtime._stop_obligation_worker()
        )
        for _ in range(100):
            if runtime.stop_obligations_latched == 1:
                break
            await asyncio.sleep(0.01)
        current = repo.position_for_token(position["token_id"])
        assert int(current["stop_stage"]) == 1
        again = repo.latch_stop_exit(position["position_id"])
        assert again["_newly_latched"] is False
        await runtime.stop()

    asyncio.run(scenario())


def test_not_ready_obligation_is_rejected_without_latching(tmp_path) -> None:
    repo, runtime, position = _runtime(tmp_path)

    async def scenario() -> None:
        await runtime._refresh_hot_state_once()
        assert not runtime._enqueue_stop_obligation(
            position,
            _update(position),
            {"ready": False, "reason": "BOOK_MISMATCH"},
            frame_hash="bad-frame",
        )
        await asyncio.sleep(0)
        current = repo.position_for_token(position["token_id"])
        assert int(current["stop_stage"]) == 0
        assert runtime.stop_obligations_rejected == 1
        await runtime.stop()

    asyncio.run(scenario())


def test_sqlite_writer_contention_retries_without_blocking_event_loop(tmp_path) -> None:
    repo, runtime, position = _runtime(tmp_path)

    async def scenario() -> None:
        await runtime._refresh_hot_state_once()
        blocker = repo.base.connect()
        blocker.execute("BEGIN IMMEDIATE")
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(40):
                ticks += 1
                await asyncio.sleep(0.01)

        try:
            assert runtime._enqueue_stop_obligation(
                position,
                _update(position),
                {"ready": True, "reason": "READY"},
                frame_hash="contended-frame",
            )
            await ticker()
            assert ticks == 40
            assert runtime.stop_obligation_failures >= 1
            assert int(
                repo.position_for_token(position["token_id"])["stop_stage"]
            ) == 0
        finally:
            blocker.rollback()
            blocker.close()

        for _ in range(150):
            if runtime.stop_obligations_latched == 1:
                break
            await asyncio.sleep(0.01)
        assert runtime.stop_obligations_latched == 1
        assert int(
            repo.position_for_token(position["token_id"])["stop_stage"]
        ) == 1
        await runtime.stop()

    asyncio.run(scenario())


def test_slow_entry_worker_cannot_delay_stop_latch(tmp_path) -> None:
    repo, runtime, position = _runtime(tmp_path)

    async def scenario() -> None:
        await runtime._refresh_hot_state_once()
        entry_started = asyncio.Event()
        release_entry = asyncio.Event()

        async def slow_entry(_context: dict) -> None:
            entry_started.set()
            await release_entry.wait()

        runtime.process_atomic_frame = slow_entry
        assert runtime._enqueue_entry_frame({
            "_critical_trigger_id": "slow-entry",
            "_entry_phase_timing": {
                "enqueued_monotonic": __import__("time").perf_counter(),
            },
        })
        await asyncio.wait_for(entry_started.wait(), timeout=1)
        started = asyncio.get_running_loop().time()
        assert runtime._enqueue_stop_obligation(
            position,
            _update(position),
            {"ready": True, "reason": "READY"},
            frame_hash="stop-during-slow-entry",
        )
        for _ in range(100):
            if runtime.stop_obligations_latched == 1:
                break
            await asyncio.sleep(0.01)
        elapsed = asyncio.get_running_loop().time() - started
        assert runtime.stop_obligations_latched == 1
        assert elapsed < 0.5
        assert not release_entry.is_set()
        assert int(
            repo.position_for_token(position["token_id"])["stop_stage"]
        ) == 1
        release_entry.set()
        await runtime.stop()

    asyncio.run(scenario())

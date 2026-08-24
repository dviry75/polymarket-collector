import asyncio
import tempfile
from pathlib import Path

import pytest

from live.adapters.mock import MockTradingAdapter
from live.reconciliation import (
    RECONCILIATION_BACKOFF_CAP_SECONDS,
    ReconciliationWorker,
)
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository


def build_repo():
    temporary = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temporary.name) / "live.sqlite3")
    base.migrate()
    strategy = StrategyRepository(base)
    strategy.migrate(pause_entries_default=False)
    base.upsert_market({
        "event_id": "event",
        "condition_id": "condition",
        "yes_token_id": "yes",
        "no_token_id": "no",
        "token_mapping_status": "verified",
        "accepting_orders": True,
        "min_order_size": "1",
        "min_tick_size": "0.01",
    })
    return temporary, base, strategy


def reconciliation_rows(base):
    with base.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT id,finished_at,status,error FROM live_reconciliation_runs ORDER BY id"
            ).fetchall()
        ]


def test_cancelled_during_remote_fetch_is_terminal_and_propagates():
    temporary, base, strategy = build_repo()

    class BlockingAdapter(MockTradingAdapter):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def get_positions(self):
            self.entered.set()
            await self.release.wait()
            return []

    async def scenario():
        adapter = BlockingAdapter()
        task = asyncio.create_task(
            ReconciliationWorker(base, adapter, strategy).run_once("test-fetch")
        )
        await asyncio.wait_for(adapter.entered.wait(), 1)
        assert reconciliation_rows(base)[-1]["status"] == "running"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(scenario())
        row = reconciliation_rows(base)[-1]
        assert row["status"] == "failed"
        assert row["finished_at"]
        assert row["error"] == "CANCELLED_RECONCILIATION_TASK"
    finally:
        temporary.cleanup()


def test_cancelled_during_intent_processing_is_terminal():
    temporary, base, strategy = build_repo()

    class BlockingOrderAdapter(MockTradingAdapter):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def get_order(self, order_id):
            self.entered.set()
            await self.release.wait()
            return None

    reserved = strategy.reserve_event_entry(
        event_id="event",
        condition_id="condition",
        token_id="yes",
        side="YES",
        simultaneous=False,
        reason_code="TEST",
    )
    strategy.update_intent(
        reserved["entry_intent_id"],
        remote_order_id="remote-order",
    )

    async def scenario():
        adapter = BlockingOrderAdapter()
        task = asyncio.create_task(
            ReconciliationWorker(base, adapter, strategy).run_once("test-processing")
        )
        await asyncio.wait_for(adapter.entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(scenario())
        row = reconciliation_rows(base)[-1]
        assert row["status"] == "failed"
        assert row["finished_at"]
        assert row["error"] == "CANCELLED_RECONCILIATION_TASK"
    finally:
        temporary.cleanup()


def test_finish_reconciliation_is_idempotent():
    temporary, base, _strategy = build_repo()
    try:
        run_id = base.start_reconciliation()
        assert base.finish_reconciliation(run_id, "ok", []) is True
        first = reconciliation_rows(base)[-1]
        assert base.finish_reconciliation(
            run_id, "failed", [{"type": "late"}], "late"
        ) is False
        assert reconciliation_rows(base)[-1] == first
    finally:
        temporary.cleanup()


def test_backoff_saturates_before_large_exponent_conversion():
    temporary, base, _strategy = build_repo()
    try:
        worker = ReconciliationWorker(base, MockTradingAdapter())
        worker._consecutive_retries = 10_000
        delay = worker._schedule_backoff("test", "persistent")
        assert 0 < delay <= RECONCILIATION_BACKOFF_CAP_SECONDS
        assert worker._consecutive_retries == 10_001
    finally:
        temporary.cleanup()


def test_normal_success_remains_terminal_ok():
    temporary, base, strategy = build_repo()
    try:
        result = asyncio.run(
            ReconciliationWorker(
                base, MockTradingAdapter(), strategy
            ).run_once("test-success")
        )
        assert result["status"] == "ok"
        row = reconciliation_rows(base)[-1]
        assert row["status"] == "ok"
        assert row["finished_at"]
    finally:
        temporary.cleanup()

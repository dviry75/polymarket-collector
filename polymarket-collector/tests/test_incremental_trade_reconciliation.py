"""Focused coverage for watermark-driven, bounded account-trade reconciliation.

The property under test throughout: reconciliation's remote read must cost in
proportion to *new* account activity, while still never losing a trade -- across
restarts, mid-pagination failures, API anomalies and late arrivals.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from live.adapters.mock import MockTradingAdapter
from live.reconciliation import ReconciliationWorker
from live.trade_window import (
    STATE_SLICE_SECONDS,
    STATE_WATERMARK_AT,
    STATE_WATERMARK_TRADE_ID,
    TradeWindowExhaustedError,
    TradeWindowPolicy,
    bootstrap_watermark,
    fetch_trade_window,
    next_watermark_state,
    plan_trade_window,
)
from test_live_full_strategy import build_repo, reserve_and_open


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _trade(trade_id: str, order_id: str, matched_at: datetime, *, size="1", price="0.60"):
    return {
        "polymarket_trade_id": trade_id,
        "polymarket_order_id": order_id,
        "condition_id": "condition-window",
        "token_id": "token-window",
        "side": "sell",
        "price": price,
        "size": size,
        "fee": "0",
        "fee_rate_bps": "0",
        "fee_source": "polymarket_fee_rate_bps",
        "fee_verification_status": "VERIFIED",
        "status": "matched",
        "matched_at": _iso(matched_at),
        "transaction_hash": f"tx-{trade_id}",
        "liquidity_role": "TAKER",
        "maker_order_ids": [],
        "raw_message": {},
    }


class WindowedAdapter(MockTradingAdapter):
    """Server-side ``after``/``before`` filtering plus fixed-size pagination.

    Records every page request so tests can assert on request counts rather
    than only on the resulting data.
    """

    def __init__(self, trades=(), *, page_size=100, fail_on_page=None):
        super().__init__()
        self.trade_book = list(trades)
        self.page_size = page_size
        self.fail_on_page = fail_on_page
        self.page_requests = []
        self.full_history_calls = 0

    async def get_trades(self):
        self.full_history_calls += 1
        return list(self.trade_book)

    def _visible(self, after, before):
        selected = []
        for trade in self.trade_book:
            matched = datetime.fromisoformat(
                str(trade["matched_at"]).replace("Z", "+00:00")
            )
            epoch = matched.timestamp()
            if after is not None and epoch < float(after):
                continue
            if before is not None and epoch > float(before):
                continue
            selected.append(trade)
        # Newest first, as the CLOB trade feed reports.
        return sorted(selected, key=lambda item: item["matched_at"], reverse=True)

    async def get_trades_page(self, *, after=None, before=None, cursor=None):
        self.page_requests.append({"after": after, "before": before, "cursor": cursor})
        if self.fail_on_page is not None and len(self.page_requests) == self.fail_on_page:
            raise TimeoutError("account trades page timed out")
        selected = self._visible(after, before)
        offset = int(cursor) if cursor else 0
        page = selected[offset:offset + self.page_size]
        has_more = offset + self.page_size < len(selected)
        return {
            "trades": page,
            "has_more": has_more,
            "next_cursor": str(offset + self.page_size) if has_more else None,
        }


class StuckCursorAdapter(MockTradingAdapter):
    """An API anomaly: always more pages, always the same cursor."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    async def get_trades(self):
        return []

    async def get_trades_page(self, *, after=None, before=None, cursor=None):
        self.calls += 1
        return {"trades": [], "has_more": True, "next_cursor": "SAME"}


def _exit_intent(strategy, position, order_id, *, shares=Decimal("1")):
    intent = strategy.reserve_position_intent(
        position, action="EXIT", purpose="STOP_066", order_type="GTC",
        shares=shares, price_limit=Decimal("0.55"), book_hash="window-test",
    )
    strategy.update_intent(intent["intent_id"], remote_order_id=order_id)
    return strategy.intent(intent["intent_id"])


def _persist_trade(strategy, intent_id, trade):
    return strategy.add_fill(
        intent_id=intent_id,
        remote_trade_id=trade["polymarket_trade_id"],
        shares=Decimal(str(trade["size"])),
        price=Decimal(str(trade["price"])),
        fee=Decimal("0"),
        status="MATCHED",
        matched_at=trade["matched_at"],
        raw=trade["raw_message"],
    )


# --------------------------------------------------------------------------
# Window planning and watermark arithmetic
# --------------------------------------------------------------------------

def test_overlap_is_applied_and_never_a_bare_last_timestamp():
    policy = TradeWindowPolicy(overlap_seconds=300.0)
    watermark = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    plan = plan_trade_window(
        watermark_at=watermark, slice_seconds=None, policy=policy,
        now=watermark + timedelta(minutes=1),
    )
    assert plan.after_at == watermark - timedelta(seconds=300)
    assert plan.before_at is None
    assert plan.after_epoch == int((watermark - timedelta(seconds=300)).timestamp())


def test_bootstrap_prefers_local_history_over_a_full_refetch():
    policy = TradeWindowPolicy(overlap_seconds=120.0)
    latest = "2026-08-30T12:00:00Z"
    watermark, source = bootstrap_watermark(latest, policy)
    assert source == "local_fill_history"
    assert watermark == datetime(2026, 8, 30, 11, 58, tzinfo=timezone.utc)

    empty_watermark, empty_source = bootstrap_watermark(None, policy)
    assert empty_watermark is None
    assert empty_source == "empty_database_bootstrap"
    assert plan_trade_window(
        watermark_at=empty_watermark, slice_seconds=None, policy=policy
    ).bootstrap is True


# --------------------------------------------------------------------------
# Fetch guardrails
# --------------------------------------------------------------------------

def test_normal_incremental_fetch_reads_only_the_window():
    now = datetime.now(timezone.utc)
    history = [
        _trade(f"old-{index}", "order-old", now - timedelta(days=10, minutes=index))
        for index in range(500)
    ]
    recent = [
        _trade("T-60", "order-new", now - timedelta(seconds=60)),
        _trade("T", "order-new", now),
        _trade("T+10", "order-new", now + timedelta(seconds=10)),
        _trade("T+20", "order-new", now + timedelta(seconds=20)),
    ]
    adapter = WindowedAdapter(history + recent, page_size=100)
    policy = TradeWindowPolicy(overlap_seconds=300.0)

    plan = plan_trade_window(
        watermark_at=now - timedelta(seconds=30), slice_seconds=None, policy=policy
    )
    result = asyncio.run(fetch_trade_window(adapter, plan, policy))

    assert {trade["polymarket_trade_id"] for trade in result.trades} == {
        "T-60", "T", "T+10", "T+20"
    }
    # The 500 historical trades are never transferred, parsed, or paged.
    assert result.remote_count == 4
    assert result.pages == 1
    assert adapter.full_history_calls == 0


def test_same_timestamp_trades_are_both_kept():
    moment = datetime.now(timezone.utc)
    adapter = WindowedAdapter([
        _trade("same-a", "order-a", moment),
        _trade("same-b", "order-b", moment),
    ])
    policy = TradeWindowPolicy()
    plan = plan_trade_window(
        watermark_at=moment - timedelta(seconds=5), slice_seconds=None, policy=policy
    )
    result = asyncio.run(fetch_trade_window(adapter, plan, policy))
    assert sorted(
        trade["polymarket_trade_id"] for trade in result.trades
    ) == ["same-a", "same-b"]


def test_multi_page_window_is_fully_drained():
    now = datetime.now(timezone.utc)
    trades = [
        _trade(f"page-{index}", "order-page", now - timedelta(seconds=index))
        for index in range(250)
    ]
    adapter = WindowedAdapter(trades, page_size=100)
    policy = TradeWindowPolicy(overlap_seconds=600.0)
    plan = plan_trade_window(
        watermark_at=now - timedelta(seconds=1), slice_seconds=None, policy=policy
    )
    result = asyncio.run(fetch_trade_window(adapter, plan, policy))

    assert result.pages == 3
    assert len(result.trades) == 250
    assert not result.truncated


def test_repeated_cursor_cannot_become_a_request_storm():
    adapter = StuckCursorAdapter()
    policy = TradeWindowPolicy(max_pages=50)
    plan = plan_trade_window(
        watermark_at=datetime.now(timezone.utc), slice_seconds=None, policy=policy
    )
    result = asyncio.run(fetch_trade_window(adapter, plan, policy))

    assert result.limit_reason == "repeated_cursor"
    assert result.truncated
    # Two requests total: the first page, then the page its cursor pointed at.
    assert adapter.calls == 2


def test_page_and_trade_caps_bound_a_single_run():
    now = datetime.now(timezone.utc)
    trades = [
        _trade(f"cap-{index}", "order-cap", now - timedelta(seconds=index))
        for index in range(5_000)
    ]
    adapter = WindowedAdapter(trades, page_size=100)
    policy = TradeWindowPolicy(overlap_seconds=10_000.0, max_pages=5, max_trades=2_500)
    plan = plan_trade_window(
        watermark_at=now, slice_seconds=None, policy=policy
    )
    result = asyncio.run(fetch_trade_window(adapter, plan, policy))

    assert result.pages == 5
    assert result.limit_reason == "max_pages"
    assert len(adapter.page_requests) == 5


def test_time_budget_stops_a_slow_feed():
    now = datetime.now(timezone.utc)
    adapter = WindowedAdapter(
        [_trade(f"slow-{i}", "order-slow", now - timedelta(seconds=i)) for i in range(400)],
        page_size=10,
    )
    clock = iter([0.0, 0.0, 0.5, 1.0, 99.0, 99.0, 99.0])
    policy = TradeWindowPolicy(overlap_seconds=10_000.0, time_budget_seconds=5.0)
    plan = plan_trade_window(watermark_at=now, slice_seconds=None, policy=policy)
    result = asyncio.run(
        fetch_trade_window(adapter, plan, policy, monotonic=lambda: next(clock))
    )
    assert result.limit_reason == "time_budget"
    assert result.truncated


# --------------------------------------------------------------------------
# Watermark advancement
# --------------------------------------------------------------------------

def test_truncated_window_narrows_instead_of_advancing():
    now = datetime.now(timezone.utc)
    policy = TradeWindowPolicy(overlap_seconds=0.0, min_slice_seconds=60.0)
    plan = plan_trade_window(
        watermark_at=now - timedelta(hours=4), slice_seconds=None, policy=policy, now=now
    )
    result = asyncio.run(fetch_trade_window(WindowedAdapter([]), plan, policy))
    result.truncated = True
    result.limit_reason = "max_pages"

    advanced = next_watermark_state(plan, result, policy)
    assert STATE_WATERMARK_AT not in advanced
    assert float(advanced[STATE_SLICE_SECONDS]) == pytest.approx(2 * 3600, rel=0.01)


def test_narrowing_bottoms_out_loudly_rather_than_skipping_trades():
    now = datetime.now(timezone.utc)
    policy = TradeWindowPolicy(overlap_seconds=0.0, min_slice_seconds=60.0)
    plan = plan_trade_window(
        watermark_at=now - timedelta(hours=1), slice_seconds=60.0, policy=policy, now=now
    )
    result = asyncio.run(fetch_trade_window(WindowedAdapter([]), plan, policy))
    result.truncated = True
    result.limit_reason = "max_trades"

    with pytest.raises(TradeWindowExhaustedError):
        next_watermark_state(plan, result, policy)


def test_drained_slice_advances_to_the_slice_edge_even_when_empty():
    now = datetime.now(timezone.utc)
    policy = TradeWindowPolicy(overlap_seconds=0.0)
    plan = plan_trade_window(
        watermark_at=now - timedelta(hours=4), slice_seconds=600.0, policy=policy, now=now
    )
    result = asyncio.run(fetch_trade_window(WindowedAdapter([]), plan, policy))

    advanced = next_watermark_state(plan, result, policy)
    assert advanced[STATE_WATERMARK_AT] == plan.before_at.isoformat()
    assert advanced[STATE_SLICE_SECONDS] == ""


# --------------------------------------------------------------------------
# End-to-end through ReconciliationWorker
# --------------------------------------------------------------------------

def _worker(base, strategy, adapter, **policy_kwargs):
    return ReconciliationWorker(
        base, adapter, strategy,
        trade_window=TradeWindowPolicy(**policy_kwargs),
    )


def test_reconciliation_persists_new_trades_once_across_repeated_runs():
    temp, base, strategy = build_repo()
    try:
        now = datetime.now(timezone.utc)
        position = reserve_and_open(strategy, event="window-dedup", shares=Decimal("3"))
        intent = _exit_intent(strategy, position, "order-dedup", shares=Decimal("3"))
        trade = _trade("dedup-1", "order-dedup", now, size="1")
        adapter = WindowedAdapter([trade])
        worker = _worker(base, strategy, adapter, overlap_seconds=300.0)

        for _ in range(3):
            asyncio.run(worker.run_once("test", force=True))

        with base.connect() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) AS n FROM live_strategy_fills WHERE remote_trade_id=?",
                ("dedup-1",),
            ).fetchone()
        assert rows["n"] == 1
        assert strategy.intent(intent["intent_id"]) is not None
    finally:
        temp.cleanup()


def test_watermark_survives_restart_and_avoids_a_full_history_scan():
    temp, base, strategy = build_repo()
    try:
        now = datetime.now(timezone.utc)
        position = reserve_and_open(strategy, event="window-restart", shares=Decimal("3"))
        _exit_intent(strategy, position, "order-restart", shares=Decimal("3"))
        history = [
            _trade(f"hist-{index}", "order-restart", now - timedelta(days=5, minutes=index))
            for index in range(300)
        ]
        fresh = _trade("fresh-1", "order-restart", now)
        adapter = WindowedAdapter(history + [fresh], page_size=100)

        first = _worker(base, strategy, adapter, overlap_seconds=300.0)
        # First ever run on an empty DB: an explicit, budgeted bootstrap sweep.
        asyncio.run(first.run_once("test", force=True))
        assert first._last_trade_fetch["trade_fetch_mode"] == "bootstrap"
        watermark = base.get_state(STATE_WATERMARK_AT)
        assert watermark
        assert base.get_state(STATE_WATERMARK_TRADE_ID) == "fresh-1"

        # A restart drops every in-memory worker field; only the DB survives.
        adapter.page_requests.clear()
        restarted = _worker(base, strategy, adapter, overlap_seconds=300.0)
        assert restarted._last_trade_fetch == {}
        asyncio.run(restarted.run_once("test", force=True))

        # The watermark came back from the DB alone: one page, no history scan.
        assert restarted._last_trade_fetch["trade_fetch_mode"] == "incremental"
        assert adapter.full_history_calls == 0
        assert len(adapter.page_requests) == 1
        assert restarted._last_trade_fetch["trade_fetch_remote_count"] <= 2
        assert base.get_state(STATE_WATERMARK_AT) >= watermark
        with base.connect() as conn:
            persisted = conn.execute(
                "SELECT COUNT(*) AS n FROM live_strategy_fills"
            ).fetchone()["n"]
        # Everything the bootstrap read stayed persisted; nothing was lost and
        # nothing was written twice.
        assert persisted == 301
    finally:
        temp.cleanup()


def test_watermark_bootstraps_from_local_fills_without_refetching_history():
    temp, base, strategy = build_repo()
    try:
        now = datetime.now(timezone.utc)
        position = reserve_and_open(strategy, event="window-boot", shares=Decimal("5"))
        intent = _exit_intent(strategy, position, "order-boot", shares=Decimal("5"))
        historical = [
            _trade(f"boot-{index}", "order-boot", now - timedelta(hours=index + 1))
            for index in range(200)
        ]
        for trade in historical:
            _persist_trade(strategy, intent["intent_id"], trade)

        adapter = WindowedAdapter(historical, page_size=50)
        worker = _worker(base, strategy, adapter, overlap_seconds=300.0)
        asyncio.run(worker.run_once("test"))

        metrics = worker._last_trade_fetch
        assert metrics["trade_fetch_mode"] == "incremental"
        assert metrics["trade_fetch_watermark_source"] == "local_fill_history"
        # The window opens one overlap behind the newest local fill, so it sees
        # that one trade again -- not the other 199.
        assert metrics["trade_fetch_remote_count"] <= 2
        assert metrics["trade_fetch_pages"] == 1
        assert adapter.full_history_calls == 0
    finally:
        temp.cleanup()


def test_mid_pagination_failure_does_not_advance_the_watermark():
    temp, base, strategy = build_repo()
    try:
        now = datetime.now(timezone.utc)
        position = reserve_and_open(strategy, event="window-fail", shares=Decimal("3"))
        _exit_intent(strategy, position, "order-fail", shares=Decimal("3"))
        trades = [
            _trade(f"fail-{index}", "order-fail", now - timedelta(seconds=index))
            for index in range(150)
        ]
        # Page 1 succeeds, page 2 times out.
        adapter = WindowedAdapter(trades, page_size=100, fail_on_page=2)
        worker = _worker(base, strategy, adapter, overlap_seconds=600.0)

        outcome = asyncio.run(worker.run_once("test"))
        assert outcome["status"] == "failed"
        assert base.get_state(STATE_WATERMARK_AT) == ""

        # The next run can complete the same range from the start.
        adapter.fail_on_page = None
        adapter.page_requests.clear()
        assert asyncio.run(
            worker.run_once("test", force=True)
        )["status"] in {"ok", "gaps"}
        assert len(adapter.page_requests) == 2
        with base.connect() as conn:
            persisted = conn.execute(
                "SELECT COUNT(*) AS n FROM live_strategy_fills"
            ).fetchone()["n"]
        assert persisted == 150
    finally:
        temp.cleanup()


def test_late_arriving_trade_is_still_caught_by_the_overlap():
    temp, base, strategy = build_repo()
    try:
        now = datetime.now(timezone.utc)
        position = reserve_and_open(strategy, event="window-late", shares=Decimal("5"))
        _exit_intent(strategy, position, "order-late", shares=Decimal("5"))
        on_time = _trade("late-anchor", "order-late", now)
        adapter = WindowedAdapter([on_time])
        worker = _worker(base, strategy, adapter, overlap_seconds=300.0)
        asyncio.run(worker.run_once("test", force=True))
        watermark = base.get_state(STATE_WATERMARK_AT)
        assert watermark

        # Publishes only now, but matched two minutes before the watermark.
        adapter.trade_book.append(
            _trade("late-arrival", "order-late", now - timedelta(seconds=120))
        )
        asyncio.run(worker.run_once("test", force=True))

        with base.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM live_strategy_fills WHERE remote_trade_id=?",
                ("late-arrival",),
            ).fetchone()
        assert row["n"] == 1
    finally:
        temp.cleanup()


def test_quiet_reconciliation_does_the_minimum_work():
    temp, base, strategy = build_repo()
    try:
        now = datetime.now(timezone.utc)
        position = reserve_and_open(strategy, event="window-quiet", shares=Decimal("3"))
        _exit_intent(strategy, position, "order-quiet", shares=Decimal("3"))
        adapter = WindowedAdapter([_trade("quiet-1", "order-quiet", now)])
        worker = _worker(base, strategy, adapter, overlap_seconds=300.0)
        asyncio.run(worker.run_once("test", force=True))

        adapter.page_requests.clear()
        asyncio.run(worker.run_once("test", force=True))
        metrics = worker._last_trade_fetch

        assert len(adapter.page_requests) == 1
        assert metrics["trade_fetch_new_count"] == 0
        assert metrics["trade_fetch_backlog_or_limit_hit"] == ""
    finally:
        temp.cleanup()


def test_incremental_cost_is_independent_of_a_100k_trade_history():
    """The scalability claim itself, at 100k historical trades.

    Work must scale with the overlap plus new activity, not with the ledger.
    """
    temp, base, strategy = build_repo()
    try:
        now = datetime.now(timezone.utc)
        position = reserve_and_open(strategy, event="window-scale", shares=Decimal("5"))
        intent = _exit_intent(strategy, position, "order-scale", shares=Decimal("5"))

        history = [
            _trade(
                f"hist-{index}", "order-scale",
                now - timedelta(days=30) + timedelta(seconds=index * 20),
                size="0.00001",
            )
            for index in range(100_000)
        ]
        newest_history = history[-1]
        _persist_trade(strategy, intent["intent_id"], newest_history)

        new_trades = [
            _trade(f"new-{index}", "order-scale", now + timedelta(seconds=index), size="0.00001")
            for index in range(3)
        ]
        adapter = WindowedAdapter(history + new_trades, page_size=100)
        worker = _worker(base, strategy, adapter, overlap_seconds=300.0)

        started = time.perf_counter()
        asyncio.run(worker.run_once("test", force=True))
        elapsed = time.perf_counter() - started
        metrics = worker._last_trade_fetch

        assert adapter.full_history_calls == 0
        # A full-history run needs 1001 pages here; the incremental one needs
        # a single page, and reads ~overlap-worth of trades instead of 100,000.
        assert metrics["trade_fetch_pages"] == 1
        assert len(adapter.page_requests) == 1
        assert metrics["trade_fetch_remote_count"] < 100
        assert elapsed < 30.0

        with base.connect() as conn:
            rows = conn.execute(
                "SELECT remote_trade_id FROM live_strategy_fills"
            ).fetchall()
        persisted = {str(row["remote_trade_id"]) for row in rows}
        # All three new trades landed, and the ledger did not.
        assert {"new-0", "new-1", "new-2"} <= persisted
        assert len(persisted) < 100
    finally:
        temp.cleanup()


def test_disabled_policy_falls_back_to_full_history():
    temp, base, strategy = build_repo()
    try:
        now = datetime.now(timezone.utc)
        position = reserve_and_open(strategy, event="window-off", shares=Decimal("3"))
        _exit_intent(strategy, position, "order-off", shares=Decimal("3"))
        adapter = WindowedAdapter([_trade("off-1", "order-off", now)])
        worker = _worker(base, strategy, adapter, enabled=False)
        asyncio.run(worker.run_once("test"))

        assert adapter.full_history_calls == 1
        assert base.get_state(STATE_WATERMARK_AT) == ""
        assert worker._last_trade_fetch == {}
    finally:
        temp.cleanup()


def test_adapters_without_windowing_still_reconcile():
    """A duck-typed adapter that predates get_trades_page must keep working."""

    class LegacyAdapter(MockTradingAdapter):
        def __init__(self, trades):
            super().__init__()
            self._trades = trades
            self.calls = 0

        async def get_trades(self):
            self.calls += 1
            return list(self._trades)

    LegacyAdapter.get_trades_page = None
    temp, base, strategy = build_repo()
    try:
        now = datetime.now(timezone.utc)
        position = reserve_and_open(strategy, event="window-legacy", shares=Decimal("3"))
        _exit_intent(strategy, position, "order-legacy", shares=Decimal("3"))
        adapter = LegacyAdapter([_trade("legacy-1", "order-legacy", now)])
        worker = _worker(base, strategy, adapter, overlap_seconds=300.0)
        asyncio.run(worker.run_once("test"))

        assert adapter.calls == 1
        with base.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM live_strategy_fills WHERE remote_trade_id=?",
                ("legacy-1",),
            ).fetchone()
        assert row["n"] == 1
    finally:
        temp.cleanup()

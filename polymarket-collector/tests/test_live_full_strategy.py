import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from live.adapters.mock import MockTradingAdapter
from live.adapters.polymarket import RealPolymarketTradingAdapter
from live.config import LiveConfig
from live.order_book import OrderBookSet
from live.reconciliation import ReconciliationWorker
from live.repository import LiveRepository, now_iso
from live.strategy import AllInBudget, StrategyPolicy, choose_entry, exact_trigger, simulate_buy_fak
from live.strategy_runtime import LiveStrategyRuntime
from live.strategy_repository import StrategyRepository, sanitize


def build_repo():
    temporary = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temporary.name) / "live.sqlite3")
    base.migrate()
    strategy = StrategyRepository(base)
    strategy.migrate()
    return temporary, base, strategy


def reserve_and_open(
    strategy,
    event="event-1",
    shares=Decimal("10"),
    minimum=Decimal("5"),
    sellable=None,
):
    strategy.reserve_event_entry(
        event_id=event, condition_id=f"condition-{event}", token_id=f"token-{event}",
        side="YES", simultaneous=False, reason_code="ENTRY_PRICE_EXACT",
    )
    return strategy.open_position(
        event_id=event, condition_id=f"condition-{event}", token_id=f"token-{event}",
        outcome="YES", shares=shares, average_price=Decimal("0.74"),
        cost_all_in=Decimal("5"), fees=Decimal("0.05"),
        sellable_shares=sellable, min_sellable=minimum,
    )


def test_exact_trigger_never_crosses_and_window_is_exact():
    assert exact_trigger("0.74", Decimal("0.74"))
    assert not exact_trigger("0.740001", Decimal("0.74"))
    assert not exact_trigger("0.75", Decimal("0.74"))
    policy = StrategyPolicy()
    observed = datetime.fromtimestamp(1180, timezone.utc)
    allowed = choose_entry(
        updates=[{"asset_id": "yes", "best_ask": "0.74"}], yes_token_id="yes",
        no_token_id="no", event_ready=True, paused=False, event_locked=False,
        active_exposure=Decimal("0"), observed_at=observed,
        event_id="btc-updown-5m-1000", policy=policy,
    )
    assert allowed.allowed and allowed.side == "YES"
    ended = choose_entry(
        updates=[{"asset_id": "yes", "best_ask": "0.74"}], yes_token_id="yes",
        no_token_id="no", event_ready=True, paused=False, event_locked=False,
        active_exposure=Decimal("0"), observed_at=datetime.fromtimestamp(1300, timezone.utc),
        event_id="btc-updown-5m-1000", policy=policy,
    )
    assert ended.reason == "EVENT_ENDED"


def test_simultaneous_trigger_skips_and_first_separate_update_wins():
    simultaneous = choose_entry(
        updates=[
            {"asset_id": "yes", "best_ask": "0.74"},
            {"asset_id": "no", "best_ask": "0.74"},
        ], yes_token_id="yes", no_token_id="no", event_ready=True, paused=False,
        event_locked=False, active_exposure=Decimal("0"),
        observed_at=datetime.fromtimestamp(1180, timezone.utc),
        event_id="btc-updown-5m-1000",
    )
    assert not simultaneous.allowed and simultaneous.simultaneous
    assert simultaneous.reason == "SKIPPED_SIMULTANEOUS_TRIGGER"
    first = choose_entry(
        updates=[{"asset_id": "no", "best_ask": "0.74"}], yes_token_id="yes",
        no_token_id="no", event_ready=True, paused=False, event_locked=False,
        active_exposure=Decimal("0"), observed_at=datetime.fromtimestamp(1180, timezone.utc),
        event_id="btc-updown-5m-1000",
    )
    assert first.allowed and first.side == "NO"


def test_order_book_snapshot_delta_delete_duplicate_out_of_order_reconnect_two_tokens():
    books = OrderBookSet(["yes", "no"])
    first = books.apply({
        "event_type": "book", "asset_id": "yes", "timestamp": "100",
        "bids": [{"price": "0.70", "size": "8"}, {"price": "0.69", "size": "9"}],
        "asks": [{"price": "0.74", "size": "7"}, {"price": "0.75", "size": "6"}],
    })
    books.apply({
        "event_type": "book", "asset_id": "no", "timestamp": "100",
        "bids": [{"price": "0.24", "size": "8"}],
        "asks": [{"price": "0.26", "size": "7"}],
    })
    assert first.updates[0]["asks"][1] == {"price": "0.75", "size": "6"}
    assert books.event_ready(["yes", "no"]) == (True, "READY")
    delta = {
        "event_type": "price_change", "timestamp": "101",
        "price_changes": [{
            "asset_id": "yes", "side": "SELL", "price": "0.75", "size": "10",
            "best_bid": "0.70", "best_ask": "0.74",
        }],
    }
    applied = books.apply(delta)
    assert applied.updates[0]["asks"] == [
        {"price": "0.74", "size": "7"}, {"price": "0.75", "size": "10"}
    ]
    assert books.apply(delta).duplicate
    deleted = books.apply({
        "event_type": "price_change", "timestamp": "102",
        "price_changes": [{
            "asset_id": "yes", "side": "SELL", "price": "0.74", "size": "0",
            "best_bid": "0.70", "best_ask": "0.75",
        }],
    })
    assert deleted.updates[0]["best_ask"] == "0.75"
    out = books.apply({
        "event_type": "price_change", "timestamp": "99",
        "price_changes": [{"asset_id": "yes", "side": "BUY", "price": "0.71", "size": "1"}],
    })
    assert out.out_of_order and books.books["yes"].ready
    assert books.books["yes"].best_ask == Decimal("0.75")
    books.mark_not_ready("RECONNECT_AWAITING_SNAPSHOT")
    assert not books.books["yes"].bids and not books.books["no"].asks
    assert books.event_ready(["yes", "no"])[0] is False


def test_five_dollar_all_in_rounding_fees_and_minimum():
    budget = AllInBudget(Decimal("5"))
    assert budget.sdk_buy_parameters() == {"amount": "3.8", "max_spend": "5"}
    result = simulate_buy_fak(
        [{"price": "0.74", "size": "100"}], max_price=Decimal("0.76"),
        max_spend=Decimal("5"), fee_rate=Decimal("0.07"),
        max_shares=Decimal("5"),
    )
    assert result.all_in <= Decimal("5")
    assert result.filled_shares <= Decimal("5")
    assert budget.minimum_viable(
        min_order_shares=Decimal("5"), maximum_price=Decimal("0.76"),
        maximum_fee_fraction=Decimal("0.07"),
    ) == (True, "VIABLE")
    assert budget.minimum_viable(
        min_order_shares=Decimal("100"), maximum_price=Decimal("0.76"),
        maximum_fee_fraction=Decimal("0.07"),
    )[0] is False

    assert budget.minimum_viable(
        min_order_shares=Decimal("5.000001"), maximum_price=Decimal("0.76"),
        maximum_fee_fraction=Decimal("0.07"),
    ) == (False, "MINIMUM_ORDER_EXCEEDS_5_TOKEN_CAP")

def test_zero_fill_allows_new_unique_entry_attempt_and_preserves_history():
    temp, base, strategy = build_repo()
    try:
        first = strategy.reserve_event_entry(
            event_id="e", condition_id="c", token_id="yes", side="YES",
            simultaneous=False, reason_code="ENTRY_PRICE_EXACT",
        )
        strategy.mark_zero_fill(
            "e", "FAK_ZERO_FILL", intent_id=first["entry_intent_id"]
        )
        restarted = StrategyRepository(LiveRepository(base.db_path))
        restarted.migrate()
        second = restarted.reserve_event_entry(
            event_id="e", condition_id="c", token_id="yes", side="YES",
            simultaneous=False, reason_code="ENTRY_PRICE_EXACT",
        )
        assert not second.get("_duplicate")
        assert second["entry_intent_id"] != first["entry_intent_id"]
        assert restarted.intent(first["entry_intent_id"])["state"] == "ZERO_FILL"
        with base.connect() as conn:
            intents = conn.execute(
                "SELECT intent_id,state FROM live_strategy_intents "
                "WHERE event_id='e' ORDER BY created_at"
            ).fetchall()
            indexes = {
                row["name"] for row in conn.execute(
                    "PRAGMA index_list('live_strategy_intents')"
                ).fetchall()
            }
        assert [(row["intent_id"], row["state"]) for row in intents] == [
            (first["entry_intent_id"], "ZERO_FILL"),
            (second["entry_intent_id"], "RESERVED"),
        ]
        assert "idx_live_strategy_one_entry" not in indexes
        assert "idx_live_strategy_one_unresolved_entry" in indexes
    finally:
        temp.cleanup()


def test_canary_reservation_consumes_and_disarms_atomically_across_restart():
    temp, base, strategy = build_repo()
    try:
        base.set_state("canary_armed", "true", "test")
        base.set_state("canary_consumed", "false", "test")
        base.set_state("kill_switch", "false", "operator")
        base.set_state("pause_entries", "false", "test")
        first = strategy.reserve_event_entry(
            event_id="canary-event", condition_id="c", token_id="yes", side="YES",
            simultaneous=False, reason_code="ENTRY_PRICE_EXACT", consume_canary=True,
        )
        assert not first.get("_blocked")
        restarted = StrategyRepository(LiveRepository(base.db_path))
        assert base.get_state("canary_armed") == "false"
        assert base.get_state("canary_consumed") == "true"
        assert base.get_state("pause_entries") == "true"
        blocked = restarted.reserve_event_entry(
            event_id="second-event", condition_id="c2", token_id="no", side="NO",
            simultaneous=False, reason_code="ENTRY_PRICE_EXACT", consume_canary=True,
        )
        assert blocked == {"_blocked": True, "reason": "CANARY_NOT_AVAILABLE"}
        assert restarted.event_state("second-event") is None
    finally:
        temp.cleanup()



def test_atomic_simultaneous_lock_has_no_order_intent():
    temp, base, strategy = build_repo()
    try:
        state = strategy.reserve_event_entry(
            event_id="e", condition_id="c", token_id=None, side=None,
            simultaneous=True, reason_code="SKIPPED_SIMULTANEOUS_TRIGGER",
        )
        assert state["status"] == "SKIPPED_SIMULTANEOUS_TRIGGER"
        assert state["entry_intent_id"] is None
        assert base.list_table("live_strategy_intents", 10) == []
    finally:
        temp.cleanup()


def test_partial_tp_cancel_race_no_oversell_and_dust():
    temp, _base, strategy = build_repo()
    try:
        position = reserve_and_open(strategy, shares=Decimal("10"))
        tp = strategy.reserve_position_intent(
            position, action="TP", purpose="TAKE_PROFIT", order_type="GTC",
            shares=Decimal("10"), price_limit=Decimal("0.96"), book_hash="entry",
        )
        partial = strategy.apply_exit_fill(
            position_id=position["position_id"], intent_id=tp["intent_id"],
            sold_shares=Decimal("3"), average_price=Decimal("0.96"), fees=Decimal("0.01"),
            final_state="PARTIAL", min_sellable=Decimal("5"), purpose="TAKE_PROFIT",
            book_hash="book-1",
        )
        assert partial["state"] == "TP_OPEN" and partial["remaining_shares_text"] == "7"
        cancel = strategy.cancel_tp(position["position_id"], "STOP_066")
        assert cancel and cancel["state"] == "CANCEL_REQUESTED"
        strategy.finalize_cancel(tp["intent_id"], True, "CANCEL_ACK")
        refreshed = strategy.position_for_token(position["token_id"])
        exit_intent = strategy.reserve_position_intent(
            refreshed, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("7"), price_limit=Decimal("0.55"), book_hash="book-2",
        )
        closed = strategy.apply_exit_fill(
            position_id=position["position_id"], intent_id=exit_intent["intent_id"],
            sold_shares=Decimal("100"), average_price=Decimal("0.60"), fees=Decimal("0"),
            final_state="FILLED", min_sellable=Decimal("5"), purpose="STOP_066",
            book_hash="book-2",
        )
        assert closed["remaining_shares_text"] == "0" and closed["state"] == "CLOSED"
    finally:
        temp.cleanup()



def test_cancel_local_waiting_tp_preserves_resolved_position_state():
    temp, _base, strategy = build_repo()
    try:
        position = reserve_and_open(
            strategy,
            event="resolved-waiting-tp",
            shares=Decimal("5"),
            sellable=Decimal("0"),
        )
        tp = strategy.reserve_position_intent(
            position,
            action="TP",
            purpose="TAKE_PROFIT",
            order_type="GTC",
            shares=Decimal("5"),
            price_limit=Decimal("0.96"),
            book_hash="entry",
        )
        strategy.mark_waiting_sellable(
            tp["intent_id"],
            reason="TAKE_PROFIT_WAITING_FOR_FULL_SELLABLE_BALANCE",
        )
        resolved = strategy.mark_position_resolved(
            position["position_id"],
            winner=True,
            redeem_pending=True,
        )
        assert resolved["state"] == "REDEEM_PENDING"

        strategy.finalize_cancel(
            tp["intent_id"],
            True,
            "RESOLVED_POSITION_LOCAL_TP_CLEANUP",
        )

        refreshed = strategy.position_for_token(position["token_id"])
        assert refreshed["state"] == "REDEEM_PENDING"
        assert refreshed["tp_intent_id"] is None
        assert strategy.intent(tp["intent_id"])["state"] == "CANCELED"
    finally:
        temp.cleanup()



def test_reconciled_exit_fill_is_idempotent_and_preserves_true_dust():
    temp, _base, strategy = build_repo()
    try:
        position = reserve_and_open(
            strategy, shares=Decimal("5.066664"), minimum=Decimal("5")
        )
        intent = strategy.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("5.0666"), price_limit=Decimal("0.55"), book_hash="stop",
        )
        kwargs = dict(
            position_id=position["position_id"], intent_id=intent["intent_id"],
            sold_shares=Decimal("5.06"), average_price=Decimal("0.8"),
            fees=Decimal("0"), final_state="PARTIAL_FINAL",
            min_sellable=Decimal("5"), purpose="STOP_066",
            book_hash="account-reconciliation",
            cumulative_filled_shares=Decimal("5.06"),
            cumulative_notional=Decimal("4.048"),
            cumulative_fees=Decimal("0"),
        )
        first = strategy.apply_exit_fill(**kwargs)
        repeated = strategy.apply_exit_fill(**kwargs)
        assert first["remaining_shares_text"] == "0.006664"
        assert repeated["remaining_shares_text"] == "0.006664"
        assert repeated["exit_value_text"] == "4.048"
        assert repeated["state"] == "DUST"
        assert repeated["closed_at"] is not None
        assert repeated["tp_intent_id"] is None
        assert repeated["active_exit_intent_id"] is None
        with _base.connect() as conn:
            conn.execute(
                "UPDATE live_strategy_positions SET closed_at=NULL "
                "WHERE position_id=?", (position["position_id"],),
            )
            conn.commit()
        repaired = strategy.repair_terminal_dust_slots(actor="test")
        assert len(repaired) == 1
        repaired_position = strategy.position_for_token(position["token_id"])
        assert repaired_position["closed_at"] is not None
        next_entry = strategy.reserve_event_entry(
            event_id="after-terminal-dust", condition_id="after-terminal-dust-c",
            token_id="after-terminal-dust-token", side="NO", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT", require_empty_slot=True,
        )
        assert not next_entry.get("_blocked")
    finally:
        temp.cleanup()


def test_stale_remote_position_after_confirmed_sell_never_resurrects_local():
    temp, base, strategy = build_repo()
    try:
        event = "stale-after-sell"
        condition = "condition-stale-after-sell"
        token = "token-stale-after-sell"
        base.upsert_market({
            "event_id": event, "condition_id": condition,
            "yes_token_id": token, "no_token_id": "other-token",
            "token_mapping_status": "verified", "accepting_orders": True,
            "min_order_size": "5",
        })
        position = reserve_and_open(
            strategy, event=event, shares=Decimal("5.066664"),
            minimum=Decimal("5"), sellable=Decimal("5.066664"),
        )
        intent = strategy.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("5.0666"), price_limit=Decimal("0.55"), book_hash="stop",
        )
        strategy.apply_exit_fill(
            position_id=position["position_id"], intent_id=intent["intent_id"],
            sold_shares=Decimal("5.06"), average_price=Decimal("0.8"), fees=Decimal("0"),
            final_state="PARTIAL_FINAL", min_sellable=Decimal("5"),
            purpose="STOP_066", book_hash="stop",
        )
        adapter = MockTradingAdapter()
        adapter.positions = [{
            "condition_id": condition, "token_id": token, "outcome": "YES",
            "size": "5.066664", "average_price": "0.75", "current_value": "4",
        }]
        result = asyncio.run(ReconciliationWorker(base, adapter, strategy).run_once("test"))
        assert result["status"] == "ok"
        current = strategy.position_for_token(token)
        assert current["remaining_shares_text"] == "0.006664"
        assert current["state"] == "DUST"
    finally:
        temp.cleanup()


def test_remote_zero_after_sell_grace_keeps_dust_and_reconciliation_ready():
    temp, base, strategy = build_repo()
    try:
        position = reserve_and_open(
            strategy, event="remote-zero-after-sell",
            shares=Decimal("5.066664"), minimum=Decimal("5"),
            sellable=Decimal("5.066664"),
        )
        intent = strategy.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("5.0666"), price_limit=Decimal("0.55"), book_hash="stop",
        )
        strategy.apply_exit_fill(
            position_id=position["position_id"], intent_id=intent["intent_id"],
            sold_shares=Decimal("5.06"), average_price=Decimal("0.8"), fees=Decimal("0"),
            final_state="PARTIAL_FINAL", min_sellable=Decimal("5"),
            purpose="STOP_066", book_hash="stop",
        )
        with base.connect() as conn:
            conn.execute(
                "UPDATE live_strategy_positions SET updated_at='2000-01-01T00:00:00+00:00' "
                "WHERE position_id=?", (position["position_id"],),
            )
            conn.commit()
        result = asyncio.run(
            ReconciliationWorker(base, MockTradingAdapter(), strategy).run_once("test")
        )
        assert result["status"] == "ok"
        assert base.get_state("reconciliation_readiness") == "READY"
        current = strategy.position_for_token(position["token_id"])
        assert current["remaining_shares_text"] == "0.006664"
        assert current["state"] == "DUST"
    finally:
        temp.cleanup()


def test_stop_success_never_recreates_take_profit():
    class RecordingAdapter(MockTradingAdapter):
        def __init__(self):
            super().__init__()
            self.create_calls = []

        async def create_order(self, order):
            self.create_calls.append(order)
            return await super().create_order(order)

    temp, base, strategy = build_repo()
    try:
        position = reserve_and_open(
            strategy, event="no-tp-after-stop", shares=Decimal("12"),
            minimum=Decimal("5"), sellable=Decimal("12"),
        )
        stop = strategy.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("12"), price_limit=Decimal("0.55"), book_hash="stop",
        )
        stopped = strategy.apply_exit_fill(
            position_id=position["position_id"], intent_id=stop["intent_id"],
            sold_shares=Decimal("6"), average_price=Decimal("0.8"), fees=Decimal("0"),
            final_state="PARTIAL_FINAL", min_sellable=Decimal("5"),
            purpose="STOP_066", book_hash="stop",
        )
        assert stopped["state"] == "EXITING"
        assert stopped["stop_stage"] == 1
        adapter = RecordingAdapter()
        runtime = LiveStrategyRuntime(
            LiveConfig(execution_mode="REAL_TRADING"), base, strategy, adapter
        )
        asyncio.run(runtime._ensure_take_profit(stopped))
        assert adapter.create_calls == []
        assert strategy.position_for_token(position["token_id"])["tp_intent_id"] is None
    finally:
        temp.cleanup()


def test_persistent_remote_position_after_sell_grace_is_real_gap():
    temp, base, strategy = build_repo()
    try:
        event = "persistent-stale-after-sell"
        condition = "condition-persistent-stale-after-sell"
        token = "token-persistent-stale-after-sell"
        base.upsert_market({
            "event_id": event, "condition_id": condition,
            "yes_token_id": token, "no_token_id": "other-token",
            "token_mapping_status": "verified", "accepting_orders": True,
            "min_order_size": "5",
        })
        position = reserve_and_open(
            strategy, event=event, shares=Decimal("5.066664"),
            minimum=Decimal("5"), sellable=Decimal("5.066664"),
        )
        stop = strategy.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("5.0666"), price_limit=Decimal("0.55"), book_hash="stop",
        )
        strategy.apply_exit_fill(
            position_id=position["position_id"], intent_id=stop["intent_id"],
            sold_shares=Decimal("5.06"), average_price=Decimal("0.8"), fees=Decimal("0"),
            final_state="PARTIAL_FINAL", min_sellable=Decimal("5"),
            purpose="STOP_066", book_hash="stop",
        )
        with base.connect() as conn:
            conn.execute(
                "UPDATE live_strategy_positions SET updated_at='2000-01-01T00:00:00+00:00' "
                "WHERE position_id=?", (position["position_id"],),
            )
            conn.commit()
        adapter = MockTradingAdapter()
        adapter.positions = [{
            "condition_id": condition, "token_id": token, "outcome": "YES",
            "size": "5.066664", "average_price": "0.75", "current_value": "4",
        }]
        result = asyncio.run(ReconciliationWorker(base, adapter, strategy).run_once("test"))
        assert result["status"] == "gaps"
        assert result["gaps"] == [{
            "type": "remote_position_after_confirmed_exit",
            "position_id": position["position_id"], "token_id": token,
            "local_shares": "0.006664", "remote_shares": "5.066664",
        }]
        assert base.get_state("reconciliation_readiness") == "NOT_READY"
        assert strategy.pause_entries()
        assert strategy.position_for_token(token)["remaining_shares_text"] == "0.006664"
    finally:
        temp.cleanup()


def test_transient_rate_limit_backs_off_without_financial_correction_then_recovers():
    class ToggleRateLimitAdapter(MockTradingAdapter):
        def __init__(self):
            super().__init__()
            self.rate_limited = True
            self.balance_calls = 0

        async def get_balance(self):
            self.balance_calls += 1
            if self.rate_limited:
                raise RuntimeError("HTTP 429 too many requests: rate limit")
            return await super().get_balance()

    temp, base, strategy = build_repo()
    try:
        base.set_state("kill_switch", "false", "operator")
        strategy.set_pause_entries(False, "operator", "READY")
        adapter = ToggleRateLimitAdapter()
        worker = ReconciliationWorker(base, adapter, strategy)
        failed = asyncio.run(worker.run_once("test"))
        assert failed["status"] == "failed" and failed["rate_limited"]
        assert failed["retry_after_seconds"] > 0
        assert base.get_state("pause_owner") == "RECONCILIATION"
        assert base.get_state("pause_auto_recoverable") == "true"
        backed_off = asyncio.run(worker.run_once("test"))
        assert backed_off["status"] == "backoff"
        assert adapter.balance_calls == 1
        assert strategy.active_positions() == []
        adapter.rate_limited = False
        worker._rate_limit_retry_after = 0
        clean = asyncio.run(worker.run_once("test"))
        assert clean["status"] == "ok"
        assert base.get_state("reconciliation_readiness") == "READY"
        assert base.get_state("kill_switch") == "false"
        assert strategy.pause_entries()
        assert base.get_state("pause_owner") == "RECONCILIATION"
    finally:
        temp.cleanup()

def test_partial_emergency_allows_new_book_only_and_never_parallel():
    temp, _base, strategy = build_repo()
    try:
        position = reserve_and_open(strategy, shares=Decimal("12"))
        intent = strategy.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("12"), price_limit=Decimal("0.55"), book_hash="book-a",
        )
        duplicate = strategy.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("12"), price_limit=Decimal("0.55"), book_hash="book-a",
        )
        assert duplicate["_duplicate"]
        partial = strategy.apply_exit_fill(
            position_id=position["position_id"], intent_id=intent["intent_id"],
            sold_shares=Decimal("6"), average_price=Decimal("0.60"), fees=Decimal("0"),
            final_state="PARTIAL_FINAL", min_sellable=Decimal("5"), purpose="STOP_066",
            book_hash="book-a",
        )
        second = strategy.reserve_position_intent(
            partial, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("6"), price_limit=Decimal("0.55"), book_hash="book-b",
        )
        assert not second.get("_duplicate") and second["intent_id"] != intent["intent_id"]
    finally:
        temp.cleanup()


def test_partial_entry_under_minimum_stays_risk_managed_and_counts_exposure():
    """A sub-minimum partial BUY is still money at risk, not a write-off.

    Treating it as weightless is what let a 3.68-share partial entry drop out
    of exit management entirely, so the remainder must keep its full cost in
    exposure and stay in the risk-managed view until it is actually settled.
    """
    temp, _base, strategy = build_repo()
    try:
        position = reserve_and_open(strategy, shares=Decimal("3"), minimum=Decimal("5"))
        assert position["state"] == "DUST"
        assert position["sellable_shares_text"] == "0"
        assert position["dust_shares_text"] == "3"
        assert position["closed_at"] is None
        assert strategy.exposure() == Decimal("5")
        managed = strategy.risk_managed_positions()
        assert [row["position_id"] for row in managed] == [position["position_id"]]
        assert [row["position_id"] for row in strategy.active_positions()] == [
            position["position_id"]
        ]
        assert strategy.unresolved_positions()[0]["position_id"] == position["position_id"]
    finally:
        temp.cleanup()


def test_resolution_winner_redeem_loser_zero_and_noop_closed():
    temp, _base, strategy = build_repo()
    try:
        winner = reserve_and_open(strategy, event="winner", shares=Decimal("6"))
        resolved = strategy.mark_position_resolved(winner["position_id"], winner=True, redeem_pending=True)
        assert resolved["state"] == "REDEEM_PENDING"
        redeemed = strategy.mark_position_redeemed(winner["position_id"], "tx-public")
        assert redeemed["state"] == "REDEEMED" and redeemed["remaining_shares_text"] == "0"
        loser = reserve_and_open(strategy, event="loser", shares=Decimal("6"))
        zero = strategy.mark_position_resolved(loser["position_id"], winner=False, redeem_pending=False)
        assert zero["state"] == "RESOLVED_LOSER"
        assert Decimal(zero["realized_pnl_text"]) == Decimal("-5")
        repeated = strategy.mark_position_resolved(
            loser["position_id"], winner=False, redeem_pending=False
        )
        assert repeated["state"] == "RESOLVED_LOSER"
        with strategy.base.connect() as conn:
            daily = conn.execute(
                "SELECT realized_pnl_usd,consecutive_losing_deals FROM live_daily_limits"
            ).fetchone()
            deal = conn.execute(
                "SELECT state,final_reason FROM live_strategy_deals WHERE event_id='loser'"
            ).fetchone()
        assert Decimal(str(daily["realized_pnl_usd"])) == Decimal("-5")
        assert daily["consecutive_losing_deals"] == 1
        assert deal["state"] == "RESOLVED_LOSER"
        assert deal["final_reason"] == "MARKET_RESOLUTION"
        assert strategy.unresolved_positions() == []
    finally:
        temp.cleanup()


def test_exposure_cap_across_events_and_dust_exception():
    temp, _base, strategy = build_repo()
    try:
        reserve_and_open(strategy, event="one", shares=Decimal("6"))
        decision = choose_entry(
            updates=[{"asset_id": "yes", "best_ask": "0.74"}], yes_token_id="yes",
            no_token_id="no", event_ready=True, paused=False, event_locked=False,
            active_exposure=strategy.exposure(), observed_at=datetime.fromtimestamp(1180, timezone.utc),
            event_id="btc-updown-5m-1000",
        )
        assert decision.reason == "EXPOSURE_CAP"
    finally:
        temp.cleanup()


def test_reconciliation_clean_then_missing_remote_position_fails_closed():
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        worker = ReconciliationWorker(base, adapter, strategy)
        clean = asyncio.run(worker.run_once("test"))
        assert clean["status"] == "ok"
        assert base.get_state("reconciliation_readiness") == "READY"
        strategy.set_pause_entries(False, "operator", "READY")
        reserve_and_open(strategy, shares=Decimal("6"))
        gap = asyncio.run(worker.run_once("test"))
        assert gap["status"] == "gaps"
        assert any(item["type"] == "local_position_missing_remote" for item in gap["gaps"])
        assert strategy.pause_entries()
    finally:
        temp.cleanup()


def test_reconciliation_recovers_remote_position_then_requires_clean_pass():
    temp, base, strategy = build_repo()
    try:
        base.upsert_market({
            "event_id": "remote-event", "condition_id": "remote-condition",
            "yes_token_id": "remote-token", "no_token_id": "remote-no",
            "token_mapping_status": "verified", "accepting_orders": True,
        })
        adapter = MockTradingAdapter()
        adapter.positions = [{
            "condition_id": "remote-condition", "token_id": "remote-token", "outcome": "YES",
            "size": "7", "average_price": "0.72", "current_value": "4.9",
        }]
        worker = ReconciliationWorker(base, adapter, strategy)
        corrected = asyncio.run(worker.run_once("test"))
        assert corrected["status"] == "gaps"
        assert strategy.position_for_token("remote-token")["remaining_shares_text"] == "7"
        backed_off = asyncio.run(worker.run_once("test"))
        assert backed_off["status"] == "backoff"
        worker._rate_limit_retry_after = 0
        clean = asyncio.run(worker.run_once("test"))
        assert clean["status"] == "ok"
        assert base.get_state("reconciliation_readiness") == "READY"
    finally:
        temp.cleanup()


class FakeSecureClient:
    wallet = "0x2222222222222222222222222222222222222222"
    signer = "0x1111111111111111111111111111111111111111"
    wallet_type = "POLY_PROXY"

    def __init__(self, allowance=10_000_000):
        self.allowance = allowance
        self.market_calls = []
        self.limit_calls = []
        self.posted = []
        self._ctx = type("Context", (), {"secure_clob": type("Transport", (), {
            "post_json": staticmethod(lambda *args, **kwargs: asyncio.sleep(0, result={"status": "ok"}))
        })()})()

    async def get_balance_allowance(self, **kwargs):
        return {"balance": 10_000_000, "allowances": {"exchange": self.allowance}}

    async def create_market_order(self, **kwargs):
        self.market_calls.append(kwargs)
        return {"signed": True, "kind": "market"}

    async def create_limit_order(self, **kwargs):
        self.limit_calls.append(kwargs)
        return {"signed": True, "kind": "limit"}

    async def post_order(self, signed):
        self.posted.append(signed)
        return {"ok": True, "status": "matched", "order_id": "remote-1", "trade_ids": ["trade-1"]}

    async def cancel_order(self, **kwargs):
        return {"canceled": [kwargs["order_id"]], "not_canceled": {}}

    async def cancel_orders(self, **kwargs):
        return {"canceled": list(kwargs["order_ids"]), "not_canceled": {}}

    async def cancel_market_orders(self, **kwargs):
        return {"canceled": ["relevant"], "not_canceled": {}}


def armed_config():
    return LiveConfig(
        trading_mode="LIVE", execution_mode="REAL_TRADING", live_module_enabled=True,
        live_trading_enabled=True, live_order_submission_enabled=True,
        live_adapter="polymarket", pause_entries_default=False, canary_armed=True,
        live_kill_switch_default=False, funder_address=FakeSecureClient.wallet, signature_type=1,
    )


def test_adapter_buy_fak_max_spend_max_price_and_no_auto_approval():
    fake = FakeSecureClient()
    adapter = RealPolymarketTradingAdapter(armed_config(), secure_client=fake)
    result = asyncio.run(adapter.create_order({
        "durable_intent_reserved": True, "token_id": "token", "side": "BUY",
        "order_type": "FAK", "requested_amount_usd": "3.8", "max_spend": "5",
        "max_price": "0.76", "max_tokens": "5",
    }))
    assert result["status"] == "matched"
    assert fake.limit_calls == [{
        "token_id": "token", "price": "0.76", "size": "5", "side": "BUY",
    }]
    assert fake.posted[0]["order_type"] == "FAK"
    assert fake.market_calls == []
    assert len(fake.posted) == 1


def test_adapter_sell_fak_gtc_targeted_cancel_and_heartbeat():
    fake = FakeSecureClient()
    adapter = RealPolymarketTradingAdapter(armed_config(), secure_client=fake)
    sold = asyncio.run(adapter.create_order({
        "durable_intent_reserved": True, "token_id": "token", "side": "SELL",
        "order_type": "FAK", "requested_size": "6", "min_price": "0.55",
    }))
    assert sold["success"]
    assert fake.market_calls[0]["shares"] == "6" and fake.market_calls[0]["min_price"] == "0.55"
    asyncio.run(adapter.create_order({
        "durable_intent_reserved": True, "token_id": "token", "side": "SELL",
        "order_type": "GTC", "requested_size": "6", "requested_price": "0.96",
    }))
    assert fake.limit_calls[0]["price"] == "0.96"
    assert asyncio.run(adapter.cancel_order("remote-1"))["success"]
    assert asyncio.run(adapter.cancel_market_orders("condition", "token"))["success"]
    assert asyncio.run(adapter.cancel_all_orders())["failure_reason"] == "GLOBAL_CANCEL_ALL_PROHIBITED"
    assert asyncio.run(adapter.heartbeat())["success"]


def test_adapter_insufficient_allowance_blocks_before_sign_and_post():
    fake = FakeSecureClient(allowance=0)
    adapter = RealPolymarketTradingAdapter(armed_config(), secure_client=fake)
    result = asyncio.run(adapter.create_order({
        "durable_intent_reserved": True, "token_id": "token", "side": "BUY",
        "order_type": "FAK", "requested_amount_usd": "3.8", "max_spend": "5",
        "max_price": "0.76", "max_tokens": "5",
    }))
    assert result["status"] == "blocked"
    assert "APPROVAL_REQUIRED" in result["failure_reason"]
    assert fake.market_calls == [] and fake.posted == []


class FakeResponseClient(FakeSecureClient):
    def __init__(self, response):
        super().__init__()
        self.response = response

    async def post_order(self, signed):
        self.posted.append(signed)
        await asyncio.sleep(0)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _adapter_entry_order():
    return {
        "durable_intent_reserved": True, "token_id": "token", "side": "BUY",
        "order_type": "FAK", "requested_amount_usd": "3.8", "max_spend": "5",
        "max_price": "0.76", "max_tokens": "5",
    }

def test_adapter_delayed_pending_partial_rejected_and_unknown_responses_never_retry():
    cases = [
        ({"ok": True, "status": "delayed", "order_id": "delayed-1"}, True, "delayed"),
        ({"ok": True, "status": "pending", "order_id": "pending-1"}, True, "pending"),
        ({"ok": True, "status": "matched", "order_id": "partial-1",
          "making_amount": "3", "taking_amount": "2.22"}, True, "matched"),
        ({"ok": False, "code": "order_rejected", "message": "rejected"}, False, "rejected"),
        ({"ok": True}, True, "unknown"),
    ]
    for response, expected_success, expected_status in cases:
        fake = FakeResponseClient(response)
        result = asyncio.run(
            RealPolymarketTradingAdapter(armed_config(), secure_client=fake).create_order(
                _adapter_entry_order()
            )
        )
        assert result["success"] is expected_success
        assert result["status"] == expected_status
        assert len(fake.posted) == 1


def test_adapter_transport_timeout_is_uncertain_and_never_retried():
    fake = FakeResponseClient(asyncio.TimeoutError("SDK read timeout"))
    result = asyncio.run(
        RealPolymarketTradingAdapter(armed_config(), secure_client=fake).create_order(
            _adapter_entry_order()
        )
    )
    assert result["success"] is False
    assert result["status"] == "unknown"
    assert "TimeoutError" in result["failure_reason"]
    assert len(fake.posted) == 1


def test_adapter_fail_closed_when_not_armed_or_without_durable_intent():
    fake = FakeSecureClient()
    adapter = RealPolymarketTradingAdapter(LiveConfig(), secure_client=fake)
    blocked = asyncio.run(adapter.create_order({"token_id": "token"}))
    assert blocked["failure_reason"] == "REAL_SUBMISSION_NOT_ARMED"
    armed = RealPolymarketTradingAdapter(armed_config(), secure_client=fake)
    durable = asyncio.run(armed.create_order({"token_id": "token"}))
    assert durable["failure_reason"] == "DURABLE_INTENT_REQUIRED"


def test_recursive_masking_covers_payloads_and_error_strings():
    masked = sanitize({
        "private_key": "never-show", "nested": {"passphrase": "never-show-2"},
        "error": "Authorization: BearerNever API_SECRET=hidden safe-text",
        "token_id": "public-token-id",
    })
    rendered = str(masked)
    assert "never-show" not in rendered and "BearerNever" not in rendered and "hidden" not in rendered
    assert masked["token_id"] == "public-token-id"



def test_emergency_cancel_failure_never_submits_parallel_sell():
    class CancelFailureAdapter(MockTradingAdapter):
        def __init__(self):
            super().__init__()
            self.create_calls = []

        async def cancel_order(self, order_id):
            return {"success": False, "status": "unknown"}

        async def create_order(self, order):
            self.create_calls.append(order)
            return await super().create_order(order)

    temp, base, strategy = build_repo()
    try:
        position = reserve_and_open(
            strategy, event="cancel-failure", shares=Decimal("5"), minimum=Decimal("1")
        )
        active = strategy.reserve_position_intent(
            position,
            action="EXIT",
            purpose="STOP_066",
            order_type="GTC",
            shares=Decimal("5"),
            price_limit=Decimal("0.55"),
            book_hash="stop",
        )
        strategy.update_intent(
            active["intent_id"], state="LIVE", remote_order_id="remote-stop"
        )
        position = strategy.position_for_token("token-cancel-failure")
        adapter = CancelFailureAdapter()
        runtime = LiveStrategyRuntime(
            LiveConfig(execution_mode="READ_ONLY"),
            base,
            strategy,
            adapter,
        )
        asyncio.run(runtime._market_exit_fak(
            position,
            {
                "asset_id": "token-cancel-failure",
                "best_bid": "0.60",
                "bids": [{"price": "0.60", "size": "5"}],
            },
            purpose="EMERGENCY_OPERATOR",
            min_price=Decimal("0.01"),
            frame_hash="emergency",
        ))
        assert adapter.create_calls == []
        assert strategy.intent(active["intent_id"])["state"] == "CANCEL_UNCERTAIN"
        assert strategy.position_for_token("token-cancel-failure")[
            "state"
        ] == "EXIT_RECONCILIATION_REQUIRED"
    finally:
        temp.cleanup()


def test_reconciliation_settles_entry_fill_and_position_across_restart():
    temp, base, strategy = build_repo()
    try:
        base.upsert_market({
            "event_id": "recovered-entry",
            "condition_id": "recovered-condition",
            "yes_token_id": "recovered-token",
            "no_token_id": "recovered-no",
            "token_mapping_status": "verified",
            "accepting_orders": True,
            "min_order_size": 5,
        })
        reservation = strategy.reserve_event_entry(
            event_id="recovered-entry", condition_id="recovered-condition",
            token_id="recovered-token", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        intent_id = reservation["entry_intent_id"]
        strategy.update_intent(intent_id, state="RECONCILIATION_REQUIRED", remote_order_id="r1")
        adapter = MockTradingAdapter()
        adapter.orders["r1"] = {
            "status": "filled",
            "fills": [{
                "polymarket_order_id": "r1", "polymarket_trade_id": "trade-r1",
                "price": "0.74", "size": "5", "fee": "0.02", "status": "matched",
            }],
        }
        adapter.positions = [{
            "condition_id": "recovered-condition", "token_id": "recovered-token",
            "outcome": "YES", "size": "5", "average_price": "0.74",
            "current_value": "3.7",
        }]
        restarted = StrategyRepository(LiveRepository(base.db_path))
        result = asyncio.run(
            ReconciliationWorker(base, adapter, restarted).run_once("restart-test")
        )
        assert result["status"] == "ok"
        position = restarted.position_for_token("recovered-token")
        assert position["remaining_shares_text"] == "5"
        assert position["cost_all_in_text"] == "3.72"
        assert restarted.intent(intent_id)["state"] == "FILLED"
    finally:
        temp.cleanup()


def test_reconciliation_failure_pauses_without_mutating_operator_kill_switch():
    class FailingAdapter(MockTradingAdapter):
        async def get_balance(self):
            raise RuntimeError("Secret Manager unavailable")

    temp, base, strategy = build_repo()
    try:
        base.set_state("kill_switch", "false", "operator")
        base.set_state("canary_armed", "true", "test")
        strategy.set_pause_entries(False, "operator", "test")
        result = asyncio.run(
            ReconciliationWorker(base, FailingAdapter(), strategy).run_once("test")
        )
        assert result["status"] == "failed"
        assert base.get_state("kill_switch") == "false"
        assert base.get_state("canary_armed") == "false"
        assert strategy.pause_entries()
        assert base.get_state("pause_auto_recoverable") == "false"

        recovered = asyncio.run(
            ReconciliationWorker(base, MockTradingAdapter(), strategy).run_once("test")
        )
        assert recovered["status"] == "ok"
        assert base.get_state("reconciliation_readiness") == "READY"
        assert base.get_state("kill_switch") == "false"
        assert strategy.pause_entries()
        assert base.get_state("pause_auto_recoverable") == "false"
    finally:
        temp.cleanup()


def test_manual_pause_cancels_reconciliation_auto_recovery():
    class FailingAdapter(MockTradingAdapter):
        async def get_balance(self):
            raise RuntimeError("HTTP 429 rate limit exceeded")

    temp, base, strategy = build_repo()
    try:
        base.set_state("kill_switch", "false", "operator")
        strategy.set_pause_entries(False, "operator", "READY")
        failed = asyncio.run(
            ReconciliationWorker(base, FailingAdapter(), strategy).run_once("test")
        )
        assert failed["status"] == "failed"
        assert base.get_state("pause_owner") == "RECONCILIATION"
        assert base.get_state("pause_auto_recoverable") == "true"

        strategy.set_pause_entries(True, "operator", "OPERATOR_PAUSE")
        clean = asyncio.run(
            ReconciliationWorker(base, MockTradingAdapter(), strategy).run_once("test")
        )
        assert clean["status"] == "ok"
        assert base.get_state("reconciliation_readiness") == "READY"
        assert base.get_state("kill_switch") == "false"
        assert strategy.pause_entries()
        assert base.get_state("pause_owner") == "OPERATOR"
        assert base.get_state("pause_auto_recoverable") == "false"
    finally:
        temp.cleanup()


def test_strategy_daily_loss_limit_locks_real_entry_path():
    temp, base, strategy = build_repo()
    try:
        config = LiveConfig(max_daily_realized_loss_usd=Decimal("10"))
        runtime = LiveStrategyRuntime(config, base, strategy, MockTradingAdapter())
        day_key = datetime.now(ZoneInfo("Asia/Jerusalem")).date().isoformat()
        base.current_daily_limit(day_key, "Asia/Jerusalem")
        with base.connect() as conn:
            conn.execute(
                "UPDATE live_daily_limits SET realized_pnl_usd='-10' WHERE day_key=?",
                (day_key,),
            )
            conn.commit()
        base.set_state("kill_switch", "false", "operator")
        base.set_state("canary_armed", "true", "test")
        strategy.set_pause_entries(False, "operator", "test")
        assert runtime._daily_loss_blocked()
        assert base.get_state("kill_switch") == "false"
        assert base.get_state("canary_armed") == "false"
        assert strategy.pause_entries()
    finally:
        temp.cleanup()


def test_configured_signer_mismatch_fails_before_network_client_creation():
    class MemorySecrets:
        values = {
            "POLYMARKET_PRIVATE_KEY": "0x" + "11" * 32,
            "POLYMARKET_API_KEY": "fake-api-key",
            "POLYMARKET_API_SECRET": "fake-api-secret",
            "POLYMARKET_API_PASSPHRASE": "fake-passphrase",
        }

        def get_secret(self, name):
            return self.values.get(name)

    config = LiveConfig(
        private_signing_readiness_enabled=True,
        signer_address="0x2222222222222222222222222222222222222222",
        funder_address="0x3333333333333333333333333333333333333333",
        signature_type=3,
    )
    result = asyncio.run(
        RealPolymarketTradingAdapter(
            config, secret_provider=MemorySecrets()
        ).identity_preflight()
    )
    assert result["status"] == "SIGNER_MISMATCH"
    assert result["signer"] is None


def test_entry_schedule_blocks_weekdays_14_to_23_jerusalem():
    jerusalem = ZoneInfo("Asia/Jerusalem")
    assert LiveStrategyRuntime.entry_schedule_status(
        datetime(2026, 8, 3, 13, 59, 59, tzinfo=jerusalem)
    )["allowed"]
    assert not LiveStrategyRuntime.entry_schedule_status(
        datetime(2026, 8, 3, 14, 0, 0, tzinfo=jerusalem)
    )["allowed"]
    assert not LiveStrategyRuntime.entry_schedule_status(
        datetime(2026, 8, 3, 22, 59, 59, tzinfo=jerusalem)
    )["allowed"]
    assert LiveStrategyRuntime.entry_schedule_status(
        datetime(2026, 8, 3, 23, 0, 0, tzinfo=jerusalem)
    )["allowed"]
    assert LiveStrategyRuntime.entry_schedule_status(
        datetime(2026, 8, 8, 16, 0, 0, tzinfo=jerusalem)
    )["allowed"]


def test_entry_schedule_is_unrestricted_only_on_2026_08_21_jerusalem():
    jerusalem = ZoneInfo("Asia/Jerusalem")
    assert LiveStrategyRuntime.entry_schedule_status(
        datetime(2026, 8, 21, 14, 0, 0, tzinfo=jerusalem)
    )["allowed"]
    assert LiveStrategyRuntime.entry_schedule_status(
        datetime(2026, 8, 21, 22, 59, 59, tzinfo=jerusalem)
    )["allowed"]
    assert not LiveStrategyRuntime.entry_schedule_status(
        datetime(2026, 8, 20, 16, 0, 0, tzinfo=jerusalem)
    )["allowed"]
    assert not LiveStrategyRuntime.entry_schedule_status(
        datetime(2026, 8, 24, 16, 0, 0, tzinfo=jerusalem)
    )["allowed"]


def test_continuous_entry_slot_is_atomic_for_intents_and_positions():
    temporary, base, strategy = build_repo()
    try:
        first = strategy.reserve_event_entry(
            event_id="event-slot-1", condition_id="condition-slot-1",
            token_id="token-slot-1", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT", require_empty_slot=True,
        )
        assert not first.get("_blocked")
        blocked_by_intent = strategy.reserve_event_entry(
            event_id="event-slot-2", condition_id="condition-slot-2",
            token_id="token-slot-2", side="NO", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT", require_empty_slot=True,
        )
        assert blocked_by_intent["reason"] == "ACTIVE_ENTRY_SLOT_OCCUPIED"
        strategy.open_position(
            event_id="event-slot-1", condition_id="condition-slot-1",
            token_id="token-slot-1", outcome="YES", shares=Decimal("5"),
            average_price=Decimal("0.74"), cost_all_in=Decimal("3.8"),
            fees=Decimal("0"), min_sellable=Decimal("1"),
        )
        blocked_by_position = strategy.reserve_event_entry(
            event_id="event-slot-3", condition_id="condition-slot-3",
            token_id="token-slot-3", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT", require_empty_slot=True,
        )
        assert blocked_by_position["reason"] == "ACTIVE_ENTRY_SLOT_OCCUPIED"
        assert blocked_by_position["blocker_kind"] == "POSITION"
        assert blocked_by_position["blocking_state"] == "OPEN"
    finally:
        temporary.cleanup()


def test_closed_dust_does_not_occupy_continuous_entry_slot():
    temporary, base, strategy = build_repo()
    try:
        first = strategy.reserve_event_entry(
            event_id="closed-dust-event", condition_id="closed-dust-condition",
            token_id="closed-dust-token", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT", require_empty_slot=True,
        )
        strategy.open_position(
            event_id="closed-dust-event", condition_id="closed-dust-condition",
            token_id="closed-dust-token", outcome="YES", shares=Decimal("0.006664"),
            average_price=Decimal("0.74"), cost_all_in=Decimal("0.00493136"),
            fees=Decimal("0"), sellable_shares=Decimal("0"),
            min_sellable=Decimal("1"), entry_intent_id=first["entry_intent_id"],
        )
        with base.connect() as conn:
            conn.execute(
                "UPDATE live_strategy_positions SET closed_at=? WHERE event_id=?",
                ("2026-08-11T21:09:32+00:00", "closed-dust-event"),
            )
            conn.commit()

        second = strategy.reserve_event_entry(
            event_id="event-after-closed-dust", condition_id="condition-after-closed-dust",
            token_id="token-after-closed-dust", side="NO", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT", require_empty_slot=True,
        )

        assert not second.get("_blocked")
        assert second["entry_intent_id"]
    finally:
        temporary.cleanup()


def test_open_dust_still_occupies_continuous_entry_slot_fail_closed():
    temporary, _base, strategy = build_repo()
    try:
        first = strategy.reserve_event_entry(
            event_id="open-dust-event", condition_id="open-dust-condition",
            token_id="open-dust-token", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT", require_empty_slot=True,
        )
        position = strategy.open_position(
            event_id="open-dust-event", condition_id="open-dust-condition",
            token_id="open-dust-token", outcome="YES", shares=Decimal("0.006664"),
            average_price=Decimal("0.74"), cost_all_in=Decimal("0.00493136"),
            fees=Decimal("0"), sellable_shares=Decimal("0"),
            min_sellable=Decimal("1"), entry_intent_id=first["entry_intent_id"],
        )
        assert position["state"] == "DUST"
        assert position["closed_at"] is None

        assert strategy.repair_terminal_dust_slots(actor="test") == []
        blocked = strategy.reserve_event_entry(
            event_id="event-after-open-dust", condition_id="condition-after-open-dust",
            token_id="token-after-open-dust", side="NO", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT", require_empty_slot=True,
        )

        assert blocked["reason"] == "ACTIVE_ENTRY_SLOT_OCCUPIED"
        assert blocked["blocker_kind"] == "POSITION"
        assert blocked["blocking_state"] == "DUST"
        assert blocked["blocking_closed_at"] is None
        assert blocked["blocking_remaining_shares_text"] == "0.006664"
        assert blocked["blocking_sellable_shares_text"] == "0"
    finally:
        temporary.cleanup()


def test_critical_074_frame_survives_regular_frame_conflation():
    async def scenario():
        runtime = LiveStrategyRuntime.__new__(LiveStrategyRuntime)

        runtime.policy = StrategyPolicy()
        runtime._pending_frames = __import__("collections").OrderedDict()
        runtime._critical_frames = __import__("collections").deque()
        runtime._critical_price_state = {}
        runtime._frame_queue_capacity = 32
        runtime.frames_coalesced = 0
        runtime.frames_dropped = 0
        runtime.critical_triggers_queued = 0
        runtime.critical_triggers_processed = 0
        runtime.critical_triggers_dropped = 0
        runtime.max_critical_queue_depth = 0
        runtime._frame_event = asyncio.Event()
        runtime._frame_task = None
        runtime._stop = asyncio.Event()

        runtime._observe_entry_trigger = lambda _context: None
        runtime.enabled = lambda: True

        processed = []

        async def fake_process(context):
            processed.append(context)

        runtime.process_atomic_frame = fake_process

        def frame(ask, number):
            return {
                "event_type": "price_change",
                "message_hash": f"frame-{number}",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "updates": [{
                    "condition_id": "condition-1",
                    "asset_id": "yes-token",
                    "outcome": "YES",
                    "best_ask": ask,
                    "best_bid": "0.73",
                    "generation": 1,
                    "update_number": number,
                    "exchange_timestamp_ms": int(
                        datetime.now(timezone.utc).timestamp() * 1000
                    ),
                }],
                "event_readiness": {
                    "condition-1": {
                        "ready": True,
                        "reason": "READY",
                    }
                },
            }

        # No await between these calls: this recreates a burst in which the
        # normal queue would otherwise collapse 0.73 -> 0.74 -> 0.75.
        runtime.schedule_frame(frame("0.73", 1))
        runtime.schedule_frame(frame("0.74", 2))
        runtime.schedule_frame(frame("0.75", 3))

        runtime._stop.set()
        runtime._frame_event.set()

        await asyncio.wait_for(runtime._frame_task, timeout=1)

        asks = [
            context["updates"][0]["best_ask"]
            for context in processed
        ]

        # Critical 0.74 is evaluated first and survives; ordinary state
        # collapses to the latest 0.75 frame.
        assert asks == ["0.74", "0.75"]
        assert processed[0]["_critical_trigger"] is True
        assert (
            processed[0]["updates"][0]["_critical_trigger_latched"]
            is True
        )

        assert runtime.critical_triggers_queued == 1
        assert runtime.critical_triggers_processed == 1
        assert runtime.critical_triggers_dropped == 0
        assert runtime.frames_coalesced == 1

    asyncio.run(scenario())


def test_latched_critical_trigger_is_not_rejected_as_frame_superseded():
    runtime = LiveStrategyRuntime.__new__(LiveStrategyRuntime)

    runtime.config = type(
        "CriticalTriggerConfig",
        (),
        {"max_market_data_age_seconds": 2},
    )()

    runtime._market_freshness = lambda _condition_id: {
        "ready": True,
        "reason": "READY",
        "book_versions": {
            "yes-token": {
                "generation": 1,
                "update_number": 3,
            }
        },
    }

    runtime.paper_mode = lambda: False

    trigger_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    latched = runtime._freshness(
        "condition-1",
        {
            "asset_id": "yes-token",
            "generation": 1,
            "update_number": 2,
            "exchange_timestamp_ms": trigger_ms,
            "_critical_trigger_latched": True,
        },
    )

    assert latched["ready"] is True
    assert latched["critical_trigger_latched"] is True
    assert latched["critical_trigger_age_ms"] <= 2000

    regular = runtime._freshness(
        "condition-1",
        {
            "asset_id": "yes-token",
            "generation": 1,
            "update_number": 2,
            "exchange_timestamp_ms": trigger_ms,
        },
    )

    assert regular["ready"] is False
    assert regular["reason"] == "FRAME_SUPERSEDED"



def test_latched_entry_waits_for_transient_depth_alignment_only():
    runtime = LiveStrategyRuntime.__new__(LiveStrategyRuntime)
    runtime.config = type(
        "CriticalTriggerConfig",
        (),
        {"max_market_data_age_seconds": 2},
    )()
    runtime.critical_alignment_waits = 0
    runtime.critical_alignment_recoveries = 0
    runtime.critical_alignment_timeouts = 0

    states = [
        {"ready": False, "reason": "BEST_PRICE_PENDING_DEPTH"},
        {"ready": False, "reason": "BEST_PRICE_PENDING_DEPTH"},
        {
            "ready": True,
            "reason": "READY",
            "book_versions": {
                "yes-token": {"generation": 1, "update_number": 3}
            },
        },
    ]

    def freshness(_condition_id):
        if len(states) > 1:
            return states.pop(0)
        return states[0]

    runtime._market_freshness = freshness
    trigger = {
        "asset_id": "yes-token",
        "generation": 1,
        "update_number": 2,
        "exchange_timestamp_ms": int(
            datetime.now(timezone.utc).timestamp() * 1000
        ),
        "_critical_trigger_latched": True,
        "_critical_entry_latched": True,
    }
    recovered = asyncio.run(
        runtime._freshness_with_alignment_grace("condition-1", trigger)
    )

    assert recovered["ready"] is True
    assert recovered["alignment_grace_recovered"] is True
    assert recovered["alignment_grace_wait_ms"] > 0
    assert runtime.critical_alignment_waits == 1
    assert runtime.critical_alignment_recoveries == 1
    assert runtime.critical_alignment_timeouts == 0

    runtime._market_freshness = lambda _condition_id: {
        "ready": False,
        "reason": "BEST_PRICE_MISMATCH",
    }
    blocked = asyncio.run(
        runtime._freshness_with_alignment_grace("condition-1", trigger)
    )
    assert blocked["reason"] == "BEST_PRICE_MISMATCH"
    assert runtime.critical_alignment_waits == 1


def test_single_stop_edge_survives_conflation():
    async def scenario():
        runtime = LiveStrategyRuntime.__new__(LiveStrategyRuntime)

        runtime.policy = StrategyPolicy()
        runtime._pending_frames = __import__("collections").OrderedDict()
        runtime._critical_frames = __import__("collections").deque()
        runtime._critical_price_state = {}
        runtime._frame_queue_capacity = 32

        runtime.frames_coalesced = 0
        runtime.frames_dropped = 0

        runtime.critical_triggers_queued = 0
        runtime.critical_triggers_processed = 0
        runtime.critical_triggers_dropped = 0
        runtime.max_critical_queue_depth = 0

        runtime._frame_event = asyncio.Event()
        runtime._frame_task = None
        runtime._stop = asyncio.Event()

        runtime._observe_entry_trigger = lambda _context: None
        runtime.enabled = lambda: True

        processed = []

        async def fake_process(context):
            processed.append(context)

        runtime.process_atomic_frame = fake_process

        def frame(bid, number):
            return {
                "event_type": "price_change",
                "message_hash": f"frame-{number}",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "updates": [{
                    "condition_id": "condition-1",
                    "asset_id": "yes-token",
                    "outcome": "YES",
                    "best_ask": "0.80",
                    "best_bid": bid,
                    "generation": 1,
                    "update_number": number,
                    "exchange_timestamp_ms": int(
                        datetime.now(timezone.utc).timestamp() * 1000
                    ),
                }],
                "event_readiness": {
                    "condition-1": {
                        "ready": True,
                        "reason": "READY",
                    }
                },
            }

        # No awaits: recreate an ingress burst.
        runtime.schedule_frame(frame("0.67", 1))
        runtime.schedule_frame(frame("0.66", 2))

        # Still 0.66: must NOT create another critical edge.
        runtime.schedule_frame(frame("0.66", 3))

        runtime.schedule_frame(frame("0.65", 4))
        runtime.schedule_frame(frame("0.60", 5))
        runtime.schedule_frame(frame("0.59", 6))

        runtime._stop.set()
        runtime._frame_event.set()

        await asyncio.wait_for(runtime._frame_task, timeout=1)

        critical = [
            context for context in processed
            if context.get("_critical_trigger")
        ]

        assert len(critical) == 1

        assert critical[0]["_critical_trigger_types"] == [
            "STOP_066"
        ]
        assert (
            critical[0]["updates"][0]["_critical_stop_latched"]
            is True
        )

        # Exact 0.66 appearing on two consecutive frames is one edge,
        # not two critical queue entries.
        assert runtime.critical_triggers_queued == 1
        assert runtime.critical_triggers_processed == 1
        assert runtime.critical_triggers_dropped == 0

        # Latest ordinary state still survives independently.
        assert processed[-1]["updates"][0]["best_bid"] == "0.59"

    asyncio.run(scenario())


def test_entry_exact_price_is_edge_triggered_not_level_triggered():
    async def scenario():
        runtime = LiveStrategyRuntime.__new__(LiveStrategyRuntime)

        runtime.policy = StrategyPolicy()
        runtime._pending_frames = __import__("collections").OrderedDict()
        runtime._critical_frames = __import__("collections").deque()
        runtime._critical_price_state = {}
        runtime._frame_queue_capacity = 32

        runtime.frames_coalesced = 0
        runtime.frames_dropped = 0

        runtime.critical_triggers_queued = 0
        runtime.critical_triggers_processed = 0
        runtime.critical_triggers_dropped = 0
        runtime.max_critical_queue_depth = 0

        runtime._frame_event = asyncio.Event()
        runtime._frame_task = None
        runtime._stop = asyncio.Event()

        runtime._observe_entry_trigger = lambda _context: None
        runtime.enabled = lambda: True

        processed = []

        async def fake_process(context):
            processed.append(context)

        runtime.process_atomic_frame = fake_process

        def frame(ask, number):
            return {
                "event_type": "price_change",
                "message_hash": f"entry-{number}",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "updates": [{
                    "condition_id": "condition-1",
                    "asset_id": "yes-token",
                    "outcome": "YES",
                    "best_ask": ask,
                    "best_bid": "0.73",
                    "generation": 1,
                    "update_number": number,
                    "exchange_timestamp_ms": int(
                        datetime.now(timezone.utc).timestamp() * 1000
                    ),
                }],
                "event_readiness": {
                    "condition-1": {
                        "ready": True,
                        "reason": "READY",
                    }
                },
            }

        runtime.schedule_frame(frame("0.73", 1))
        runtime.schedule_frame(frame("0.74", 2))
        runtime.schedule_frame(frame("0.74", 3))
        runtime.schedule_frame(frame("0.75", 4))
        runtime.schedule_frame(frame("0.74", 5))

        runtime._stop.set()
        runtime._frame_event.set()

        await asyncio.wait_for(runtime._frame_task, timeout=1)

        critical = [
            context for context in processed
            if context.get("_critical_trigger")
        ]

        assert len(critical) == 2
        assert all(
            context["_critical_trigger_types"] == ["ENTRY_074"]
            for context in critical
        )
        assert runtime.critical_triggers_queued == 2
        assert runtime.critical_triggers_dropped == 0

    asyncio.run(scenario())


def test_hot_state_snapshot_combines_safety_lock_and_exposure():
    temp, base, strategy = build_repo()

    try:
        base.set_state("pause_entries", "false", "test")
        base.set_state("kill_switch", "false", "operator")
        base.set_state("canary_armed", "true", "test")
        base.set_state(
            "reconciliation_readiness",
            "READY",
            "test",
        )

        reserve_and_open(
            strategy,
            event="hot-state-event",
            shares=Decimal("10"),
        )

        snapshot = strategy.hot_state_snapshot()

        assert snapshot["pause_entries"] is False
        assert snapshot["kill_switch"] is False
        assert snapshot["canary_armed"] is True
        assert (
            snapshot["reconciliation_readiness"]
            == "READY"
        )
        assert (
            "hot-state-event"
            in snapshot["locked_event_ids"]
        )
        assert snapshot["active_exposure"] == Decimal("5")

    finally:
        temp.cleanup()


def test_stale_ram_entry_state_fails_closed():
    runtime = LiveStrategyRuntime.__new__(
        LiveStrategyRuntime
    )

    runtime._hot_state = {
        "pause_entries": False,
        "kill_switch": False,
        "canary_armed": True,
        "canary_consumed": False,
        "reconciliation_readiness": "READY",
        "locked_event_ids": set(),
        "active_exposure": Decimal("0"),
    }

    runtime._hot_state_max_age_seconds = 1.0
    runtime._hot_state_refreshed_monotonic = (
        __import__("time").monotonic() - 2.0
    )

    state = runtime._entry_state_from_ram("event-1")

    assert state["stale"] is True
    assert state["paused"] is True
    assert state["event_locked"] is False
    assert state["active_exposure"] == Decimal("0")


def test_entry_decision_hot_path_does_not_query_durable_state():
    inspect = __import__("inspect")

    source = (
        inspect.getsource(
            LiveStrategyRuntime._observe_entry_trigger
        )
        + inspect.getsource(
            LiveStrategyRuntime._process_event
        )
    )

    forbidden = (
        "self.repo.pause_entries()",
        "self.base.kill_switch_active()",
        'self.base.get_state("canary_armed"',
        "self.repo.event_state(",
        "self.repo.exposure()",
    )

    for item in forbidden:
        assert item not in source

    # Daily-loss SQLite work is now trigger-only.
    process_source = inspect.getsource(
        LiveStrategyRuntime._process_event
    )

    assert "if has_trigger" in process_source
    assert "self._daily_loss_blocked()" in process_source


def test_hot_state_snapshot_contains_active_position_and_tp_state():
    temp, base, strategy = build_repo()

    try:
        position = reserve_and_open(
            strategy,
            event="ram-position-event",
            shares=Decimal("10"),
        )

        intent = strategy.reserve_position_intent(
            position,
            action="TP",
            purpose="TAKE_PROFIT",
            order_type="GTC",
            shares=Decimal("10"),
            price_limit=Decimal("0.96"),
            book_hash="ram-test",
        )

        strategy.update_intent(
            str(intent["intent_id"]),
            state="LIVE",
        )

        snapshot = strategy.hot_state_snapshot()

        positions = snapshot[
            "positions_by_token"
        ]["token-ram-position-event"]

        assert len(positions) == 1

        cached = positions[0]

        assert (
            cached["position_id"]
            == position["position_id"]
        )
        assert (
            cached["tp_intent_id"]
            == intent["intent_id"]
        )
        assert cached["tp_intent_state"] == "LIVE"

    finally:
        temp.cleanup()


class _NullExitTracker:
    """Exit supervision heartbeat stub: records nothing, touches no DB."""

    def note(self, position, *, update, decision):
        return None

    def fault(self, position, *, reason, message):
        return None

    def unsellable_remainder(self, position, minimum):
        return False

    def waiting_sellable_sla_exceeded(self, position):
        return False


def test_manage_position_neutral_frame_uses_ram_only():
    runtime = LiveStrategyRuntime.__new__(
        LiveStrategyRuntime
    )

    runtime.policy = StrategyPolicy()

    runtime._hot_state = {
        "reconciliation_readiness": "READY",
        "positions_by_token": {
            "token-1": [{
                "position_id": "position-1",
                "event_id": "event-1",
                "condition_id": "condition-1",
                "token_id": "token-1",
                "outcome": "YES",
                "state": "TP_OPEN",
                "remaining_shares_text": "5",
                "sellable_shares_text": "5",
                "stop_stage": 0,
                "tp_intent_id": "tp-1",
                "tp_intent_state": "LIVE",
                "active_exit_intent_id": None,
            }]
        },
    }

    runtime.paper_mode = lambda: False
    # Production always installs the RAM market cache, and exit supervision now
    # needs the market's minimum order size to tell an unsellable remainder
    # from a sellable one. Serve it from the cache so the assertion under test
    # stays "no SQLite on a neutral frame" rather than "no minimum lookup".
    runtime._market_provider = lambda condition_id: {
        "condition_id": condition_id,
        "minimum_order_size": "5",
    }
    runtime._exit_tracker = _NullExitTracker()

    class ExplodingRepo:
        def __getattr__(self, name):
            raise AssertionError(
                f"Unexpected DB repository read: {name}"
            )

    runtime.repo = ExplodingRepo()

    asyncio.run(
        runtime._manage_position(
            market={},
            update={
                "asset_id": "token-1",
                "best_bid": "0.70",
            },
            event_ready=True,
            frame_hash="neutral-frame",
        )
    )


def test_manage_position_has_no_unconditional_position_db_reads():
    inspect = __import__("inspect")

    source = inspect.getsource(
        LiveStrategyRuntime._manage_position
    )

    forbidden = (
        "self.repo.active_positions(",
        "self.repo.position_for_token(",
        "self.repo.intent(",
        'self.base.get_state("reconciliation_readiness"',
    )

    for item in forbidden:
        assert item not in source


def test_persistence_depth_materialization_is_sorted_off_hot_path():
    from live.market_websocket import MarketWebSocketManager

    snapshot = {
        "asset_id": "token-1",
        "_persistence_bid_items": (
            (Decimal("0.68"), Decimal("2")),
            (Decimal("0.70"), Decimal("1")),
            (Decimal("0.69"), Decimal("3")),
        ),
        "_persistence_ask_items": (
            (Decimal("0.74"), Decimal("5")),
            (Decimal("0.72"), Decimal("4")),
            (Decimal("0.73"), Decimal("6")),
        ),
    }

    result = (
        MarketWebSocketManager
        ._materialize_persistence_snapshot(snapshot)
    )

    assert result["bids"] == [
        {"price": "0.7", "size": "1"},
        {"price": "0.69", "size": "3"},
        {"price": "0.68", "size": "2"},
    ]

    assert result["asks"] == [
        {"price": "0.72", "size": "4"},
        {"price": "0.73", "size": "6"},
        {"price": "0.74", "size": "5"},
    ]

    assert "_persistence_bid_items" not in result
    assert "_persistence_ask_items" not in result


def test_market_process_message_does_not_build_full_depth_for_persistence():
    import inspect
    from live.market_websocket import MarketWebSocketManager

    source = inspect.getsource(
        MarketWebSocketManager.process_message
    )

    assert 'book.levels("bids")' not in source
    assert 'book.levels("asks")' not in source
    assert "_persistence_bid_items" in source
    assert "_persistence_ask_items" in source


def test_market_persistence_batch_reuses_one_sqlite_connection():
    from live.market_websocket import MarketWebSocketManager

    temp, base, _strategy = build_repo()

    try:
        connection_calls = 0
        real_connect = base.connect

        def counted_connect():
            nonlocal connection_calls
            connection_calls += 1
            return real_connect()

        base.connect = counted_connect

        manager = MarketWebSocketManager(base)

        first = {
            "condition_id": "condition-persist",
            "event_id": "event-persist",
            "asset_id": "token-persist",
            "outcome": "YES",
            "event_type": "best_bid_ask",
            "best_bid": "0.70",
            "best_ask": "0.71",
            "message_hash": "persist-1",
            "raw_message": {"sequence": 1},
        }

        second = {
            **first,
            "best_bid": "0.71",
            "best_ask": "0.72",
            "message_hash": "persist-2",
            "raw_message": {"sequence": 2},
        }

        manager._persistence_batch_sync(
            [first],
            {"market_ws_status": "CONNECTED"},
        )

        manager._persistence_batch_sync(
            [second],
            {"strategy_readiness": "READY"},
        )

        # The dedicated writer owns exactly one SQLite connection.
        assert connection_calls == 1

        manager._close_persistence_connection_sync()

    finally:
        temp.cleanup()


def test_market_cache_refresh_publishes_atomically():
    from pathlib import Path
    from live.market_websocket import (
        MarketWebSocketManager,
    )

    old_market = {
        "condition_id": "old-condition",
        "yes_token_id": "old-yes",
        "no_token_id": "old-no",
    }

    new_market = {
        "condition_id": "new-condition",
        "yes_token_id": "new-yes",
        "no_token_id": "new-no",
    }

    class FakeRepo:
        db_path = Path("/tmp/fake-market-cache.sqlite3")

        def market_for_asset(self, asset_id):
            # While the replacement cache is being constructed,
            # readers must continue seeing the OLD complete cache.
            assert (
                manager._markets_by_condition.get(
                    "old-condition"
                )
                is old_market
            )

            if asset_id in {
                "new-yes",
                "new-no",
            }:
                return new_market

            return None

    manager = MarketWebSocketManager(
        FakeRepo()
    )

    manager._markets_by_asset = {
        "old-yes": old_market,
        "old-no": old_market,
    }

    manager._markets_by_condition = {
        "old-condition": old_market,
    }

    manager._refresh_market_cache([
        "new-yes",
        "new-no",
    ])

    assert set(manager._markets_by_asset) == {
        "new-yes",
        "new-no",
    }

    assert set(manager._markets_by_condition) == {
        "new-condition",
    }


def test_market_for_condition_never_uses_sqlite_fallback():
    from pathlib import Path
    from live.market_websocket import (
        MarketWebSocketManager,
    )

    class ExplodingRepo:
        db_path = Path("/tmp/fake-no-fallback.sqlite3")

        def latest_market(self, *_args, **_kwargs):
            raise AssertionError(
                "SQLite fallback must not run"
            )

        def market_for_asset(
            self,
            *_args,
            **_kwargs,
        ):
            raise AssertionError(
                "SQLite fallback must not run"
            )

    manager = MarketWebSocketManager(
        ExplodingRepo()
    )

    assert (
        manager.market_for_condition(
            "missing-condition"
        )
        is None
    )

    assert manager.market_cache_misses == 1


def test_market_process_message_has_no_sync_metadata_db_fallback():
    import inspect
    from live.market_websocket import (
        MarketWebSocketManager,
    )

    source = inspect.getsource(
        MarketWebSocketManager.process_message
    )

    forbidden = (
        "self.repo.market_for_asset(",
        "self.repo.market_ws_asset_ids(",
        "self._refresh_market_cache(",
    )

    for needle in forbidden:
        assert needle not in source


def test_market_cache_miss_schedules_background_refresh():
    from pathlib import Path
    from live.market_websocket import (
        MarketWebSocketManager,
    )

    class FakeRepo:
        db_path = Path(
            "/tmp/fake-background-cache.sqlite3"
        )

    async def scenario():
        manager = MarketWebSocketManager(
            FakeRepo()
        )

        manager.subscribed_asset_ids = [
            "yes-token",
            "no-token",
        ]

        calls = []

        def fake_refresh(asset_ids):
            calls.append(tuple(asset_ids))

        manager._refresh_market_cache = (
            fake_refresh
        )

        manager._request_market_cache_refresh([
            "yes-token",
        ])

        await asyncio.wait_for(
            manager._market_cache_refresh_task,
            timeout=1,
        )

        assert calls == [(
            "no-token",
            "yes-token",
        )]

    asyncio.run(scenario())


def test_order_book_preserves_intermediate_exact_ask_transition():
    from live.order_book import OrderBookSet

    books = OrderBookSet(["token-transition"])

    books.apply({
        "event_type": "book",
        "asset_id": "token-transition",
        "bids": [
            {"price": "0.70", "size": "10"},
        ],
        "asks": [
            {"price": "0.73", "size": "10"},
            {"price": "0.74", "size": "10"},
            {"price": "0.75", "size": "10"},
        ],
        "timestamp": "100",
    })

    frame = books.apply({
        "event_type": "price_change",
        "price_changes": [
            {
                "asset_id": "token-transition",
                "side": "SELL",
                "price": "0.73",
                "size": "0",
                "best_bid": "0.70",
                "best_ask": "0.74",
            },
            {
                "asset_id": "token-transition",
                "side": "SELL",
                "price": "0.74",
                "size": "0",
                "best_bid": "0.70",
                "best_ask": "0.75",
            },
        ],
        "timestamp": "101",
    })

    assert frame.updates[0]["best_ask"] == "0.75"

    assert [
        item["best_ask"]
        for item in frame.top_transitions
    ] == [
        "0.74",
        "0.75",
    ]


def test_order_book_preserves_stop_then_emergency_transition_order():
    from live.order_book import OrderBookSet

    books = OrderBookSet(["token-stop"])

    books.apply({
        "event_type": "book",
        "asset_id": "token-stop",
        "bids": [
            {"price": "0.67", "size": "10"},
            {"price": "0.66", "size": "10"},
            {"price": "0.60", "size": "10"},
        ],
        "asks": [
            {"price": "0.70", "size": "10"},
        ],
        "timestamp": "200",
    })

    frame = books.apply({
        "event_type": "price_change",
        "price_changes": [
            {
                "asset_id": "token-stop",
                "side": "BUY",
                "price": "0.67",
                "size": "0",
                "best_bid": "0.66",
                "best_ask": "0.70",
            },
            {
                "asset_id": "token-stop",
                "side": "BUY",
                "price": "0.66",
                "size": "0",
                "best_bid": "0.60",
                "best_ask": "0.70",
            },
        ],
        "timestamp": "201",
    })

    assert [
        Decimal(item["best_bid"])
        for item in frame.top_transitions
    ] == [
        Decimal("0.66"),
        Decimal("0.60"),
    ]


def test_strategy_queues_only_first_stop_transition_in_message():
    import asyncio
    from collections import OrderedDict, deque
    from types import SimpleNamespace

    from live.strategy_runtime import LiveStrategyRuntime

    class ActiveTask:
        def done(self):
            return False

    async def scenario():
        runtime = LiveStrategyRuntime.__new__(
            LiveStrategyRuntime
        )

        runtime.policy = SimpleNamespace(
            entry_price=Decimal("0.74"),
            stop_price=Decimal("0.66"),
        )

        runtime._critical_price_state = {}
        runtime._critical_frames = deque()
        runtime._pending_frames = OrderedDict()
        runtime._frame_queue_capacity = 64
        runtime._frame_task = ActiveTask()
        runtime._frame_event = asyncio.Event()

        runtime.frames_coalesced = 0
        runtime.frames_dropped = 0
        runtime.critical_triggers_queued = 0
        runtime.max_critical_queue_depth = 0

        runtime.enabled = lambda: True
        runtime._observe_entry_trigger = (
            lambda _context: None
        )

        context = {
            "event_type": "price_change",
            "message_hash": "ordered-critical",
            "received_at": "2026-08-08T00:00:00+00:00",
            "updates": [
                {
                    "condition_id": "condition-1",
                    "asset_id": "token-1",
                    "best_bid": "0.59",
                    "best_ask": "0.70",
                    "exchange_timestamp_ms": 1000,
                },
            ],
            "top_transitions": [
                {
                    "condition_id": "condition-1",
                    "asset_id": "token-1",
                    "best_bid": "0.66",
                    "best_ask": "0.70",
                    "exchange_timestamp_ms": 1000,
                },
                {
                    "condition_id": "condition-1",
                    "asset_id": "token-1",
                    "best_bid": "0.60",
                    "best_ask": "0.70",
                    "exchange_timestamp_ms": 1000,
                },
            ],
            "event_readiness": {
                "condition-1": {
                    "ready": True,
                    "reason": "READY",
                },
            },
        }

        runtime.schedule_frame(context)

        assert len(runtime._critical_frames) == 1

        critical = runtime._critical_frames[0]

        latched = [
            update
            for update in critical["updates"]
            if update.get(
                "_critical_trigger_latched"
            )
        ]

        assert [
            update["best_bid"]
            for update in latched
        ] == ["0.66"]

        assert (
            latched[0][
                "_critical_stop_latched"
            ]
            is True
        )

        assert critical["_critical_trigger_types"] == [
            "STOP_066"
        ]

    asyncio.run(scenario())


def test_adapter_fak_no_match_exception_is_deterministic_zero_fill():
    fake = FakeResponseClient(
        RuntimeError(
            "No orders found to match with FAK order. "
            "FAK orders are partially filled or killed if no match is found."
        )
    )
    result = asyncio.run(
        RealPolymarketTradingAdapter(armed_config(), secure_client=fake).create_order(
            _adapter_entry_order()
        )
    )
    assert result["success"] is False
    assert result["status"] == "rejected"
    assert result["failure_reason"] == "FAK_NOT_FILLED"
    assert len(fake.posted) == 1


def test_zero_fill_without_remote_id_is_terminal_and_reconciliation_clean():
    temp, base, strategy = build_repo()
    try:
        strategy.set_pause_entries(False, "operator", "PRECONDITION")
        attempt = strategy.reserve_event_entry(
            event_id="zero-event", condition_id="zero-condition",
            token_id="zero-token", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        strategy.mark_zero_fill(
            "zero-event", "FAK_ZERO_FILL", intent_id=attempt["entry_intent_id"]
        )
        result = asyncio.run(
            ReconciliationWorker(base, MockTradingAdapter(), strategy).run_once("test")
        )
        assert result["status"] == "ok"
        assert result["gaps"] == []
        assert not strategy.pause_entries()
        intent = strategy.intent(attempt["entry_intent_id"])
        assert intent["state"] == "ZERO_FILL"
        assert intent["remote_order_id"] is None
    finally:
        temp.cleanup()


def test_reconciliation_submission_handoff_expires_fail_closed():
    temp, base, strategy = build_repo()
    try:
        strategy.set_pause_entries(False, "operator", "PRECONDITION")
        attempt = strategy.reserve_event_entry(
            event_id="handoff-event", condition_id="handoff-condition",
            token_id="handoff-token", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        worker = ReconciliationWorker(base, MockTradingAdapter(), strategy)
        fresh = asyncio.run(worker.run_once("test"))
        assert fresh["status"] == "ok"
        assert fresh["gaps"] == []
        assert not strategy.pause_entries()

        with base.connect() as conn:
            conn.execute(
                "UPDATE live_strategy_intents SET created_at=?,updated_at=? "
                "WHERE intent_id=?",
                ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00",
                 attempt["entry_intent_id"]),
            )
            conn.commit()
        expired = asyncio.run(worker.run_once("test"))
        assert expired["status"] == "gaps"
        assert expired["gaps"][0]["type"] == "durable_intent_without_remote_id"
        assert strategy.pause_entries()
        assert base.get_state("pause_auto_recoverable") == "false"
    finally:
        temp.cleanup()


# --- REMOTE_MATCHED_ZERO_FILL propagation race -----------------------------
#
# Two confirmed production incidents (2026-08-23, 09:58 and 12:18): a FAK
# ENTRY order comes back from Polymarket's order-status endpoint as
# "matched" before get_trades()/get_positions() have propagated the fill.
# The old code treated any terminal status with filled==0 as a confirmed
# zero-fill, immediately calling mark_zero_fill(). Once terminal, the intent
# left unresolved_intents() forever, so the fill that showed up 6.5-8s later
# could never resolve it through the normal path -- only the unrelated
# remote_positions loop caught it, as an "unexplained" new position, which
# is always classified RECONCILIATION_CONTRADICTION / MANUAL_ONLY.

def _reserve_matched_pending_entry(
    strategy, base, adapter, *, event_id="race-event", order_status="matched",
):
    condition_id = f"condition-{event_id}"
    token_id = f"token-{event_id}"
    base.upsert_market({
        "event_id": event_id, "condition_id": condition_id,
        "yes_token_id": token_id, "no_token_id": f"other-{event_id}",
        "token_mapping_status": "verified", "accepting_orders": True,
        "min_order_size": "5",
    })
    strategy.set_pause_entries(False, "operator", "PRECONDITION")
    attempt = strategy.reserve_event_entry(
        event_id=event_id, condition_id=condition_id, token_id=token_id,
        side="YES", simultaneous=False, reason_code="ENTRY_PRICE_EXACT",
    )
    order_id = f"order-{event_id}"
    strategy.update_intent(
        attempt["entry_intent_id"], remote_order_id=order_id, submitted_at=now_iso(),
    )
    adapter.orders[order_id] = {"status": order_status, "fills": []}
    return attempt, order_id, condition_id, token_id


def test_matched_zero_trades_defers_instead_of_zero_fill():  # Test A (part 1)
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        attempt, order_id, _cond, _token = _reserve_matched_pending_entry(
            strategy, base, adapter, event_id="race-a"
        )
        result = asyncio.run(ReconciliationWorker(base, adapter, strategy).run_once("test"))
        assert result["status"] == "ok"
        assert result["gaps"] == []
        intent = strategy.intent(attempt["entry_intent_id"])
        assert intent["state"] != "ZERO_FILL"
        assert intent["remote_order_id"] == order_id
    finally:
        temp.cleanup()


def test_matched_zero_trades_then_fill_resolves_no_contradiction():  # Test A (full)
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        attempt, order_id, _cond, token_id = _reserve_matched_pending_entry(
            strategy, base, adapter, event_id="race-a2"
        )
        worker = ReconciliationWorker(base, adapter, strategy)
        asyncio.run(worker.run_once("test"))  # deferred, no ZERO_FILL

        adapter.orders[order_id]["fills"] = [{
            "polymarket_trade_id": "trade-a2", "polymarket_order_id": order_id,
            "price": "0.7", "size": "5.4285", "fee": "0", "status": "matched",
            "matched_at": now_iso(), "transaction_hash": "tx-a2", "raw_message": {},
        }]
        second = asyncio.run(worker.run_once("test"))
        assert second["status"] == "ok"
        assert second["gaps"] == []
        intent = strategy.intent(attempt["entry_intent_id"])
        assert intent["state"] == "FILLED"
        assert intent["filled_shares_text"] == "5.4285"
        position = strategy.position_for_token(token_id)
        assert position is not None
        assert position["position_id"] == intent["position_id"]
        assert position["acquired_shares_text"] == "5.4285"
        assert not strategy.pause_entries()
    finally:
        temp.cleanup()


def test_matched_zero_trades_then_position_only_resolves_no_contradiction():  # Test B
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        attempt, order_id, condition_id, token_id = _reserve_matched_pending_entry(
            strategy, base, adapter, event_id="race-b"
        )
        worker = ReconciliationWorker(base, adapter, strategy)
        asyncio.run(worker.run_once("test"))  # deferred, no ZERO_FILL, no position yet

        # get_trades() never surfaces the fill; get_positions() does.
        adapter.positions.append({
            "token_id": token_id, "condition_id": condition_id,
            "size": "5.4285", "average_price": "0.7", "outcome": "YES",
            "redeemable": False, "current_value": "0",
        })
        second = asyncio.run(worker.run_once("test"))
        assert second["status"] == "ok"
        assert second["gaps"] == []
        intent = strategy.intent(attempt["entry_intent_id"])
        assert intent["state"] == "FILLED"
        position = strategy.position_for_token(token_id)
        assert position is not None
        assert position["position_id"] == intent["position_id"]
        assert position["acquired_shares_text"] == "5.4285"
        assert not strategy.pause_entries()
    finally:
        temp.cleanup()


def test_matched_zero_trades_no_evidence_after_grace_fails_closed():  # Test C
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        attempt, order_id, _cond, _token = _reserve_matched_pending_entry(
            strategy, base, adapter, event_id="race-c"
        )
        worker = ReconciliationWorker(base, adapter, strategy)
        asyncio.run(worker.run_once("test"))  # deferred

        with base.connect() as conn:
            conn.execute(
                "UPDATE live_strategy_intents SET submitted_at=?,created_at=? "
                "WHERE intent_id=?",
                (
                    "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00",
                    attempt["entry_intent_id"],
                ),
            )
            conn.commit()
        expired = asyncio.run(worker.run_once("test"))
        assert expired["status"] == "ok"
        intent = strategy.intent(attempt["entry_intent_id"])
        assert intent["state"] == "ZERO_FILL"
        assert intent["reason_code"] == "REMOTE_MATCHED_ZERO_FILL"
        assert not strategy.pause_entries()
    finally:
        temp.cleanup()


def test_truly_zero_filled_entry_unaffected_by_race_fix():  # Test D
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        attempt, order_id, _cond, _token = _reserve_matched_pending_entry(
            strategy, base, adapter, event_id="race-d", order_status="unmatched",
        )
        result = asyncio.run(ReconciliationWorker(base, adapter, strategy).run_once("test"))
        assert result["status"] == "ok"
        intent = strategy.intent(attempt["entry_intent_id"])
        assert intent["state"] == "ZERO_FILL"
        assert intent["reason_code"] == "REMOTE_UNMATCHED_ZERO_FILL"
        assert not strategy.pause_entries()
    finally:
        temp.cleanup()


def test_unrelated_new_remote_position_still_contradiction():  # Test E
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        strategy.set_pause_entries(False, "operator", "PRECONDITION")
        event_id = "race-e"
        condition_id = f"condition-{event_id}"
        token_id = f"token-{event_id}"
        base.upsert_market({
            "event_id": event_id, "condition_id": condition_id,
            "yes_token_id": token_id, "no_token_id": f"other-{event_id}",
            "token_mapping_status": "verified", "accepting_orders": True,
            "min_order_size": "5",
        })
        # No ENTRY intent was ever reserved for this token -- nothing pending.
        adapter.positions.append({
            "token_id": token_id, "condition_id": condition_id,
            "size": "5.4285", "average_price": "0.7", "outcome": "YES",
            "redeemable": False, "current_value": "0",
        })
        result = asyncio.run(ReconciliationWorker(base, adapter, strategy).run_once("test"))
        assert result["status"] == "gaps"
        assert result["gaps"][0]["type"] == "remote_position_corrected_local"
        assert strategy.pause_record()["pause_cause"] == "RECONCILIATION_CONTRADICTION"
    finally:
        temp.cleanup()


def test_position_without_matched_status_stays_contradiction():  # Test F
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        # The order genuinely came back UNMATCHED (not matched/filled), so it
        # is never added to pending_entry_tokens and is finalized ZERO_FILL
        # immediately. A position later appearing for that same token is not
        # linkable to it and must not auto-resolve.
        attempt, order_id, condition_id, token_id = _reserve_matched_pending_entry(
            strategy, base, adapter, event_id="race-f", order_status="unmatched",
        )
        worker = ReconciliationWorker(base, adapter, strategy)
        first = asyncio.run(worker.run_once("test"))
        assert strategy.intent(attempt["entry_intent_id"])["state"] == "ZERO_FILL"

        adapter.positions.append({
            "token_id": token_id, "condition_id": condition_id,
            "size": "5.4285", "average_price": "0.7", "outcome": "YES",
            "redeemable": False, "current_value": "0",
        })
        second = asyncio.run(worker.run_once("test"))
        assert second["status"] == "gaps"
        assert second["gaps"][0]["type"] == "remote_position_corrected_local"
        assert strategy.pause_record()["pause_cause"] == "RECONCILIATION_CONTRADICTION"
    finally:
        temp.cleanup()


def test_restart_during_propagation_pending_preserves_state_no_duplicate():  # Test G
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        attempt, order_id, condition_id, token_id = _reserve_matched_pending_entry(
            strategy, base, adapter, event_id="race-g"
        )
        asyncio.run(ReconciliationWorker(base, adapter, strategy).run_once("test"))
        before = strategy.intent(attempt["entry_intent_id"])
        assert before["state"] != "ZERO_FILL"

        # Simulate process restart: fresh StrategyRepository over the same DB.
        restarted = StrategyRepository(base)
        restarted.migrate(pause_entries_default=False)
        after_restart = restarted.intent(attempt["entry_intent_id"])
        assert after_restart["state"] == before["state"]
        assert after_restart["remote_order_id"] == order_id

        # A duplicate entry attempt for the same event must still be rejected
        # while this intent remains unresolved.
        duplicate = restarted.reserve_event_entry(
            event_id="race-g", condition_id=condition_id, token_id=token_id,
            side="YES", simultaneous=False, reason_code="ENTRY_PRICE_EXACT",
        )
        assert duplicate.get("_duplicate") is True

        adapter.orders[order_id]["fills"] = [{
            "polymarket_trade_id": "trade-g", "polymarket_order_id": order_id,
            "price": "0.7", "size": "5.4285", "fee": "0", "status": "matched",
            "matched_at": now_iso(), "transaction_hash": "tx-g", "raw_message": {},
        }]
        resumed = asyncio.run(
            ReconciliationWorker(base, adapter, restarted).run_once("test")
        )
        assert resumed["status"] == "ok"
        assert resumed["gaps"] == []
        resolved = restarted.intent(attempt["entry_intent_id"])
        assert resolved["state"] == "FILLED"
        positions = [
            row for row in restarted.active_positions() + [
                restarted.position_for_token(token_id)
            ]
            if row and row.get("token_id") == token_id
        ]
        assert len({row["position_id"] for row in positions}) == 1
    finally:
        temp.cleanup()


def test_bounded_entry_position_correction_is_auto_recoverable():
    temp, base, strategy = build_repo()
    try:
        strategy.set_pause_entries(False, "operator", "PRECONDITION")
        event = "bounded-correction"
        condition = "condition-bounded-correction"
        token = "token-bounded-correction"
        base.upsert_market({
            "event_id": event, "condition_id": condition,
            "yes_token_id": token, "no_token_id": "other-token",
            "token_mapping_status": "verified", "accepting_orders": True,
            "min_order_size": "5",
        })
        reserve_and_open(
            strategy, event=event, shares=Decimal("5.5"),
            minimum=Decimal("5"), sellable=Decimal("0"),
        )
        adapter = MockTradingAdapter()
        adapter.positions = [{
            "condition_id": condition, "token_id": token, "outcome": "YES",
            "size": "5.5042", "average_price": "0.74", "current_value": "4.07",
        }]
        result = asyncio.run(ReconciliationWorker(base, adapter, strategy).run_once("test"))
        assert result["status"] == "gaps"
        assert result["gaps"][0]["type"] == "remote_position_corrected_local"
        assert base.get_state("pause_owner") == "RECONCILIATION"
        assert base.get_state("release_policy") == "AUTO_AFTER_REPAIR_AND_VERIFICATION"
        assert base.get_state("pause_auto_recoverable") == "false"
        clean = asyncio.run(
            ReconciliationWorker(base, adapter, strategy).run_once("test")
        )
        assert clean["status"] == "ok"
        assert base.get_state("pause_auto_recoverable") == "true"
        assert base.get_state("recovery_financial_verified_generation") == base.get_state(
            "pause_generation"
        )
    finally:
        temp.cleanup()


def test_two_zero_fills_then_partial_fill_locks_event_and_opposite_side():
    temp, _base, strategy = build_repo()
    try:
        event_id = "three-attempt-event"
        attempts = []
        for _ in range(2):
            attempt = strategy.reserve_event_entry(
                event_id=event_id, condition_id="three-condition",
                token_id="yes-token", side="YES", simultaneous=False,
                reason_code="ENTRY_PRICE_EXACT",
            )
            attempts.append(attempt["entry_intent_id"])
            strategy.mark_zero_fill(
                event_id, "FAK_ZERO_FILL", intent_id=attempt["entry_intent_id"]
            )
        third = strategy.reserve_event_entry(
            event_id=event_id, condition_id="three-condition",
            token_id="yes-token", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        attempts.append(third["entry_intent_id"])
        strategy.add_fill(
            intent_id=third["entry_intent_id"], remote_trade_id="partial-trade",
            shares=Decimal("2"), price=Decimal("0.74"), fee=Decimal("0"),
            status="MATCHED",
        )
        strategy.open_position(
            event_id=event_id, condition_id="three-condition", token_id="yes-token",
            outcome="YES", shares=Decimal("2"), average_price=Decimal("0.74"),
            cost_all_in=Decimal("1.48"), fees=Decimal("0"),
            min_sellable=Decimal("5"), entry_intent_id=third["entry_intent_id"],
        )
        fourth = strategy.reserve_event_entry(
            event_id=event_id, condition_id="three-condition",
            token_id="no-token", side="NO", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        assert fourth["_duplicate"]
        assert strategy.intent(third["entry_intent_id"])["state"] == "FILLED"
        assert len(set(attempts)) == 3
    finally:
        temp.cleanup()


def test_positive_partial_fill_below_five_tokens_still_locks_event():
    temp, _base, strategy = build_repo()
    try:
        attempt = strategy.reserve_event_entry(
            event_id="small-partial", condition_id="small-condition",
            token_id="small-token", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        intent_id = attempt["entry_intent_id"]
        strategy.add_fill(
            intent_id=intent_id, remote_trade_id="small-trade",
            shares=Decimal("0.01"), price=Decimal("0.74"), fee=Decimal("0"),
            status="MATCHED",
        )
        rejected_zero_fill = False
        try:
            strategy.mark_zero_fill(
                "small-partial", "FAK_ZERO_FILL", intent_id=intent_id
            )
        except RuntimeError:
            rejected_zero_fill = True
        assert rejected_zero_fill
        strategy.open_position(
            event_id="small-partial", condition_id="small-condition",
            token_id="small-token", outcome="YES", shares=Decimal("0.01"),
            average_price=Decimal("0.74"), cost_all_in=Decimal("0.0074"),
            fees=Decimal("0"), min_sellable=Decimal("5"),
            entry_intent_id=intent_id,
        )
        retry = strategy.reserve_event_entry(
            event_id="small-partial", condition_id="small-condition",
            token_id="other-token", side="NO", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        assert retry["_duplicate"]
    finally:
        temp.cleanup()


def test_unresolved_entry_blocks_retry_and_is_not_treated_as_zero_fill():
    temp, _base, strategy = build_repo()
    try:
        first = strategy.reserve_event_entry(
            event_id="unknown-event", condition_id="unknown-condition",
            token_id="unknown-token", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        strategy.update_intent(
            first["entry_intent_id"], state="RECONCILIATION_REQUIRED",
            normalized_error="transport timeout with unknown exchange state",
        )
        retry = strategy.reserve_event_entry(
            event_id="unknown-event", condition_id="unknown-condition",
            token_id="other-token", side="NO", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        assert retry["_duplicate"]
        assert strategy.intent(first["entry_intent_id"])["state"] == "RECONCILIATION_REQUIRED"
    finally:
        temp.cleanup()


def test_real_reconciliation_gap_still_pauses_entries():
    temp, base, strategy = build_repo()
    try:
        strategy.set_pause_entries(False, "operator", "PRECONDITION")
        attempt = strategy.reserve_event_entry(
            event_id="gap-event", condition_id="gap-condition",
            token_id="gap-token", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        strategy.update_intent(
            attempt["entry_intent_id"], state="RECONCILIATION_REQUIRED"
        )
        result = asyncio.run(
            ReconciliationWorker(base, MockTradingAdapter(), strategy).run_once("test")
        )
        assert result["status"] == "gaps"
        assert any(
            gap["type"] == "durable_intent_without_remote_id"
            and gap["intent_id"] == attempt["entry_intent_id"]
            for gap in result["gaps"]
        )
        assert strategy.pause_entries()
        assert base.get_state("reconciliation_readiness") == "NOT_READY"
    finally:
        temp.cleanup()


def test_zero_fill_in_event_a_does_not_block_entry_in_event_b():
    temp, _base, strategy = build_repo()
    try:
        first = strategy.reserve_event_entry(
            event_id="event-a", condition_id="condition-a", token_id="token-a",
            side="YES", simultaneous=False, reason_code="ENTRY_PRICE_EXACT",
            require_empty_slot=True,
        )
        strategy.mark_zero_fill(
            "event-a", "FAK_ZERO_FILL", intent_id=first["entry_intent_id"]
        )
        second = strategy.reserve_event_entry(
            event_id="event-b", condition_id="condition-b", token_id="token-b",
            side="NO", simultaneous=False, reason_code="ENTRY_PRICE_EXACT",
            require_empty_slot=True,
        )
        assert not second.get("_blocked")
        assert not second.get("_duplicate")
        assert second["entry_intent_id"] != first["entry_intent_id"]
    finally:
        temp.cleanup()




def test_tp_waits_for_full_actual_fill_sellability_then_retries_once():
    class RecordingAdapter(MockTradingAdapter):
        def __init__(self):
            super().__init__(scenario="live")
            self.create_calls = []

        async def create_order(self, order):
            self.create_calls.append(order)
            return await super().create_order(order)

    temp, base, strategy = build_repo()
    try:
        shares = Decimal("5.4285")
        position = reserve_and_open(
            strategy,
            event="tp-propagation",
            shares=shares,
            minimum=Decimal("0.0001"),
            sellable=Decimal("0"),
        )
        adapter = RecordingAdapter()
        runtime = LiveStrategyRuntime(
            LiveConfig(execution_mode="READ_ONLY"),
            base,
            strategy,
            adapter,
        )

        asyncio.run(runtime._ensure_take_profit(position))

        waiting_position = strategy.position_for_token("token-tp-propagation")
        waiting = strategy.intent(waiting_position["tp_intent_id"])
        assert waiting["state"] == "WAITING_SELLABLE"
        assert waiting["requested_shares_text"] == "5.4285"
        assert adapter.create_calls == []

        strategy.reconcile_remote_position(
            event_id="tp-propagation",
            condition_id="condition-tp-propagation",
            token_id="token-tp-propagation",
            outcome="YES",
            remote_shares=shares,
            average_price=Decimal("0.74"),
        )
        strategy.set_reconciliation_state(
            ready=True, reason="", actor="test"
        )
        asyncio.run(runtime._refresh_hot_state_once())
        asyncio.run(runtime._manage_position(
            market={},
            update={
                "asset_id": "token-tp-propagation",
                "best_bid": "0.70",
            },
            event_ready=True,
            frame_hash="tp-sellable-returned",
        ))

        assert len(adapter.create_calls) == 1
        order = adapter.create_calls[0]
        assert order["purpose"] == "TAKE_PROFIT"
        assert order["order_type"] == "GTC"
        assert order["requested_price"] == "0.96"
        assert order["requested_size"] == "5.4285"
        current = strategy.position_for_token("token-tp-propagation")
        assert current["tp_intent_id"] != waiting["intent_id"]
        assert strategy.intent(current["tp_intent_id"])["state"] == "LIVE"
    finally:
        temp.cleanup()


def test_unknown_tp_without_remote_id_is_not_cancelled_or_parallel_sold():
    class RecordingAdapter(MockTradingAdapter):
        def __init__(self):
            super().__init__()
            self.cancel_calls = []
            self.create_calls = []

        async def cancel_order(self, order_id):
            self.cancel_calls.append(order_id)
            return await super().cancel_order(order_id)

        async def create_order(self, order):
            self.create_calls.append(order)
            return await super().create_order(order)

    temp, base, strategy = build_repo()
    try:
        position = reserve_and_open(
            strategy, event="unknown-tp", shares=Decimal("5"),
            minimum=Decimal("1"),
        )
        tp = strategy.reserve_position_intent(
            position, action="TP", purpose="TAKE_PROFIT",
            order_type="GTC", shares=Decimal("5"),
            price_limit=Decimal("0.96"), book_hash="tp-unknown",
        )
        strategy.update_intent(
            tp["intent_id"], state="RECONCILIATION_REQUIRED"
        )
        adapter = RecordingAdapter()
        runtime = LiveStrategyRuntime(
            LiveConfig(execution_mode="READ_ONLY"),
            base, strategy, adapter,
        )

        latched = strategy.latch_stop_exit(
            position["position_id"]
        )
        asyncio.run(runtime._market_exit_fak(
            latched,
            {"asset_id": "token-unknown-tp", "best_bid": "0.65"},
            purpose="STOP_066",
            min_price=Decimal("0.01"),
            frame_hash="unknown-tp-stop",
        ))

        assert adapter.cancel_calls == []
        assert adapter.create_calls == []
        assert strategy.intent(tp["intent_id"])["state"] == "RECONCILIATION_REQUIRED"
        assert strategy.position_for_token("token-unknown-tp")[
            "active_exit_intent_id"
        ] is None
    finally:
        temp.cleanup()


# --- EXIT MATCHED/fill propagation race ------------------------------------

def _reserve_matched_pending_exit(
    strategy, base, adapter, *, event_id="exit-race", status="matched",
    shares=Decimal("5.0666"),
):
    base.upsert_market({
        "event_id": event_id,
        "condition_id": f"condition-{event_id}",
        "yes_token_id": f"token-{event_id}",
        "no_token_id": f"other-{event_id}",
        "token_mapping_status": "verified",
        "accepting_orders": True,
        "min_order_size": "5",
    })
    position = reserve_and_open(
        strategy,
        event=event_id,
        shares=shares,
        minimum=Decimal("5"),
        sellable=shares,
    )
    intent = strategy.reserve_position_intent(
        position,
        action="EXIT",
        purpose="STOP_066",
        order_type="FAK",
        shares=shares,
        price_limit=Decimal("0.55"),
        book_hash="exit-race",
    )
    order_id = f"exit-order-{event_id}"
    strategy.update_intent(
        intent["intent_id"],
        remote_order_id=order_id,
        submitted_at=now_iso(),
    )
    adapter.orders[order_id] = {
        "polymarket_order_id": order_id,
        "status": status,
        "fills": [],
    }
    return position, intent, order_id


def _exit_fill(order_id, size):
    return {
        "polymarket_trade_id": f"trade-{order_id}-{size}",
        "polymarket_order_id": order_id,
        "price": "0.60",
        "size": str(size),
        "fee": "0",
        "status": "matched",
        "matched_at": now_iso(),
        "transaction_hash": f"tx-{order_id}",
        "raw_message": {},
    }


def test_exit_matched_zero_fill_then_delayed_fill_is_not_terminalized():
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        position, intent, order_id = _reserve_matched_pending_exit(
            strategy, base, adapter, event_id="exit-delay"
        )
        worker = ReconciliationWorker(base, adapter, strategy)

        first = asyncio.run(worker.run_once("test"))
        pending = strategy.intent(intent["intent_id"])
        assert first["status"] == "ok"
        assert pending["state"] == "RECONCILIATION_REQUIRED"
        assert pending["remote_order_id"] == order_id
        assert pending["reason_code"] == (
            "REMOTE_MATCHED_FILL_PROPAGATION_PENDING"
        )
        assert strategy.position_for_token(position["token_id"])[
            "remaining_shares_text"
        ] == "5.0666"

        adapter.orders[order_id]["fills"] = [
            _exit_fill(order_id, Decimal("5.0666"))
        ]
        second = asyncio.run(worker.run_once("test"))
        assert second["status"] == "ok"
        resolved = strategy.intent(intent["intent_id"])
        assert resolved["state"] == "FILLED"
        assert resolved["filled_shares_text"] == "5.0666"
        assert strategy.position_for_token(position["token_id"])[
            "state"
        ] == "CLOSED"
    finally:
        temp.cleanup()


def test_exit_propagation_pending_prevents_duplicate_exit():
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        position, intent, _order_id = _reserve_matched_pending_exit(
            strategy, base, adapter, event_id="exit-no-duplicate"
        )
        asyncio.run(
            ReconciliationWorker(base, adapter, strategy).run_once("test")
        )
        current = strategy.position_for_token(position["token_id"])
        duplicate = strategy.reserve_position_intent(
            current,
            action="EXIT",
            purpose="STOP_066",
            order_type="FAK",
            shares=Decimal("5.0666"),
            price_limit=Decimal("0.55"),
            book_hash="duplicate",
        )
        assert duplicate["_duplicate"] is True
        assert duplicate["intent_id"] == intent["intent_id"]
    finally:
        temp.cleanup()


def test_exit_propagation_pending_survives_restart():
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        _position, intent, order_id = _reserve_matched_pending_exit(
            strategy, base, adapter, event_id="exit-restart"
        )
        asyncio.run(
            ReconciliationWorker(base, adapter, strategy).run_once("test")
        )

        restarted = StrategyRepository(base)
        restarted.migrate(pause_entries_default=False)
        durable = restarted.intent(intent["intent_id"])
        assert durable["state"] == "RECONCILIATION_REQUIRED"
        assert durable["remote_order_id"] == order_id

        adapter.orders[order_id]["fills"] = [
            _exit_fill(order_id, Decimal("5.0666"))
        ]
        result = asyncio.run(
            ReconciliationWorker(base, adapter, restarted).run_once(
                "restart-test"
            )
        )
        assert result["status"] == "ok"
        assert restarted.intent(intent["intent_id"])["state"] == "FILLED"
    finally:
        temp.cleanup()


def test_exit_matched_partial_fill_uses_observed_quantity_only():
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        position, intent, order_id = _reserve_matched_pending_exit(
            strategy, base, adapter, event_id="exit-partial"
        )
        adapter.orders[order_id]["fills"] = [
            _exit_fill(order_id, Decimal("2"))
        ]
        result = asyncio.run(
            ReconciliationWorker(base, adapter, strategy).run_once("test")
        )
        assert result["status"] == "ok"
        resolved = strategy.intent(intent["intent_id"])
        assert resolved["state"] == "PARTIAL_FINAL"
        assert resolved["filled_shares_text"] == "2"
        current = strategy.position_for_token(position["token_id"])
        assert current["remaining_shares_text"] == "3.0666"
    finally:
        temp.cleanup()


def test_exit_matched_unexplained_beyond_grace_fails_closed():
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        position, intent, order_id = _reserve_matched_pending_exit(
            strategy, base, adapter, event_id="exit-expired"
        )
        with base.connect() as conn:
            conn.execute(
                "UPDATE live_strategy_intents "
                "SET submitted_at='2000-01-01T00:00:00+00:00' "
                "WHERE intent_id=?",
                (intent["intent_id"],),
            )
            conn.commit()

        result = asyncio.run(
            ReconciliationWorker(base, adapter, strategy).run_once("test")
        )
        assert result["status"] == "gaps"
        assert result["gaps"] == [{
            "type": "exit_matched_without_fill_evidence",
            "intent_id": intent["intent_id"],
            "position_id": position["position_id"],
            "token_id": position["token_id"],
            "polymarket_order_id": order_id,
            "status": "matched",
        }]
        assert strategy.intent(intent["intent_id"])[
            "state"
        ] == "RECONCILIATION_REQUIRED"
        assert strategy.position_for_token(position["token_id"])[
            "state"
        ] == "EXIT_RECONCILIATION_REQUIRED"
        assert strategy.pause_entries()
    finally:
        temp.cleanup()


def test_exit_cancelled_and_rejected_remain_terminal():
    for status, expected in (("cancelled", "CANCELED"), ("rejected", "REJECTED")):
        temp, base, strategy = build_repo()
        try:
            adapter = MockTradingAdapter()
            position, intent, _order_id = _reserve_matched_pending_exit(
                strategy,
                base,
                adapter,
                event_id=f"exit-{status}",
                status=status,
            )
            adapter.positions.append({
                "token_id": position["token_id"],
                "condition_id": position["condition_id"],
                "size": position["remaining_shares_text"],
                "average_price": position["average_entry_price_text"],
                "outcome": position["outcome"],
                "redeemable": False,
                "current_value": "3",
            })
            result = asyncio.run(
                ReconciliationWorker(base, adapter, strategy).run_once("test")
            )
            assert result["status"] == "ok"
            assert strategy.intent(intent["intent_id"])["state"] == expected
        finally:
            temp.cleanup()


# --- resolved-market local ghost reconciliation (D6) ----------------------

class _TokenBalanceAdapter(MockTradingAdapter):
    def __init__(self, balances=None):
        super().__init__()
        self.balances = dict(balances or {})
        self.balance_calls = {}

    async def get_token_balance(self, token_id):
        self.balance_calls[token_id] = self.balance_calls.get(token_id, 0) + 1
        value = self.balances.get(token_id)
        if value is None:
            return {"status": "unavailable"}
        return {"status": "mock", "balance_text": str(value)}


def _resolved_position_case(
    *, event_id, winner_is_local, balance, age_position=True,
):
    temp, base, strategy = build_repo()
    token = f"token-{event_id}"
    other = f"other-{event_id}"
    condition = f"condition-{event_id}"
    base.upsert_market({
        "event_id": event_id,
        "condition_id": condition,
        "yes_token_id": token,
        "no_token_id": other,
        "token_mapping_status": "verified",
        "accepting_orders": True,
        "min_order_size": "5",
    })
    position = reserve_and_open(
        strategy,
        event=event_id,
        shares=Decimal("5"),
        minimum=Decimal("5"),
        sellable=Decimal("5"),
    )
    base.mark_market_resolved(
        condition,
        token if winner_is_local else other,
        "YES" if winner_is_local else "NO",
    )
    if age_position:
        with base.connect() as conn:
            conn.execute(
                "UPDATE live_strategy_positions "
                "SET created_at='2000-01-01T00:00:00+00:00' "
                "WHERE position_id=?",
                (position["position_id"],),
            )
            conn.commit()
    adapter = _TokenBalanceAdapter({token: balance})
    return temp, base, strategy, adapter, position


def test_resolved_loser_requires_second_authoritative_zero():
    temp, base, strategy, adapter, position = _resolved_position_case(
        event_id="resolved-loser-zero",
        winner_is_local=False,
        balance="0",
    )
    try:
        worker = ReconciliationWorker(base, adapter, strategy)
        first = asyncio.run(worker.run_once("test"))
        assert first["status"] == "gaps"
        assert strategy.position_for_token(position["token_id"])[
            "state"
        ] == "OPEN"

        worker._rate_limit_retry_after = 0
        second = asyncio.run(worker.run_once("test"))
        assert second["status"] == "ok", second
        assert strategy.position_for_token(position["token_id"])[
            "state"
        ] == "RESOLVED_LOSER"
        assert second["repairs"][0]["type"] == (
            "resolved_loser_authoritative_zero"
        )
        with base.connect() as conn:
            audit = conn.execute(
                "SELECT action,reason FROM live_audit_log "
                "WHERE action='resolved_position_repair' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert tuple(audit) == (
            "resolved_position_repair",
            "RESOLVED_LOSER_AUTHORITATIVE_ZERO",
        )
    finally:
        temp.cleanup()


def test_resolved_winner_positive_balance_becomes_redeem_pending():
    temp, base, strategy, adapter, position = _resolved_position_case(
        event_id="resolved-winner-positive",
        winner_is_local=True,
        balance="5",
    )
    try:
        result = asyncio.run(
            ReconciliationWorker(base, adapter, strategy).run_once("test")
        )
        assert result["status"] == "ok"
        assert strategy.position_for_token(position["token_id"])[
            "state"
        ] == "REDEEM_PENDING"
        assert result["repairs"][0]["type"] == (
            "resolved_winner_marked_redeem_pending"
        )
    finally:
        temp.cleanup()


def test_recent_resolved_position_within_grace_is_not_closed():
    temp, base, strategy, adapter, position = _resolved_position_case(
        event_id="resolved-recent",
        winner_is_local=False,
        balance="0",
        age_position=False,
    )
    try:
        asyncio.run(
            ReconciliationWorker(base, adapter, strategy).run_once("test")
        )
        assert strategy.position_for_token(position["token_id"])[
            "state"
        ] == "OPEN"
    finally:
        temp.cleanup()


def test_resolved_remote_absence_without_balance_never_closes():
    temp, base, strategy, _adapter, position = _resolved_position_case(
        event_id="resolved-absence",
        winner_is_local=False,
        balance=None,
    )
    try:
        adapter = MockTradingAdapter()
        worker = ReconciliationWorker(base, adapter, strategy)
        asyncio.run(worker.run_once("test"))
        worker._rate_limit_retry_after = 0
        asyncio.run(worker.run_once("test"))
        assert strategy.position_for_token(position["token_id"])[
            "state"
        ] == "OPEN"
    finally:
        temp.cleanup()


def test_resolved_loser_positive_authoritative_balance_stays_active():
    temp, base, strategy, adapter, position = _resolved_position_case(
        event_id="resolved-loser-active",
        winner_is_local=False,
        balance="5",
    )
    try:
        result = asyncio.run(
            ReconciliationWorker(base, adapter, strategy).run_once("test")
        )
        assert result["status"] == "gaps"
        assert any(
            gap["type"] == "resolved_loser_authoritative_balance_active"
            for gap in result["gaps"]
        )
        assert strategy.position_for_token(position["token_id"])[
            "state"
        ] == "OPEN"
    finally:
        temp.cleanup()


def test_resolved_winner_authoritative_zero_remains_fail_closed():
    temp, base, strategy, adapter, position = _resolved_position_case(
        event_id="resolved-winner-zero",
        winner_is_local=True,
        balance="0",
    )
    try:
        worker = ReconciliationWorker(base, adapter, strategy)
        asyncio.run(worker.run_once("test"))
        worker._rate_limit_retry_after = 0
        result = asyncio.run(worker.run_once("test"))
        assert result["status"] == "gaps"
        assert any(
            gap["type"] == "resolved_winner_authoritative_zero"
            for gap in result["gaps"]
        )
        assert strategy.position_for_token(position["token_id"])[
            "state"
        ] == "OPEN"
    finally:
        temp.cleanup()

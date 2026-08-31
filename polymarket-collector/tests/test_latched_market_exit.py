import asyncio
from decimal import Decimal
import time
from pathlib import Path
import tempfile

from live.adapters.mock import MockTradingAdapter
from live.config import LiveConfig
from live.repository import LiveRepository, now_iso
from live.strategy_runtime import LiveStrategyRuntime
from live.strategy_repository import StrategyRepository


async def _ok_reconcile(_reason):
    return {"status": "ok"}


def _case(
    name,
    *,
    shares=Decimal("5"),
    sellable=None,
    minimum=Decimal("1"),
    paper=True,
    adapter=None,
    reconciliation=None,
    event_id=None,
    config_overrides=None,
):
    temporary = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temporary.name) / "live.sqlite3")
    base.migrate()
    repo = StrategyRepository(base)
    repo.migrate()
    event_id = event_id or f"exit-{name}"
    condition_id = f"condition-{name}"
    token_id = f"token-{name}"
    base.upsert_market({
        "event_id": event_id,
        "condition_id": condition_id,
        "yes_token_id": token_id,
        "no_token_id": f"no-{name}",
        "token_mapping_status": "verified",
        "accepting_orders": True,
        "min_order_size": str(minimum),
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
        shares=shares,
        average_price=Decimal("0.74"),
        cost_all_in=Decimal("3.70"),
        fees=Decimal("0"),
        sellable_shares=sellable,
        min_sellable=minimum,
    )
    config = LiveConfig(
        live_module_enabled=True,
        execution_mode="PAPER_TRADING" if paper else "READ_ONLY",
        paper_trading_enabled=paper,
        stop_loss_retry_delay_ms=0,
        **(config_overrides or {}),
    )
    runtime = LiveStrategyRuntime(
        config,
        base,
        repo,
        adapter or MockTradingAdapter(),
        reconciliation=reconciliation,
    )
    return temporary, base, repo, runtime, position


def _book(token, bid, bids, generation=1):
    return {
        "asset_id": token,
        "best_bid": bid,
        "generation": generation,
        "bids": [
            {"price": price, "size": size}
            for price, size in bids
        ],
    }


def _manage(runtime, update, label):
    asyncio.run(runtime._manage_position(
        market={},
        update=update,
        event_ready=True,
        frame_hash=label,
    ))


def _exit_intents(base, position_id):
    with base.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM live_strategy_intents
            WHERE position_id=? AND action='EXIT'
            ORDER BY created_at,intent_id
            """,
            (position_id,),
        ).fetchall()
    return [dict(row) for row in rows]


class RecordingSellAdapter(MockTradingAdapter):
    def __init__(self, response=None):
        super().__init__(scenario="delayed")
        self.create_calls = []
        self.cancel_calls = []
        self.response = response

    async def create_order(self, order):
        self.create_calls.append(dict(order))
        if self.response is not None:
            return dict(self.response)
        return {
            "success": True,
            "status": "delayed",
            "polymarket_order_id": f"remote-{len(self.create_calls)}",
        }

    async def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        return await super().cancel_order(order_id)


def test_01_bid_above_066_does_not_latch_exit():
    temp, base, repo, runtime, position = _case("above")
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.67", [("0.67", "5")]),
            "above",
        )
        current = repo.position_for_token(position["token_id"])
        assert current["stop_stage"] == 0
        assert _exit_intents(base, position["position_id"]) == []
    finally:
        temp.cleanup()


def test_02_bid_066_latches_and_market_sells_all():
    temp, base, repo, runtime, position = _case("trigger")
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5")]),
            "trigger",
        )
        current = repo.position_for_token(position["token_id"])
        intents = _exit_intents(base, position["position_id"])
        assert current["state"] == "CLOSED"
        assert current["remaining_shares_text"] == "0"
        assert current["stop_stage"] == 1
        assert len(intents) == 1
        assert intents[0]["purpose"] == "STOP_066"
        assert intents[0]["order_type"] == "FAK"
        assert intents[0]["requested_shares_text"] == "5"
        assert intents[0]["price_limit_text"] == "0.01"
    finally:
        temp.cleanup()


def test_03_latch_survives_price_recovery():
    temp, _base, repo, runtime, position = _case("recovery")
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.67", [("0.67", "5")]),
            "before",
        )
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "2")]),
            "trigger",
        )
        partial = repo.position_for_token(position["token_id"])
        assert partial["state"] == "EXITING"
        assert partial["remaining_shares_text"] == "3"
        asyncio.run(runtime._refresh_hot_state_once())
        _manage(
            runtime,
            _book(position["token_id"], "0.70", [("0.70", "3")]),
            "recovered",
        )
        assert repo.position_for_token(position["token_id"])["state"] == "CLOSED"
    finally:
        temp.cleanup()


def test_04_latched_stop_submits_before_local_sellability_is_confirmed():
    """The settlement window must not silence the STOP.

    Local sellable_shares only turns positive after a reconciliation pass
    reads remote position truth, which trails on-chain settlement of the
    entry by 8-30 seconds. The exchange is the authority on balance, so the
    SELL goes out immediately at the market-equivalent floor.
    """
    adapter = RecordingSellAdapter()
    temp, _base, repo, runtime, position = _case(
        "sellable",
        sellable=Decimal("0"),
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
    )
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5")]),
            "waiting",
        )
        assert len(adapter.create_calls) == 1
        assert adapter.create_calls[0]["requested_size"] == "5"
        assert adapter.create_calls[0]["min_price"] == "0.01"
        assert adapter.create_calls[0]["order_type"] == "FAK"
        assert repo.position_for_token(
            position["token_id"]
        )["stop_stage"] == 1
    finally:
        temp.cleanup()


def test_04b_exchange_balance_rejection_waits_and_costs_no_attempt():
    """An INSUFFICIENT_BALANCE rejection is a wait, not a spent attempt."""
    adapter = RecordingSellAdapter(response={
        "success": False,
        "status": "blocked",
        "submission_state": "NOT_SUBMITTED",
        "failure_reason": "INSUFFICIENT_BALANCE",
        "message": "INSUFFICIENT_BALANCE",
    })
    temp, _base, repo, runtime, position = _case(
        "sellable-rejected",
        sellable=Decimal("0"),
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
    )
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5")]),
            "waiting",
        )
        waiting_position = repo.position_for_token(position["token_id"])
        waiting = repo.intent(waiting_position["active_exit_intent_id"])
        assert waiting["state"] == "WAITING_SELLABLE"
        assert waiting["submitted_at"] is None
        assert repo.stop_attempt_state(
            position["position_id"]
        )["attempt_count"] == 0

        repo.reconcile_remote_position(
            event_id=position["event_id"],
            condition_id=position["condition_id"],
            token_id=position["token_id"],
            outcome="YES",
            remote_shares=Decimal("5"),
            average_price=Decimal("0.74"),
        )
        asyncio.run(runtime._refresh_hot_state_once())
        _manage(
            runtime,
            _book(position["token_id"], "0.40", [("0.40", "5")]),
            "sellable-at-040",
        )
        # Balance propagation must not restore the retired protected ladder.
        assert adapter.create_calls[-1]["min_price"] == "0.01"
    finally:
        temp.cleanup()


def test_04c_optimistic_submit_can_be_disabled():
    adapter = RecordingSellAdapter()
    temp, _base, repo, runtime, position = _case(
        "sellable-conservative",
        sellable=Decimal("0"),
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
        config_overrides={"stop_optimistic_submit_enabled": False},
    )
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5")]),
            "waiting",
        )
        waiting_position = repo.position_for_token(position["token_id"])
        waiting = repo.intent(waiting_position["active_exit_intent_id"])
        assert waiting["state"] == "WAITING_SELLABLE"
        assert adapter.create_calls == []
    finally:
        temp.cleanup()

def test_05_market_exit_sweeps_flash_crash_liquidity():
    temp, base, repo, runtime, position = _case("sweep")
    try:
        _manage(
            runtime,
            _book(
                position["token_id"],
                "0.60",
                [("0.60", "2"), ("0.50", "2"), ("0.40", "2")],
            ),
            "sweep",
        )
        current = repo.position_for_token(position["token_id"])
        intent = _exit_intents(base, position["position_id"])[0]
        assert current["state"] == "CLOSED"
        assert current["remaining_shares_text"] == "0"
        assert intent["state"] == "FILLED"
        assert intent["price_limit_text"] == "0.01"
    finally:
        temp.cleanup()


def test_06_partial_fill_retries_only_remaining():
    temp, base, repo, runtime, position = _case("partial")
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "3")]),
            "partial-1",
        )
        assert repo.position_for_token(position["token_id"])[
            "remaining_shares_text"
        ] == "2"
        asyncio.run(runtime._refresh_hot_state_once())
        _manage(
            runtime,
            _book(position["token_id"], "0.63", [("0.63", "2")]),
            "partial-2",
        )
        intents = _exit_intents(base, position["position_id"])
        assert [item["requested_shares_text"] for item in intents] == ["5", "2"]
        assert repo.position_for_token(position["token_id"])["state"] == "CLOSED"
    finally:
        temp.cleanup()


def test_07_no_bids_never_false_completes_exit():
    temp, _base, repo, runtime, position = _case("no-bids")
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "2")]),
            "some-liquidity",
        )
        asyncio.run(runtime._refresh_hot_state_once())
        _manage(
            runtime,
            _book(position["token_id"], None, []),
            "no-liquidity",
        )
        current = repo.position_for_token(position["token_id"])
        assert current["state"] == "EXITING"
        assert current["remaining_shares_text"] == "3"
        assert current["stop_stage"] == 1
        with runtime.base.connect() as conn:
            documented = conn.execute(
                """
                SELECT COUNT(*) FROM live_audit_timeline
                WHERE reason_code='EXIT_NO_LIQUIDITY_ABOVE_FLOOR'
                  AND result_status='EXIT_RETRY_PENDING'
                """
            ).fetchone()[0]
        assert documented == 1
    finally:
        temp.cleanup()


def test_08_existing_tp_is_canceled_before_market_exit():
    temp, base, repo, runtime, position = _case("tp-cancel")
    try:
        tp = repo.reserve_position_intent(
            position,
            action="TP",
            purpose="TAKE_PROFIT",
            order_type="GTC",
            shares=Decimal("5"),
            price_limit=Decimal("0.96"),
            book_hash="tp",
        )
        repo.update_intent(tp["intent_id"], state="LIVE")
        asyncio.run(runtime._refresh_hot_state_once())
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5")]),
            "stop-with-tp",
        )
        assert repo.intent(tp["intent_id"])["state"] == "CANCELED"
        exit_intent = _exit_intents(base, position["position_id"])[0]
        assert exit_intent["order_type"] == "FAK"
        assert repo.position_for_token(position["token_id"])["state"] == "CLOSED"
    finally:
        temp.cleanup()


def test_09_unknown_tp_cancel_is_fail_closed():
    class CancelUnknownAdapter(RecordingSellAdapter):
        async def cancel_order(self, order_id):
            self.cancel_calls.append(order_id)
            return {"success": False, "status": "unknown"}

    adapter = CancelUnknownAdapter()
    temp, base, repo, runtime, position = _case(
        "tp-unknown",
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
    )
    try:
        tp = repo.reserve_position_intent(
            position,
            action="TP",
            purpose="TAKE_PROFIT",
            order_type="GTC",
            shares=Decimal("5"),
            price_limit=Decimal("0.96"),
            book_hash="tp",
        )
        repo.update_intent(
            tp["intent_id"],
            state="LIVE",
            remote_order_id="remote-tp",
        )
        asyncio.run(runtime._refresh_hot_state_once())
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5")]),
            "unknown-cancel",
        )
        current = repo.position_for_token(position["token_id"])
        assert adapter.cancel_calls == ["remote-tp"]
        assert adapter.create_calls == []
        assert current["state"] == "EXIT_RECONCILIATION_REQUIRED"
        assert current["stop_stage"] == 1
        assert _exit_intents(base, position["position_id"]) == []
    finally:
        temp.cleanup()


def test_10_duplicate_stop_frames_do_not_parallel_sell():
    adapter = RecordingSellAdapter()
    temp, _base, repo, runtime, position = _case(
        "duplicate",
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
    )
    try:
        for number, bid in enumerate(("0.66", "0.65", "0.64"), 1):
            _manage(
                runtime,
                _book(position["token_id"], bid, [(bid, "5")]),
                f"duplicate-{number}",
            )
        assert len(adapter.create_calls) == 1
        assert repo.position_for_token(position["token_id"])[
            "active_exit_intent_id"
        ]
    finally:
        temp.cleanup()


def test_11_delayed_user_ws_fill_retries_reconciled_remaining():
    adapter = RecordingSellAdapter()
    temp, _base, repo, runtime, position = _case(
        "user-ws",
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
    )
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5")]),
            "user-ws-first",
        )
        current = repo.position_for_token(position["token_id"])
        first_id = current["active_exit_intent_id"]
        repo.apply_exit_fill(
            position_id=position["position_id"],
            intent_id=first_id,
            sold_shares=Decimal("3"),
            average_price=Decimal("0.60"),
            fees=Decimal("0"),
            final_state="PARTIAL_FINAL",
            min_sellable=Decimal("1"),
            purpose="STOP_066",
            book_hash=current["last_exit_book_hash"] or "first",
            cumulative_filled_shares=Decimal("3"),
            cumulative_notional=Decimal("1.8"),
            cumulative_fees=Decimal("0"),
        )
        asyncio.run(runtime._refresh_hot_state_once())
        _manage(
            runtime,
            _book(position["token_id"], "0.50", [("0.50", "2")]),
            "user-ws-second",
        )
        assert [call["requested_size"] for call in adapter.create_calls] == [
            "5", "2",
        ]
    finally:
        temp.cleanup()


def test_12_http_timeout_requires_reconcile_before_retry():
    adapter = RecordingSellAdapter({
        "success": False,
        "status": "unknown",
        "failure_reason": "TimeoutError: post status unknown",
    })
    temp, _base, repo, runtime, position = _case(
        "timeout",
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
    )
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5")]),
            "timeout-1",
        )
        asyncio.run(runtime._refresh_hot_state_once())
        _manage(
            runtime,
            _book(position["token_id"], "0.60", [("0.60", "5")]),
            "timeout-2",
        )
        current = repo.position_for_token(position["token_id"])
        intent = repo.intent(current["active_exit_intent_id"])
        assert len(adapter.create_calls) == 1
        assert intent["state"] == "RECONCILIATION_REQUIRED"
        assert current["stop_stage"] == 1
    finally:
        temp.cleanup()


def test_13_zero_position_stops_retrying():
    temp, base, repo, runtime, position = _case("complete")
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5")]),
            "complete-1",
        )
        asyncio.run(runtime._refresh_hot_state_once())
        _manage(
            runtime,
            _book(position["token_id"], "0.50", [("0.50", "5")]),
            "complete-2",
        )
        assert repo.position_for_token(position["token_id"])["state"] == "CLOSED"
        assert len(_exit_intents(base, position["position_id"])) == 1
    finally:
        temp.cleanup()


def test_14_dust_uses_existing_dust_policy_and_stops_retrying():
    temp, base, repo, runtime, position = _case(
        "dust",
        shares=Decimal("5"),
        minimum=Decimal("1"),
    )
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "4.5")]),
            "dust-1",
        )
        current = repo.position_for_token(position["token_id"])
        assert current["state"] == "DUST"
        assert current["remaining_shares_text"] == "0.5"
        assert current["dust_shares_text"] == "0.5"
        asyncio.run(runtime._refresh_hot_state_once())
        _manage(
            runtime,
            _book(position["token_id"], "0.40", [("0.40", "10")]),
            "dust-2",
        )
        assert len(_exit_intents(base, position["position_id"])) == 1
    finally:
        temp.cleanup()


def test_15_critical_stop_latches_while_event_not_ready():
    temp, base, repo, runtime, position = _case("critical-not-ready")
    try:
        update = _book(
            position["token_id"],
            "0.65",
            [("0.65", "5")],
        )
        update["_critical_stop_latched"] = True
        asyncio.run(runtime._manage_position(
            market={},
            update=update,
            event_ready=False,
            frame_hash="critical-not-ready",
        ))

        current = repo.position_for_token(position["token_id"])
        assert current["stop_stage"] == 1
        assert current["state"] == "EXITING"
        assert _exit_intents(base, position["position_id"]) == []
    finally:
        temp.cleanup()


def test_16_sellability_refresh_resumes_without_new_market_frame():
    adapter = RecordingSellAdapter()
    temp, _base, repo, runtime, position = _case(
        "supervisor-sellable",
        sellable=Decimal("0"),
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
    )
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5")]),
            "waiting-frame",
        )
        repo.reconcile_remote_position(
            event_id=position["event_id"],
            condition_id=position["condition_id"],
            token_id=position["token_id"],
            outcome="YES",
            remote_shares=Decimal("5"),
            average_price=Decimal("0.74"),
        )
        asyncio.run(runtime._refresh_hot_state_once())
        runtime.set_exit_book_provider(
            lambda token_id: _book(
                token_id,
                "0.66",
                [("0.66", "5")],
            )
        )

        asyncio.run(runtime._drive_latched_exits_once())

        assert len(adapter.create_calls) == 1
        assert adapter.create_calls[0]["requested_size"] == "5"
        assert adapter.create_calls[0]["min_price"] == "0.01"
    finally:
        temp.cleanup()


def test_17_rest_watchdog_retries_market_floor_when_liquidity_changes():
    adapter = RecordingSellAdapter({
        "success": False,
        "status": "rejected",
        "failure_reason": "FAK_NOT_FILLED",
    })
    books = iter(("0.60", "0.20", "0.05"))

    async def falling_book(token_id):
        bid = next(books, "0.05")
        return {
            "asset_id": token_id,
            "bids": [{"price": bid, "size": "5"}],
            "asks": [],
        }

    adapter.get_order_book = falling_book
    temp, base, repo, runtime, position = _case(
        "rest-market-retry",
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
    )
    try:
        asyncio.run(runtime._drive_latched_exits_once())
        asyncio.run(runtime._refresh_hot_state_once())
        runtime._exit_rest_last_attempt.clear()
        asyncio.run(runtime._drive_latched_exits_once())
        asyncio.run(runtime._refresh_hot_state_once())
        runtime._exit_rest_last_attempt.clear()
        asyncio.run(runtime._drive_latched_exits_once())
        asyncio.run(runtime._refresh_hot_state_once())
        runtime._exit_rest_last_attempt.clear()
        asyncio.run(runtime._drive_latched_exits_once())

        assert repo.position_for_token(position["token_id"])["stop_stage"] == 1
        assert [call["min_price"] for call in adapter.create_calls] == [
            "0.01",
            "0.01",
            "0.01",
        ]
        with base.connect() as conn:
            alert_count = conn.execute(
                """
                SELECT COUNT(*) FROM live_alerts
                WHERE reason_code='STOP_EXIT_PRICE_PROTECTION_EXHAUSTED'
                  AND active=1
                """
            ).fetchone()[0]
        assert alert_count == 0
    finally:
        temp.cleanup()


def _closing_event_id(seconds_from_now):
    """A 5-minute slug whose window closes `seconds_from_now` from now."""
    start = int(time.time()) - 300 + int(seconds_from_now)
    return f"btc-updown-5m-{start}"


def _record_zero_fill_market_attempts(repo, position, count=3):
    """Record prior market-style zero fills against the durable STOP."""
    for index in range(count):
        intent = repo.reserve_position_intent(
            position,
            action="EXIT",
            purpose="STOP_066",
            order_type="FAK",
            shares=Decimal("5"),
            price_limit=Decimal("0.01"),
            book_hash=f"attempt-{index}",
        )
        repo.update_intent(
            str(intent["intent_id"]),
            state="ZERO_FILL",
            submitted_at=now_iso(),
            final_at=now_iso(),
        )
        # reserve_position_intent parks the intent on the position; the real
        # lifecycle clears it on finalisation, so mirror that here.
        with repo.base.connect() as conn:
            conn.execute(
                "UPDATE live_strategy_positions "
                "SET active_exit_intent_id=NULL WHERE position_id=?",
                (str(position["position_id"]),),
            )
            conn.commit()


def test_20_prior_zero_fills_never_exhaust_the_stop_obligation():
    temp, _base, repo, runtime, position = _case(
        "unbounded-market-obligation",
        event_id=_closing_event_id(30),
    )
    try:
        _record_zero_fill_market_attempts(repo, position)
        position = repo.position_for_token(position["token_id"])
        plan = runtime._stop_attempt_plan(position)
        assert plan["ready"] is True
        assert plan["min_price"] == Decimal("0.01")
        assert plan["execution_style"] == "AGGRESSIVE_MARKET_FAK"
        assert "exhausted" not in plan
    finally:
        temp.cleanup()


def test_21_market_floor_is_immediate_long_before_close():
    temp, _base, repo, runtime, position = _case(
        "market-early",
        event_id=_closing_event_id(200),
    )
    try:
        plan = runtime._stop_attempt_plan(position)
        assert plan["ready"] is True
        assert plan["min_price"] == Decimal("0.01")
    finally:
        temp.cleanup()


def test_22_legacy_capitulation_flag_cannot_restore_price_protection():
    temp, _base, repo, runtime, position = _case(
        "legacy-capitulation-off",
        event_id=_closing_event_id(30),
        config_overrides={"stop_capitulation_enabled": False},
    )
    try:
        plan = runtime._stop_attempt_plan(position)
        assert plan["ready"] is True
        assert plan["min_price"] == Decimal("0.01")
    finally:
        temp.cleanup()


def test_23_unparseable_slug_still_uses_market_floor():
    temp, _base, repo, runtime, position = _case(
        "market-unknown-slug",
    )
    try:
        assert runtime._seconds_to_market_close(position["event_id"]) is None
        plan = runtime._stop_attempt_plan(position)
        assert plan["ready"] is True
        assert plan["min_price"] == Decimal("0.01")
    finally:
        temp.cleanup()


def test_24_restart_recovery_keeps_market_style_stop():
    temp, base, repo, runtime, position = _case("restart-market-style")
    try:
        repo.latch_stop_exit(position["position_id"])
        restarted_repo = StrategyRepository(base)
        restarted = LiveStrategyRuntime(
            runtime.config,
            base,
            restarted_repo,
            MockTradingAdapter(),
            reconciliation=_ok_reconcile,
        )
        _manage(
            restarted,
            _book(position["token_id"], "0.05", [("0.05", "5")]),
            "restart-flash-crash",
        )
        current = restarted_repo.position_for_token(position["token_id"])
        intent = _exit_intents(base, position["position_id"])[0]
        assert current["state"] == "CLOSED"
        assert current["stop_stage"] == 1
        assert intent["price_limit_text"] == "0.01"
    finally:
        temp.cleanup()


def test_25_zero_liquidity_stays_managed_then_retries_new_liquidity():
    adapter = RecordingSellAdapter({
        "success": False,
        "status": "rejected",
        "failure_reason": "FAK_NOT_FILLED",
    })
    temp, _base, repo, runtime, position = _case(
        "zero-then-return",
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
    )
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5")]),
            "trigger-zero-fill",
        )
        managed = repo.position_for_token(position["token_id"])
        assert managed["state"] == "EXITING"
        assert managed["stop_stage"] == 1
        asyncio.run(runtime._refresh_hot_state_once())
        _manage(
            runtime,
            _book(position["token_id"], None, []),
            "no-liquidity",
        )
        _manage(
            runtime,
            _book(position["token_id"], "0.05", [("0.05", "5")]),
            "liquidity-returned",
        )
        assert len(adapter.create_calls) == 2
        assert [call["min_price"] for call in adapter.create_calls] == [
            "0.01",
            "0.01",
        ]
        assert repo.position_for_token(position["token_id"])["stop_stage"] == 1
    finally:
        temp.cleanup()

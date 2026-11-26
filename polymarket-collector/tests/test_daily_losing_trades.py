"""Daily cash-flow cap: stop entries once the day is down by the cost of
N losing trades (Friday 2, Monday-Thursday 4; one losing trade = $6.08).

Wins offset losses (15 wins and 2 losses keep trading). Only trades whose
result is known count. Resets at midnight Asia/Jerusalem.
"""

import asyncio
import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from live.adapters.mock import MockTradingAdapter
from live.config import LiveConfig
from live.strategy_runtime import LiveStrategyRuntime
from test_live_full_strategy import build_repo

J = ZoneInfo("Asia/Jerusalem")
SHARES, PRICE, FEE = "8.216215", "0.74", "0.079039"   # cost 6.16 with fee


def _trade(base, event, opened_at_utc, *, won=None, sell_price=None, shares=SHARES):
    """One held (or stopped) trade with its own fills; won=None leaves it open."""
    token, other, cond = f"token-{event}", f"other-{event}", f"condition-{event}"
    position_id = f"position-{event}"
    base.upsert_market({
        "event_id": event, "condition_id": cond, "yes_token_id": token, "no_token_id": other,
        "token_mapping_status": "verified", "accepting_orders": True, "min_order_size": "5",
    })
    with base.connect() as conn:
        conn.execute(
            "INSERT INTO live_strategy_positions(position_id,event_id,condition_id,token_id,outcome,"
            "state,acquired_shares_text,remaining_shares_text,sellable_shares_text,"
            "average_entry_price_text,cost_all_in_text,entry_fees_text,created_at,updated_at)"
            " VALUES(?,?,?,?,'YES','OPEN',?,?,?,?,'6.08',?,?,?)",
            (position_id, event, cond, token, shares, shares, shares, PRICE, FEE,
             opened_at_utc, opened_at_utc),
        )
        fills = [("ENTRY", shares, PRICE, FEE)]
        if sell_price is not None:
            fills.append(("EXIT", shares, sell_price, "0.05"))
        for n, (action, qty, price, fee) in enumerate(fills):
            intent_id = f"intent-{event}-{n}"
            conn.execute(
                "INSERT INTO live_strategy_intents(intent_id,correlation_id,event_id,condition_id,"
                "position_id,action,purpose,token_id,side,state,order_type,requested_amount_text,"
                "requested_shares_text,price_limit_text,max_spend_text,filled_shares_text,"
                "average_price_text,fee_text,remaining_shares_text,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,'YES','FILLED','FAK','6.08',?,?,'8',?,?,?,'0',?,?)",
                (intent_id, intent_id, event, cond, position_id, action,
                 "ENTRY" if action == "ENTRY" else "STOP_066", token, qty, price, qty, price, fee,
                 opened_at_utc, opened_at_utc),
            )
            conn.execute(
                "INSERT INTO live_strategy_fills(fill_id,intent_id,remote_trade_id,shares_text,"
                "price_text,fee_text,fee_verification_status,fee_source,status,matched_at,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,'VERIFIED','test','MATCHED',?,?,?)",
                (f"fill-{event}-{n}", intent_id, f"trade-{event}-{n}", qty, price, fee,
                 opened_at_utc, opened_at_utc, opened_at_utc),
            )
            if sell_price is not None and action == "EXIT":
                conn.execute(
                    "UPDATE live_strategy_positions SET remaining_shares_text='0',state='CLOSED'"
                    " WHERE position_id=?", (position_id,),
                )
        conn.commit()
    if won is not None:
        base.mark_market_resolved(cond, token if won else other, "YES" if won else "NO")


def _runtime():
    temp, base, strategy = build_repo()
    return temp, base, LiveStrategyRuntime(LiveConfig(), base, strategy, MockTradingAdapter())


def test_limits_friday_two_other_days_four():
    assert LiveStrategyRuntime.daily_losing_trade_limit(date(2026, 12, 4)) == 2   # Friday
    for day in (date(2026, 11, 30), date(2026, 12, 1), date(2026, 12, 2), date(2026, 12, 3)):
        assert LiveStrategyRuntime.daily_losing_trade_limit(day) == 4


def test_friday_stops_after_two_losses_without_wins():
    temp, base, runtime = _runtime()
    friday = datetime(2026, 12, 4, 12, 0, tzinfo=J)
    try:
        _trade(base, "fri-1", "2026-12-04T06:00:00+00:00", won=False)
        status = runtime.daily_losing_trades_status(friday)
        assert status["allowed"] and status["losses"] == 1
        _trade(base, "fri-2", "2026-12-04T07:00:00+00:00", won=False)
        status = runtime.daily_losing_trades_status(friday)
        assert not status["allowed"]
        assert status["reason"] == "DAILY_LOSING_TRADES_LIMIT"
        assert status["threshold_usd"] == "-12.16"
        assert Decimal(status["cash_flow_usd"]) == Decimal("-12.32")
    finally:
        temp.cleanup()


def test_wins_offset_losses_fifteen_wins_two_losses_keep_trading():
    temp, base, runtime = _runtime()
    friday = datetime(2026, 12, 4, 23, 30, tzinfo=J)
    try:
        for i in range(15):
            _trade(base, f"win-{i}", f"2026-12-04T{i:02d}:00:00+00:00", won=True)
        _trade(base, "loss-a", "2026-12-04T15:00:00+00:00", won=False)
        _trade(base, "loss-b", "2026-12-04T16:00:00+00:00", won=False)
        status = runtime.daily_losing_trades_status(friday)
        assert (status["wins"], status["losses"]) == (15, 2)
        assert Decimal(status["cash_flow_usd"]) > 0
        assert status["allowed"]
    finally:
        temp.cleanup()


def test_weekday_cap_is_four_losing_trades_of_cash():
    temp, base, runtime = _runtime()
    tuesday = datetime(2026, 12, 1, 23, 30, tzinfo=J)
    try:
        for i in range(3):
            _trade(base, f"tue-l{i}", f"2026-12-01T0{i}:00:00+00:00", won=False)
        _trade(base, "tue-open", "2026-12-01T05:00:00+00:00")  # unresolved: not counted
        assert runtime.daily_losing_trades_status(tuesday)["allowed"]
        _trade(base, "tue-l3", "2026-12-01T21:10:00+00:00", won=False)  # 23:10 local
        status = runtime.daily_losing_trades_status(tuesday)
        assert status["threshold_usd"] == "-24.32" and not status["allowed"]
    finally:
        temp.cleanup()


def test_sold_trades_count_their_sell_proceeds():
    temp, base, runtime = _runtime()
    friday = datetime(2026, 12, 4, 12, 0, tzinfo=J)
    try:
        # Two stopped trades sold at 0.62 lose about $1 each: far from -$12.16.
        _trade(base, "stop-1", "2026-12-04T06:00:00+00:00", sell_price="0.62")
        _trade(base, "stop-2", "2026-12-04T07:00:00+00:00", sell_price="0.62")
        status = runtime.daily_losing_trades_status(friday)
        assert status["losses"] == 2 and status["allowed"]
        assert Decimal("-2.5") < Decimal(status["cash_flow_usd"]) < Decimal("-2")
    finally:
        temp.cleanup()


def test_cap_resets_at_jerusalem_midnight():
    temp, base, runtime = _runtime()
    try:
        _trade(base, "fri-a", "2026-12-03T22:30:00+00:00", won=False)  # Fri 00:30 local
        _trade(base, "fri-b", "2026-12-04T10:00:00+00:00", won=False)
        _trade(base, "thu-late", "2026-12-03T21:50:00+00:00", won=False)  # Thu 23:50 local
        assert not runtime.daily_losing_trades_status(datetime(2026, 12, 4, 23, 30, tzinfo=J))["allowed"]
        thursday = runtime.daily_losing_trades_status(datetime(2026, 12, 3, 23, 55, tzinfo=J))
        assert thursday["losses"] == 1 and thursday["allowed"]
        monday = runtime.daily_losing_trades_status(datetime(2026, 12, 7, 0, 1, tzinfo=J))
        assert monday["losses"] == 0 and monday["allowed"]
    finally:
        temp.cleanup()


def test_exempt_date_has_no_cap():
    temp, base, runtime = _runtime()
    try:
        for i in range(5):
            _trade(base, f"exempt-{i}", f"2026-11-26T0{i}:00:00+00:00", won=False)
        status = runtime.daily_losing_trades_status(datetime(2026, 11, 26, 23, 30, tzinfo=J))
        assert (status["losses"], status["exempt"], status["allowed"]) == (5, True, True)
        _trade(base, "fri-x", "2026-11-27T06:00:00+00:00", won=False)
        _trade(base, "fri-y", "2026-11-27T07:00:00+00:00", won=False)
        friday = runtime.daily_losing_trades_status(datetime(2026, 11, 27, 12, 0, tzinfo=J))
        assert (friday["exempt"], friday["allowed"]) == (False, False)
    finally:
        temp.cleanup()


def test_cap_gates_the_decision_and_the_final_check_and_is_reported():
    process = inspect.getsource(LiveStrategyRuntime._process_event)
    assert "or losing_cap_blocked" in process
    assert "if has_trigger and schedule[\"allowed\"]" in process
    source = inspect.getsource(LiveStrategyRuntime)
    # decision, log, final check and health read RAM, never SQLite
    assert source.count("self._losing_cap_status_ram()") >= 4
    assert "settled_cash_flow_between" not in inspect.getsource(LiveStrategyRuntime._process_event)
    temp, base, runtime = _runtime()
    try:
        assert "daily_losing_trades" in runtime.health()
    finally:
        temp.cleanup()


# --- hot path: RAM only; recomputed when a trade changes state -------------

def _count_db_reads(runtime):
    calls = []
    real = runtime.repo.settled_cash_flow_between

    def counted(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    runtime.repo.settled_cash_flow_between = counted
    return calls


def _today_utc(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_hot_path_read_never_touches_the_database():
    temp, _base, runtime = _runtime()
    try:
        calls = _count_db_reads(runtime)
        for _ in range(50):
            assert runtime._losing_cap_status_ram()["allowed"]
        runtime.health()
        assert calls == []
    finally:
        temp.cleanup()


def test_settled_losses_are_picked_up_by_the_background_refresh():
    temp, base, runtime = _runtime()
    try:
        assert runtime._losing_cap_status_ram()["allowed"]
        calls = _count_db_reads(runtime)
        asyncio.run(runtime._refresh_hot_state_once())
        assert calls == []  # nothing changed: no recompute
        for i in range(5):  # more than any day's limit
            _trade(base, f"bg-{i}", _today_utc(30 - i), won=False)
        asyncio.run(runtime._refresh_hot_state_once())
        assert len(calls) == 1
        cap = runtime._losing_cap_status_ram()
        if not cap.get("exempt"):
            assert cap["allowed"] is False
            assert cap["reason"] == "DAILY_LOSING_TRADES_LIMIT"
    finally:
        temp.cleanup()


def test_backstop_recomputes_without_a_position_change():
    temp, _base, runtime = _runtime()
    try:
        calls = _count_db_reads(runtime)
        runtime._losing_cap_computed_monotonic -= runtime.LOSING_CAP_BACKSTOP_SECONDS + 1
        asyncio.run(runtime._refresh_hot_state_once())
        assert len(calls) == 1
    finally:
        temp.cleanup()


def test_a_previous_day_block_does_not_carry_over():
    temp, _base, runtime = _runtime()
    try:
        runtime._losing_cap = {"allowed": False, "reason": "DAILY_LOSING_TRADES_LIMIT",
                               "day": "2000-01-01"}
        assert runtime._losing_cap_status_ram()["allowed"] is True
    finally:
        temp.cleanup()


def test_unknown_cap_fails_closed():
    temp, _base, runtime = _runtime()
    try:
        runtime._losing_cap = {"allowed": False, "reason": "DAILY_LOSING_TRADES_UNKNOWN", "day": ""}
        assert runtime._losing_cap_status_ram()["allowed"] is False
    finally:
        temp.cleanup()

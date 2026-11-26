"""Daily cap on losing trades: Friday 2, Monday-Thursday 4 (Asia/Jerusalem).

A loss is a trade opened that local day whose market resolved against the
token bought. Once the day's losses reach the cap, entries stop until midnight.
"""

import inspect
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from live.adapters.mock import MockTradingAdapter
from live.config import LiveConfig
from live.strategy_runtime import LiveStrategyRuntime
from test_live_full_strategy import build_repo, reserve_and_open

J = ZoneInfo("Asia/Jerusalem")


def _trade(base, strategy, event, opened_at_utc, *, won=None):
    """One position opened at `opened_at_utc`; won=True/False resolves its market."""
    base.upsert_market({
        "event_id": event, "condition_id": f"condition-{event}",
        "yes_token_id": f"token-{event}", "no_token_id": f"other-{event}",
        "token_mapping_status": "verified", "accepting_orders": True, "min_order_size": "5",
    })
    position = reserve_and_open(strategy, event=event, shares=Decimal("8"), sellable=Decimal("8"))
    with base.connect() as conn:
        conn.execute(
            "UPDATE live_strategy_positions SET created_at=? WHERE position_id=?",
            (opened_at_utc, position["position_id"]),
        )
        conn.commit()
    if won is not None:
        winner = f"token-{event}" if won else f"other-{event}"
        base.mark_market_resolved(f"condition-{event}", winner, "YES" if won else "NO")
    return position


def _runtime():
    temp, base, strategy = build_repo()
    runtime = LiveStrategyRuntime(LiveConfig(), base, strategy, MockTradingAdapter())
    return temp, base, strategy, runtime


def test_limits_friday_two_other_days_four():
    assert LiveStrategyRuntime.daily_losing_trade_limit(date(2026, 12, 4)) == 2   # Friday
    for day in (30, ):
        assert LiveStrategyRuntime.daily_losing_trade_limit(date(2026, 11, day)) == 4  # Monday
    for day in (1, 2, 3):
        assert LiveStrategyRuntime.daily_losing_trade_limit(date(2026, 12, day)) == 4  # Tue-Thu


def test_friday_stops_after_the_second_loss():
    temp, base, strategy, runtime = _runtime()
    friday_noon = datetime(2026, 12, 4, 12, 0, tzinfo=J)
    try:
        _trade(base, strategy, "fri-1", "2026-12-04T06:00:00+00:00", won=False)
        _trade(base, strategy, "fri-2", "2026-12-04T07:00:00+00:00", won=True)
        status = runtime.daily_losing_trades_status(friday_noon)
        assert (status["losses"], status["limit"], status["allowed"]) == (1, 2, True)
        _trade(base, strategy, "fri-3", "2026-12-04T08:00:00+00:00", won=False)
        status = runtime.daily_losing_trades_status(friday_noon)
        assert (status["losses"], status["allowed"]) == (2, False)
        assert status["reason"] == "DAILY_LOSING_TRADES_LIMIT"
    finally:
        temp.cleanup()


def test_weekday_stops_after_the_fourth_loss_and_ignores_open_and_won_trades():
    temp, base, strategy, runtime = _runtime()
    tuesday = datetime(2026, 12, 1, 23, 30, tzinfo=J)
    try:
        for i in range(3):
            _trade(base, strategy, f"tue-l{i}", f"2026-12-01T0{i}:00:00+00:00", won=False)
        _trade(base, strategy, "tue-w", "2026-12-01T05:00:00+00:00", won=True)
        _trade(base, strategy, "tue-open", "2026-12-01T06:00:00+00:00")  # not resolved yet
        assert runtime.daily_losing_trades_status(tuesday)["allowed"]
        _trade(base, strategy, "tue-l3", "2026-12-01T21:10:00+00:00", won=False)  # 23:10 local
        status = runtime.daily_losing_trades_status(tuesday)
        assert (status["losses"], status["limit"], status["allowed"]) == (4, 4, False)
    finally:
        temp.cleanup()


def test_cap_resets_at_jerusalem_midnight():
    temp, base, strategy, runtime = _runtime()
    try:
        # Friday 4/12 local: two losses, one of them at 00:30 local (22:30 UTC Thursday).
        _trade(base, strategy, "fri-a", "2026-12-03T22:30:00+00:00", won=False)
        _trade(base, strategy, "fri-b", "2026-12-04T10:00:00+00:00", won=False)
        # Thursday 23:50 local (21:50 UTC) belongs to Thursday.
        _trade(base, strategy, "thu-late", "2026-12-03T21:50:00+00:00", won=False)
        assert not runtime.daily_losing_trades_status(datetime(2026, 12, 4, 23, 30, tzinfo=J))["allowed"]
        thursday = runtime.daily_losing_trades_status(datetime(2026, 12, 3, 23, 55, tzinfo=J))
        assert (thursday["losses"], thursday["allowed"]) == (1, True)
        monday = runtime.daily_losing_trades_status(datetime(2026, 12, 7, 0, 1, tzinfo=J))
        assert (monday["losses"], monday["allowed"]) == (0, True)
    finally:
        temp.cleanup()


def test_resolved_loser_state_counts_even_without_a_market_winner():
    temp, base, strategy, runtime = _runtime()
    try:
        position = _trade(base, strategy, "state-loser", "2026-12-04T09:00:00+00:00")
        strategy.mark_position_resolved(
            position["position_id"], winner=False, redeem_pending=False, authoritative=True,
        )
        assert runtime.daily_losing_trades_status(
            datetime(2026, 12, 4, 13, 0, tzinfo=J)
        )["losses"] == 1
    finally:
        temp.cleanup()


def test_cap_gates_the_decision_and_the_final_check_and_is_reported():
    process = inspect.getsource(LiveStrategyRuntime._process_event)
    assert "losing_cap_blocked" in process and "or losing_cap_blocked" in process
    assert "if has_trigger and schedule[\"allowed\"]" in process
    source = inspect.getsource(LiveStrategyRuntime)
    assert source.count("self.daily_losing_trades_status()") >= 4  # decision, log, final, health
    temp, base, strategy, runtime = _runtime()
    try:
        assert "daily_losing_trades" in runtime.health()
    finally:
        temp.cleanup()


def test_exempt_date_has_no_cap():
    temp, base, strategy, runtime = _runtime()
    try:
        for i in range(5):  # Thursday 26/11/2026 local
            _trade(base, strategy, f"exempt-{i}", f"2026-11-26T0{i}:00:00+00:00", won=False)
        status = runtime.daily_losing_trades_status(datetime(2026, 11, 26, 23, 30, tzinfo=J))
        assert (status["losses"], status["exempt"], status["allowed"]) == (5, True, True)
        # The next day is capped again.
        _trade(base, strategy, "fri-x", "2026-11-27T06:00:00+00:00", won=False)
        _trade(base, strategy, "fri-y", "2026-11-27T07:00:00+00:00", won=False)
        friday = runtime.daily_losing_trades_status(datetime(2026, 11, 27, 12, 0, tzinfo=J))
        assert (friday["exempt"], friday["allowed"]) == (False, False)
    finally:
        temp.cleanup()

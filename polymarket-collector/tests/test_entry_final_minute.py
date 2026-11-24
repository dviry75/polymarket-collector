"""No new entry in the last 60 seconds of an event; exits keep working."""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live.strategy_runtime as strategy_runtime_module  # noqa: E402
from live.config import LiveConfig  # noqa: E402
from live.strategy import (  # noqa: E402
    StrategyPolicy,
    choose_entry,
    entry_window_reason,
    in_entry_final_minute,
)
from test_btc_reference_integration import (  # noqa: E402
    EVENT, OPEN, W, harness_factory,  # noqa: F401  (fixture)
)
from test_latched_market_exit import (  # noqa: E402
    RecordingSellAdapter, _book, _case, _manage, _ok_reconcile,
)
from test_p0_live_hardening import (  # noqa: E402
    _MATCHED_074, _entry_case, _latched_update, _submit,
)

START = 1_800_000_000
EVENT_ID = f"btc-updown-5m-{START}"


def _at_remaining(seconds: float) -> datetime:
    return datetime.fromtimestamp(START + 300 - seconds, timezone.utc)


def _choose(remaining: float):
    return choose_entry(
        updates=[{"asset_id": "yes", "best_ask": "0.74"}], yes_token_id="yes",
        no_token_id="no", event_ready=True, paused=False, event_locked=False,
        active_exposure=Decimal("0"), observed_at=_at_remaining(remaining),
        event_id=EVENT_ID, policy=StrategyPolicy(),
    )


@pytest.mark.parametrize("remaining, allowed, reason", [
    (121, False, "BEFORE_ENTRY_WINDOW"),
    (120, True, "ENTRY_PRICE_EXACT"),
    (61, True, "ENTRY_PRICE_EXACT"),
    (60.001, True, "ENTRY_PRICE_EXACT"),
    (60, False, "ENTRY_FINAL_MINUTE"),
    (59, False, "ENTRY_FINAL_MINUTE"),
    (0.5, False, "ENTRY_FINAL_MINUTE"),
    (0, False, "EVENT_ENDED"),
])
def test_entry_allowed_only_between_60_and_120_seconds(remaining, allowed, reason):
    decision = _choose(remaining)
    assert (decision.allowed, decision.reason) == (allowed, reason)


def test_cutoff_is_part_of_the_locked_policy():
    assert StrategyPolicy().entry_cutoff_seconds_before_close == 60
    StrategyPolicy(entry_cutoff_seconds_before_close=60).validate()
    with pytest.raises(ValueError):
        StrategyPolicy(entry_cutoff_seconds_before_close=30).validate()
    assert "entry cutoff must remain exactly 60 seconds before close" not in (
        LiveConfig().validation_errors()
    )
    assert "entry cutoff must remain exactly 60 seconds before close" in (
        LiveConfig(strategy_entry_cutoff_seconds_before_close=30).validation_errors()
    )


def test_window_reason_and_final_minute_helper_agree():
    assert entry_window_reason(EVENT_ID, _at_remaining(60), 120, 60) == "ENTRY_FINAL_MINUTE"
    assert entry_window_reason(EVENT_ID, _at_remaining(61), 120, 60) is None
    assert in_entry_final_minute(EVENT_ID, _at_remaining(60), 60)
    assert not in_entry_final_minute(EVENT_ID, _at_remaining(61), 60)
    # Unknown end time fails closed.
    assert in_entry_final_minute("btc-updown-5m-x", _at_remaining(90), 60)


def _entry_case_with_remaining(name, remaining_floor):
    """An entry case whose event has (remaining_floor, remaining_floor + 1] left."""
    start = int(time.time()) - (300 - remaining_floor - 1)
    return _entry_case(
        name, response=_MATCHED_074, exit_bid="0.73",
        event_id=f"btc-updown-5m-{start}",
    )


def test_signal_reaching_submit_in_final_minute_is_not_sent():
    temp, base, repo, runtime, adapter, market, intent_id, tok, event_id = (
        _entry_case_with_remaining("fm-initial", 58)
    )
    try:
        _submit(runtime, market, "YES", intent_id, _latched_update(tok))
        assert adapter.post_calls == 0
        intent = repo.intent(intent_id)
        assert intent["reason_code"] == "ENTRY_REVALIDATION_FINAL_MINUTE"
        assert repo.event_state(event_id)["status"] == "ENTRY_ZERO_FILL"
        assert event_id not in runtime._hot_state.get("locked_event_ids", set())
        assert base.get_state("pause_entries") != "true"
    finally:
        temp.cleanup()


def test_signal_at_61_crossing_cutoff_before_post_is_not_sent(monkeypatch):
    # Remaining is in (61, 62] when the submit starts; the clock then moves
    # 3 seconds during preflight/signing, i.e. to ~59 at the final guard.
    temp, base, repo, runtime, adapter, market, intent_id, tok, event_id = (
        _entry_case_with_remaining("fm-final", 61)
    )
    shift = [timedelta(0)]
    real = strategy_runtime_module.in_entry_final_minute
    monkeypatch.setattr(
        strategy_runtime_module, "in_entry_final_minute",
        lambda event, observed_at, cutoff: real(event, observed_at + shift[0], cutoff),
    )
    try:
        adapter.preflight = lambda: shift.__setitem__(0, timedelta(seconds=3))
        _submit(runtime, market, "YES", intent_id, _latched_update(tok))
        assert adapter.post_calls == 0
        intent = repo.intent(intent_id)
        assert intent["reason_code"] == "ENTRY_FINAL_REVALIDATION_FINAL_MINUTE"
        assert repo.event_state(event_id)["status"] == "ENTRY_ZERO_FILL"
        assert base.get_state("pause_entries") != "true"
    finally:
        temp.cleanup()


def test_signal_at_61_without_crossing_is_sent():
    temp, base, repo, runtime, adapter, market, intent_id, tok, event_id = (
        _entry_case_with_remaining("fm-ok", 61)
    )
    try:
        _submit(runtime, market, "YES", intent_id, _latched_update(tok))
        assert adapter.post_calls == 1
    finally:
        temp.cleanup()


def _final_minute_event():
    return f"btc-updown-5m-{int(time.time()) - 270}"  # ~30 seconds left


def test_stop_fires_in_final_minute():
    adapter = RecordingSellAdapter()
    temp, base, repo, runtime, position = _case(
        "fm-stop", paper=False, adapter=adapter, reconciliation=_ok_reconcile,
        event_id=_final_minute_event(),
    )
    try:
        _manage(runtime, _book(position["token_id"], "0.66", [("0.66", "5")]), "fm")
        assert len(adapter.create_calls) == 1
        assert adapter.create_calls[0]["purpose"] == "STOP_066"
    finally:
        temp.cleanup()


def test_take_profit_is_placed_in_final_minute():
    adapter = RecordingSellAdapter()
    temp, base, repo, runtime, position = _case(
        "fm-tp", paper=False, adapter=adapter, reconciliation=_ok_reconcile,
        sellable=Decimal("5"), event_id=_final_minute_event(),
    )
    try:
        asyncio.run(runtime._ensure_take_profit(position))
        assert [call["purpose"] for call in adapter.create_calls] == ["TAKE_PROFIT"]
    finally:
        temp.cleanup()


def test_shadow_evaluations_continue_in_final_minute(harness_factory, monkeypatch):
    h = harness_factory(mode="observe")
    monkeypatch.setattr(strategy_runtime_module, "remaining_seconds",
                        lambda _event, _at: Decimal("30"))
    h.tick(0, OPEN)
    h.tick(269_000, OPEN + 12)
    h.runtime.schedule_frame({
        "event_type": "price_change", "message_hash": "final-minute",
        "updates": [{"condition_id": "condition", "asset_id": "yes",
                     "outcome": "YES", "best_ask": "0.74"}],
        "event_readiness": {"condition": {"ready": True, "reason": "READY"}},
    })
    h.flush()
    rows = h.rows("SELECT * FROM live_btc_gate_evaluations WHERE source='SHADOW'")
    assert len(rows) == 1
    assert rows[0]["trading_state"] == "FINAL_MINUTE"
    assert rows[0]["remaining_seconds"] == "30"
    assert h.submits == []

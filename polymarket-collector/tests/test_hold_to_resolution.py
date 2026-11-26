"""hold_to_resolution: no take profit, no stop loss, no automatic exit at all.

Every position is held until the market resolves. Only an exit that was
already latched before the switch (and the operator emergency close) still
sells.
"""

import asyncio
from decimal import Decimal

from live.config import LiveConfig
from test_latched_market_exit import (
    RecordingSellAdapter,
    _book,
    _case,
    _exit_intents,
    _manage,
    _ok_reconcile,
)

HOLD = {"hold_to_resolution": True}


def test_from_env_holds_to_resolution_by_default(monkeypatch):
    monkeypatch.delenv("LIVE_HOLD_TO_RESOLUTION", raising=False)
    assert LiveConfig.from_env().hold_to_resolution is True
    assert LiveConfig.from_env().safe_public_dict()["hold_to_resolution"] is True
    monkeypatch.setenv("LIVE_HOLD_TO_RESOLUTION", "0")
    assert LiveConfig.from_env().hold_to_resolution is False
    # Tests and other direct constructions keep the historical exits.
    assert LiveConfig().hold_to_resolution is False


def test_no_stop_loss_when_the_bid_crashes():
    temp, base, repo, runtime, position = _case("hold-crash", config_overrides=HOLD)
    try:
        for label, bid in (("0.66", "0.66"), ("0.60", "0.60"), ("0.20", "0.20")):
            _manage(runtime, _book(position["token_id"], bid, [(bid, "5")]), label)
        current = repo.position_for_token(position["token_id"])
        assert current["stop_stage"] == 0
        assert current["state"] == "OPEN"
        assert current["remaining_shares_text"] == "5"
        assert _exit_intents(base, position["position_id"]) == []
    finally:
        temp.cleanup()


def test_no_take_profit_order_is_placed():
    adapter = RecordingSellAdapter()
    temp, _base, repo, runtime, position = _case(
        "hold-tp", paper=False, adapter=adapter, reconciliation=_ok_reconcile,
        sellable=Decimal("5"), config_overrides=HOLD,
    )
    try:
        asyncio.run(runtime._ensure_take_profit(position))
        assert adapter.create_calls == []
        assert not repo.position_for_token(position["token_id"]).get("tp_intent_id")
    finally:
        temp.cleanup()


def test_take_profit_still_placed_without_the_flag():
    adapter = RecordingSellAdapter()
    temp, _base, _repo, runtime, position = _case(
        "nohold-tp", paper=False, adapter=adapter, reconciliation=_ok_reconcile,
        sellable=Decimal("5"),
    )
    try:
        asyncio.run(runtime._ensure_take_profit(position))
        assert [call["purpose"] for call in adapter.create_calls] == ["TAKE_PROFIT"]
    finally:
        temp.cleanup()


def test_no_invalid_entry_liquidation_or_stop_on_first_evaluation():
    temp, base, repo, runtime, position = _case("hold-invalid", config_overrides=HOLD)
    try:
        cheap_fill = {**position, "average_entry_price_text": "0.60"}
        asyncio.run(runtime._evaluate_new_position_exit_state(cheap_fill))
        current = repo.position_for_token(position["token_id"])
        assert current["stop_stage"] == 0
        assert current["state"] == "OPEN"
        assert _exit_intents(base, position["position_id"]) == []
    finally:
        temp.cleanup()


def test_invalid_entry_still_liquidates_without_the_flag():
    temp, _base, repo, runtime, position = _case("nohold-invalid")
    try:
        cheap_fill = {**position, "average_entry_price_text": "0.60"}
        asyncio.run(runtime._evaluate_new_position_exit_state(cheap_fill))
        assert repo.position_for_token(position["token_id"])["stop_stage"] >= 1
    finally:
        temp.cleanup()


def test_stop_obligation_is_not_published():
    temp, _base, _repo, runtime, position = _case("hold-mailbox", config_overrides=HOLD)
    try:
        published = runtime._enqueue_stop_obligation(
            position,
            _book(position["token_id"], "0.50", [("0.50", "5")]),
            {"ready": True, "reason": "READY"},
            frame_hash="hold-mailbox",
        )
        assert published is False
    finally:
        temp.cleanup()


def test_a_stop_latched_before_the_switch_still_sells():
    temp, base, repo, runtime, position = _case("hold-prelatched", config_overrides=HOLD)
    try:
        repo.latch_stop_exit(position["position_id"])
        _manage(runtime, _book(position["token_id"], "0.50", [("0.50", "5")]), "prelatched")
        intents = _exit_intents(base, position["position_id"])
        assert [intent["purpose"] for intent in intents] == ["STOP_066"]
        assert repo.position_for_token(position["token_id"])["remaining_shares_text"] == "0"
    finally:
        temp.cleanup()


def test_status_reports_the_exit_mode():
    temp, _base, _repo, runtime, _position = _case("hold-status", config_overrides=HOLD)
    try:
        assert runtime._hold_to_resolution() is True
    finally:
        temp.cleanup()

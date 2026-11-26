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


# --- reconciliation of winners already redeemed by Polymarket's redeemer ---

from live.reconciliation import ReconciliationWorker  # noqa: E402
from test_live_full_strategy import _TokenBalanceAdapter, _resolved_position_case  # noqa: E402

WALLET = "0xCE075637152167517e1492FcF5ff2d131686ee38"
TX = "0x" + "ab" * 32


class _VerifiedBalanceAdapter(_TokenBalanceAdapter):
    async def identity_preflight(self):
        return {"status": "VERIFIED", "wallet": WALLET, "signer": "0xsigner"}


def _redeem_activity(position, *, size="5", tx=TX):
    return [{
        "type": "REDEEM", "proxyWallet": WALLET.lower(),
        "conditionId": position["condition_id"], "asset": position["token_id"],
        "size": size, "transactionHash": tx, "usdcSize": size,
    }]


def _winner_zero(event_id):
    temp, base, strategy, _adapter, position = _resolved_position_case(
        event_id=event_id, winner_is_local=True, balance="0",
    )
    return temp, base, strategy, _VerifiedBalanceAdapter({position["token_id"]: "0"}), position


def _run_twice(worker):
    asyncio.run(worker.run_once("test"))
    worker._rate_limit_retry_after = 0
    return asyncio.run(worker.run_once("test"))


def test_redeemed_winner_with_public_proof_closes_as_redeemed():
    temp, base, strategy, adapter, position = _winner_zero("hold-redeemed")
    calls = []

    async def lookup(wallet, condition_id):
        calls.append((wallet, condition_id))
        return _redeem_activity(position)

    try:
        result = _run_twice(ReconciliationWorker(base, adapter, strategy, redemption_activity=lookup))
        assert not any(gap["type"] in {
            "resolved_winner_authoritative_zero", "local_position_missing_remote",
        } for gap in result["gaps"]), result["gaps"]
        assert any(r["type"] == "resolved_winner_public_redemption" for r in result["repairs"])
        current = strategy.position_for_token(position["token_id"])
        assert current["state"] == "REDEEMED"
        assert current["remaining_shares_text"] == "0"
        assert calls and calls[-1] == (WALLET, position["condition_id"])
    finally:
        temp.cleanup()


def test_redeemed_winner_without_proof_stays_fail_closed():
    temp, base, strategy, adapter, position = _winner_zero("hold-noproof")

    async def lookup(_wallet, _condition_id):
        return [{**_redeem_activity(position)[0], "asset": "some-other-token"}]

    try:
        result = _run_twice(ReconciliationWorker(base, adapter, strategy, redemption_activity=lookup))
        assert any(gap["type"] == "resolved_winner_authoritative_zero" for gap in result["gaps"])
        assert strategy.position_for_token(position["token_id"])["state"] == "OPEN"
    finally:
        temp.cleanup()


def test_redemption_lookup_failure_stays_fail_closed():
    temp, base, strategy, adapter, position = _winner_zero("hold-lookupfail")

    async def lookup(_wallet, _condition_id):
        raise RuntimeError("HTTP 503")

    try:
        result = _run_twice(ReconciliationWorker(base, adapter, strategy, redemption_activity=lookup))
        assert any(gap["type"] == "resolved_winner_authoritative_zero" for gap in result["gaps"])
        assert strategy.position_for_token(position["token_id"])["state"] == "OPEN"
    finally:
        temp.cleanup()


def test_partial_redemption_proof_is_rejected():
    temp, base, strategy, adapter, position = _winner_zero("hold-partial")

    async def lookup(_wallet, _condition_id):
        return _redeem_activity(position, size="4")  # 4 of 5 shares

    try:
        result = _run_twice(ReconciliationWorker(base, adapter, strategy, redemption_activity=lookup))
        assert any(gap["type"] == "resolved_winner_authoritative_zero" for gap in result["gaps"])
        assert strategy.position_for_token(position["token_id"])["state"] == "OPEN"
    finally:
        temp.cleanup()


# --- transient reconciliation gaps must not become manual-only pauses -------

from live.adapters.mock import MockTradingAdapter  # noqa: E402
from test_live_full_strategy import build_repo, reserve_and_open  # noqa: E402


def _aged_open_position(event_id, shares):
    temp, base, strategy = build_repo()
    base.upsert_market({
        "event_id": event_id, "condition_id": f"condition-{event_id}",
        "yes_token_id": f"token-{event_id}", "no_token_id": f"other-{event_id}",
        "token_mapping_status": "verified", "accepting_orders": True,
        "min_order_size": "5",
    })
    position = reserve_and_open(
        strategy, event=event_id, shares=Decimal(shares), sellable=Decimal(shares),
    )
    with base.connect() as conn:  # past the 15 s propagation grace
        conn.execute(
            "UPDATE live_strategy_positions SET created_at='2000-01-01T00:00:00+00:00' "
            "WHERE position_id=?", (position["position_id"],),
        )
        conn.commit()
    strategy.set_pause_entries(False, "operator", "PRECONDITION")
    return temp, base, strategy, position


def _remote(position, size, *, redeemable=False):
    return {
        "token_id": position["token_id"], "condition_id": position["condition_id"],
        "size": size, "average_price": "0", "outcome": "YES",
        "redeemable": redeemable, "current_value": "0",
    }


def test_four_decimal_rounding_after_grace_is_auto_recoverable():
    temp, base, strategy, position = _aged_open_position("round-1", "8.216215")
    try:
        adapter = MockTradingAdapter()
        adapter.positions.append(_remote(position, "8.2162"))
        result = asyncio.run(ReconciliationWorker(base, adapter, strategy).run_once("test"))
        assert [gap["type"] for gap in result["gaps"]] == ["remote_position_corrected_local"]
        assert strategy.pause_record()["pause_cause"] == "RECONCILIATION_GAP"
    finally:
        temp.cleanup()


def test_real_share_difference_after_grace_is_still_a_contradiction():
    temp, base, strategy, position = _aged_open_position("round-2", "8.216215")
    try:
        adapter = MockTradingAdapter()
        adapter.positions.append(_remote(position, "8.2"))
        asyncio.run(ReconciliationWorker(base, adapter, strategy).run_once("test"))
        assert strategy.pause_record()["pause_cause"] == "RECONCILIATION_CONTRADICTION"
    finally:
        temp.cleanup()


def test_redeemable_before_recorded_resolution_is_auto_recoverable():
    temp, base, strategy, position = _aged_open_position("redeemable-1", "8.216215")
    try:
        adapter = MockTradingAdapter()
        adapter.positions.append(_remote(position, "8.216215", redeemable=True))
        result = asyncio.run(ReconciliationWorker(base, adapter, strategy).run_once("test"))
        assert [gap["type"] for gap in result["gaps"]] == [
            "redeemable_position_awaiting_authoritative_resolution"
        ]
        assert strategy.pause_record()["pause_cause"] == "RECONCILIATION_GAP"
    finally:
        temp.cleanup()

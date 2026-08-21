import asyncio
from decimal import Decimal

from live.adapters.mock import MockTradingAdapter
from live.reconciliation import ReconciliationWorker
from live.reconciliation_stability import MISSING_POSITION_GRACE_SECONDS
from test_live_full_strategy import build_repo, reserve_and_open


class BalanceAdapter(MockTradingAdapter):
    def __init__(self, balances=None, *, balance_status="ok"):
        super().__init__()
        self.balances = balances or {}
        self.balance_status = balance_status

    async def get_token_balance(self, token_id):
        return {
            "status": self.balance_status,
            "token_id": token_id,
            "balance_text": self.balances.get(token_id),
        }


def _tp(strategy, position, *, shares, remote_id, state="FAILED"):
    intent = strategy.reserve_position_intent(
        position, action="TP", purpose="TAKE_PROFIT", order_type="GTC",
        shares=shares, price_limit=Decimal("0.96"), book_hash="test-tp",
    )
    strategy.update_intent(
        intent["intent_id"], state=state, remote_order_id=remote_id,
        reason_code="REMOTE_MATCHED",
    )
    return strategy.intent(intent["intent_id"])


def _maker_trade(trade_id, order_id, shares, *, token="token-maker", price="0.96"):
    return {
        "polymarket_trade_id": trade_id,
        "polymarket_order_id": f"taker-{trade_id}",
        "condition_id": "condition-maker",
        "token_id": token,
        "side": "buy",
        "price": price,
        "size": "100",
        "fee": "0",
        "status": "confirmed",
        "matched_at": "2026-08-19T20:13:17Z",
        "transaction_hash": f"tx-{trade_id}",
        "raw_message": {"maker_orders": [{
            "order_id": order_id, "token_id": token, "side": "SELL",
            "price": price, "matched_amount": shares,
        }]},
    }


def _setup_position(strategy, base, *, shares=Decimal("5.42857"), token="token-maker"):
    event = "event-maker"
    condition = f"condition-{event}"
    position_token = f"token-{event}"
    base.upsert_market({
        "event_id": event, "condition_id": condition,
        "yes_token_id": position_token, "no_token_id": "other-maker",
        "token_mapping_status": "verified", "accepting_orders": True,
        "min_order_size": "5",
    })
    return reserve_and_open(
        strategy, event=event, shares=shares,
        minimum=Decimal("5"), sellable=shares,
    )


def test_two_maker_tp_fills_become_verified_dust_and_are_idempotent():
    temp, base, strategy = build_repo()
    try:
        position = _setup_position(strategy, base)
        order_id = "maker-tp-order"
        intent = _tp(
            strategy, position, shares=Decimal("5.4285"), remote_id=order_id
        )
        adapter = BalanceAdapter({position["token_id"]: "0.00857"})
        adapter.orders["parent"] = {"status": "filled", "fills": [
            _maker_trade("trade-1", order_id, "4.61", token=position["token_id"]),
            _maker_trade("trade-2", order_id, "0.81", token=position["token_id"]),
        ]}
        strategy.set_pause_entries(False, "operator", "TEST_PRECONDITION")
        strategy.set_reconciliation_state(
            ready=False, reason="RECONCILIATION_GAP", actor="test"
        )
        worker = ReconciliationWorker(base, adapter, strategy)
        result = asyncio.run(worker.run_once("test"))
        assert result["status"] == "ok"
        assert len(result["repairs"]) == 1
        updated = strategy.position_for_token(position["token_id"])
        assert updated["state"] == "DUST"
        assert updated["remaining_shares_text"] == "0.00857"
        assert updated["sellable_shares_text"] == "0"
        assert updated["closed_at"] is not None
        updated_intent = strategy.intent(intent["intent_id"])
        assert updated_intent["state"] == "PARTIAL_FINAL"
        assert updated_intent["filled_shares_text"] == "5.42"
        assert updated_intent["average_price_text"] == "0.96"
        assert base.get_state("reconciliation_readiness") == "READY"
        assert base.get_state("pause_auto_recoverable") == "true"
        repeated = asyncio.run(worker.run_once("test"))
        assert repeated["status"] == "ok"
        assert repeated["repairs"] == []
        assert strategy.fill_summary(intent["intent_id"])["shares"] == Decimal("5.42")
    finally:
        temp.cleanup()


def test_full_maker_tp_fill_and_zero_balance_closes_position():
    temp, base, strategy = build_repo()
    try:
        position = _setup_position(strategy, base, shares=Decimal("6"))
        order_id = "maker-full-order"
        _tp(strategy, position, shares=Decimal("6"), remote_id=order_id)
        adapter = BalanceAdapter({position["token_id"]: "0"})
        adapter.orders["parent"] = {"status": "filled", "fills": [
            _maker_trade("full-trade", order_id, "6", token=position["token_id"]),
        ]}
        result = asyncio.run(ReconciliationWorker(base, adapter, strategy).run_once("test"))
        assert result["status"] == "ok"
        updated = strategy.position_for_token(position["token_id"])
        assert updated["state"] == "CLOSED"
        assert updated["remaining_shares_text"] == "0"
    finally:
        temp.cleanup()


def test_positions_false_negative_with_equal_balance_keeps_position_active():
    temp, base, strategy = build_repo()
    try:
        position = _setup_position(strategy, base, shares=Decimal("6"))
        adapter = BalanceAdapter({position["token_id"]: "6"})
        result = asyncio.run(ReconciliationWorker(base, adapter, strategy).run_once("test"))
        assert result["status"] == "ok"
        assert strategy.position_for_token(position["token_id"])["state"] == "OPEN"
    finally:
        temp.cleanup()


def test_unknown_balance_gets_one_grace_then_fails_closed_with_backoff():
    temp, base, strategy = build_repo()
    try:
        position = _setup_position(strategy, base, shares=Decimal("6"))
        adapter = BalanceAdapter(balance_status="unavailable")
        worker = ReconciliationWorker(base, adapter, strategy)
        suspect = asyncio.run(worker.run_once("test"))
        assert suspect["status"] == "ok"
        worker._missing_position_suspects[position["token_id"]] -= (
            MISSING_POSITION_GRACE_SECONDS + 1
        )
        gap = asyncio.run(worker.run_once("test"))
        assert gap["status"] == "gaps"
        assert gap["gaps"][0]["cross_check_status"] == "unknown"
        assert gap["retry_after_seconds"] > 0
        backed_off = asyncio.run(worker.run_once("test"))
        assert backed_off["status"] == "backoff"
        assert base.get_state("reconciliation_retry_count") == "1"
    finally:
        temp.cleanup()


def test_known_balance_contradiction_fails_closed_without_grace():
    temp, base, strategy = build_repo()
    try:
        position = _setup_position(strategy, base, shares=Decimal("6"))
        adapter = BalanceAdapter({position["token_id"]: "0"})
        result = asyncio.run(ReconciliationWorker(base, adapter, strategy).run_once("test"))
        assert result["status"] == "gaps"
        assert result["gaps"][0]["cross_check_status"] == "contradiction"
        assert result["gaps"][0]["authoritative_balance"] == "0"
        assert base.get_state("pause_auto_recoverable") == "false"
    finally:
        temp.cleanup()

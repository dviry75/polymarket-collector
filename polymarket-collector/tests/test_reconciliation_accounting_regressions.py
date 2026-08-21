import asyncio
from decimal import Decimal

from live.reconciliation import ReconciliationWorker
from test_reconciliation_stability import (
    BalanceAdapter, _maker_trade, _setup_position, _tp,
)
from test_live_full_strategy import build_repo


def test_maker_fill_rebuilds_baseline_after_positions_lag_reduced_local_shares():
    temp, base, strategy = build_repo()
    try:
        position = _setup_position(strategy, base, shares=Decimal("5.42857"))
        order_id = "maker-after-remote-correction"
        _tp(strategy, position, shares=Decimal("5.4285"), remote_id=order_id)
        corrected, changed = strategy.reconcile_remote_position(
            event_id=position["event_id"],
            condition_id=position["condition_id"],
            token_id=position["token_id"],
            outcome=position["outcome"],
            remote_shares=Decimal("4.6185"),
            average_price=Decimal("0.7"),
        )
        assert changed is True
        assert corrected["remaining_shares_text"] == "4.6185"
        assert corrected["exit_value_text"] == "0"

        adapter = BalanceAdapter({position["token_id"]: "0.00857"})
        adapter.orders["parent"] = {"status": "filled", "fills": [
            _maker_trade("corrected-1", order_id, "4.61", token=position["token_id"]),
            _maker_trade("corrected-2", order_id, "0.81", token=position["token_id"]),
        ]}
        result = asyncio.run(
            ReconciliationWorker(base, adapter, strategy).run_once("test")
        )
        assert result["status"] == "ok"
        assert result["repairs"][0]["authoritative_balance"] == "0.00857"
        updated = strategy.position_for_token(position["token_id"])
        assert updated["state"] == "DUST"
        assert updated["remaining_shares_text"] == "0.00857"
        assert updated["exit_value_text"] == "5.2032"
    finally:
        temp.cleanup()


def test_rest_maker_trade_deduplicates_fill_already_persisted_by_user_ws():
    temp, base, strategy = build_repo()
    try:
        position = _setup_position(strategy, base, shares=Decimal("5.588234"))
        order_id = "maker-already-user-ws"
        intent = _tp(strategy, position, shares=Decimal("5.5882"), remote_id=order_id)
        strategy.add_fill(
            intent_id=intent["intent_id"], remote_trade_id="existing-trade",
            shares=Decimal("5.58"), price=Decimal("0.96"), fee=Decimal("0"),
            fee_verification_status="VERIFIED", fee_source="user_ws",
            status="CONFIRMED", transaction_hash="tx-existing",
            matched_at="2026-08-19T01:29:01Z", raw={"source": "user_ws"},
        )
        strategy.apply_exit_fill(
            position_id=position["position_id"], intent_id=intent["intent_id"],
            sold_shares=Decimal("5.58"), average_price=Decimal("0.96"),
            fees=Decimal("0"), final_state="PARTIAL_FINAL",
            min_sellable=Decimal("5"), purpose="TAKE_PROFIT",
            book_hash="user-ws-confirmed",
            cumulative_filled_shares=Decimal("5.58"),
            cumulative_notional=Decimal("5.3568"), cumulative_fees=Decimal("0"),
        )
        adapter = BalanceAdapter({position["token_id"]: "0.008234"})
        adapter.orders["parent"] = {"status": "filled", "fills": [
            _maker_trade(
                "existing-trade", order_id, "5.58", token=position["token_id"]
            ),
        ]}
        result = asyncio.run(
            ReconciliationWorker(base, adapter, strategy).run_once("test")
        )
        assert result["status"] == "ok"
        assert result["repairs"] == []
        assert strategy.fill_summary(intent["intent_id"])["shares"] == Decimal("5.58")
        updated = strategy.position_for_token(position["token_id"])
        assert updated["remaining_shares_text"] == "0.008234"
        assert updated["exit_value_text"] == "5.3568"
    finally:
        temp.cleanup()

import asyncio
from decimal import Decimal
from pathlib import Path
import tempfile

from live.adapters.mock import MockTradingAdapter
from live.order_attempts import OrderAttemptRecorder
from live.reconciliation import (
    POSITION_PROPAGATION_GRACE_SECONDS,
    ReconciliationWorker,
)
from live.repository import LiveRepository, now_iso
from live.strategy_repository import StrategyRepository


class TokenBalanceAdapter(MockTradingAdapter):
    def __init__(self, token_id: str, balance: str):
        super().__init__()
        self.token_id = token_id
        self.balance = balance

    async def get_token_balance(self, token_id):
        assert token_id == self.token_id
        return {"status": "mock", "balance_text": self.balance}


def build_repo():
    temporary = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temporary.name) / "live.sqlite3")
    base.migrate()
    strategy = StrategyRepository(base)
    strategy.migrate(pause_entries_default=False)
    return temporary, base, strategy


def open_position(strategy, event_id: str, shares=Decimal("5.066664")):
    condition_id = f"condition-{event_id}"
    token_id = f"token-{event_id}"
    entry = strategy.reserve_event_entry(
        event_id=event_id,
        condition_id=condition_id,
        token_id=token_id,
        side="YES",
        simultaneous=False,
        reason_code="ENTRY_PRICE_EXACT",
    )
    return strategy.open_position(
        event_id=event_id,
        condition_id=condition_id,
        token_id=token_id,
        outcome="YES",
        shares=shares,
        average_price=Decimal("0.74"),
        cost_all_in=Decimal("3.74933136"),
        fees=Decimal("0"),
        sellable_shares=shares,
        min_sellable=Decimal("5"),
        entry_intent_id=entry["entry_intent_id"],
    )


def test_scoped_quarantine_stays_risk_visible_without_blocking_other_event():
    temporary, _base, strategy = build_repo()
    try:
        position = open_position(strategy, "quarantined-event", Decimal("5"))
        intent = strategy.reserve_position_intent(
            position,
            action="EXIT",
            purpose="STOP_066",
            order_type="FAK",
            shares=Decimal("5"),
            price_limit=Decimal("0.55"),
            book_hash="unsafe-exit",
        )
        strategy.quarantine_position(
            position["position_id"],
            reason_code="UNSAFE_REMOTE_IDENTITY",
            evidence={"intent_id": intent["intent_id"]},
            actor="test",
        )

        assert strategy.position_for_token(position["token_id"])["state"] == "QUARANTINED"
        assert strategy.active_positions()[0]["position_id"] == position["position_id"]
        assert strategy.quarantined_positions()[0]["position_id"] == position["position_id"]
        assert strategy.entry_blocking_positions() == []
        assert strategy.fast_reconciliation_positions() == []
        assert strategy.entry_blocking_intents() == []
        assert strategy.exposure() > 0

        same_event = strategy.reserve_event_entry(
            event_id=position["event_id"],
            condition_id=position["condition_id"],
            token_id=position["token_id"],
            side="YES",
            simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
            require_empty_slot=True,
        )
        assert same_event["reason"] == "SCOPED_QUARANTINE"

        other_event = strategy.reserve_event_entry(
            event_id="independent-event",
            condition_id="condition-independent-event",
            token_id="token-independent-event",
            side="NO",
            simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
            require_empty_slot=True,
        )
        assert not other_event.get("_blocked")
    finally:
        temporary.cleanup()


def build_matched_exit_case(*, valid_proof: bool):
    temporary, base, strategy = build_repo()
    event_id = "historical-matched-exit"
    position = open_position(strategy, event_id)
    base.upsert_market({
        "event_id": event_id,
        "condition_id": position["condition_id"],
        "yes_token_id": position["token_id"],
        "no_token_id": f"other-{event_id}",
        "token_mapping_status": "verified",
        "accepting_orders": True,
        "min_order_size": "5",
    })
    base.mark_market_resolved(
        position["condition_id"], f"other-{event_id}", "NO"
    )
    exit_intent = strategy.reserve_position_intent(
        position,
        action="EXIT",
        purpose="STOP_066",
        order_type="FAK",
        shares=Decimal("5.06"),
        price_limit=Decimal("0.54"),
        book_hash="historical-exit",
    )
    order_id = "0xmatched-order"
    strategy.update_intent(
        exit_intent["intent_id"],
        state="FAILED",
        remote_order_id=order_id,
        reason_code="REMOTE_MATCHED",
        normalized_error="matched without propagated fill",
        submitted_at=now_iso(),
        final_at=now_iso(),
    )
    strategy.require_exit_reconciliation(position["position_id"])
    with base.connect() as conn:
        conn.execute(
            "UPDATE live_strategy_positions SET remaining_shares_text='5.0666',"
            "created_at='2000-01-01T00:00:00+00:00' WHERE position_id=?",
            (position["position_id"],),
        )
        conn.commit()
    recorder = OrderAttemptRecorder(base)
    started = recorder.start("CREATE_ORDER", {
        "idempotency_key": exit_intent["intent_id"],
        "event_id": event_id,
        "condition_id": position["condition_id"],
        "token_id": position["token_id"],
        "position_id": position["position_id"],
        "purpose": "STOP_066",
        "side": "SELL",
        "order_type": "FAK",
        "requested_price": "0.54",
        "requested_size": "5.06",
    })
    recorder.result(
        started,
        result_status="matched",
        success=True,
        remote_order_id=order_id,
        transaction_hash="0xmatched-tx",
        normalized={
            "polymarket_order_id": order_id if valid_proof else "0xwrong-order",
            "status": "matched",
            "making_amount": "5.06",
            "taking_amount": "2.7324",
            "transaction_hashes": ["0xmatched-tx"],
        },
    )
    adapter = TokenBalanceAdapter(position["token_id"], "0.006664")
    return temporary, base, strategy, adapter, position, exit_intent


def confirm(worker, token_id: str):
    first = asyncio.run(worker.run_once("test", force=True))
    evidence_key, count, first_seen = worker._exit_repair_observations[token_id]
    worker._exit_repair_observations[token_id] = (
        evidence_key,
        count,
        first_seen - POSITION_PROPAGATION_GRACE_SECONDS - 1,
    )
    second = asyncio.run(worker.run_once("test", force=True))
    return first, second


def test_authoritative_matched_exit_repair_matches_live_gap_arithmetic():
    temporary, base, strategy, adapter, position, intent = build_matched_exit_case(
        valid_proof=True
    )
    try:
        first, second = confirm(
            ReconciliationWorker(base, adapter, strategy), position["token_id"]
        )
        assert first["status"] == "gaps"
        assert any(
            gap["type"] == "authoritative_exit_repair_confirmation_pending"
            for gap in first["gaps"]
        )
        assert second["status"] == "ok", second
        current = strategy.position_for_token(position["token_id"])
        repaired_intent = strategy.intent(intent["intent_id"])
        assert current["state"] == "DUST"
        assert current["remaining_shares_text"] == "0.006664"
        assert repaired_intent["state"] == "PARTIAL_FINAL"
        assert repaired_intent["filled_shares_text"] == "5.06"
        assert repaired_intent["average_price_text"] == "0.54"
        assert any(
            repair["type"] == "authoritative_matched_exit_repair"
            for repair in second["repairs"]
        )
        with base.connect() as conn:
            audit = conn.execute(
                "SELECT details_json FROM live_audit_log WHERE "
                "action='authoritative_exit_auto_repair' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert audit is not None
        assert strategy.pause_record()["global_entry_halt_required"] == "false"
    finally:
        temporary.cleanup()


def test_authoritative_repair_recovers_existing_fill_and_prior_double_count():
    temporary, base, strategy, adapter, position, intent = build_matched_exit_case(
        valid_proof=True
    )
    try:
        for remote_trade_id, fee_source, status in (
            ("confirmed-trade", "polymarket_fee_rate_bps", "CONFIRMED"),
            (
                "authoritative:0xmatched-order:0xmatched-tx",
                "authoritative_matched_order_attempt",
                "MATCHED",
            ),
        ):
            strategy.add_fill(
                intent_id=intent["intent_id"],
                remote_trade_id=remote_trade_id,
                shares=Decimal("5.06"),
                price=Decimal("0.54"),
                fee=Decimal("0"),
                fee_verification_status="VERIFIED",
                fee_source=fee_source,
                status=status,
                transaction_hash="0xmatched-tx",
                matched_at=now_iso(),
                raw={"fixture": "prior-double-count"},
            )
        with base.connect() as conn:
            conn.execute(
                "UPDATE live_strategy_positions SET state='CLOSED',"
                "remaining_shares_text='0',sellable_shares_text='0',"
                "dust_shares_text='0',exit_value_text='2.73599856',"
                "active_exit_intent_id=NULL,closed_at=? WHERE position_id=?",
                (now_iso(), position["position_id"]),
            )
            conn.execute(
                "UPDATE live_strategy_intents SET state='PARTIAL_FINAL',"
                "filled_shares_text='10.12',average_price_text='0.54',"
                "remaining_shares_text='0' WHERE intent_id=?",
                (intent["intent_id"],),
            )
            conn.commit()
        strategy.quarantine_position(
            position["position_id"],
            reason_code="AUTO_REPAIR_POSTCONDITION_MISMATCH",
            evidence={
                "remote_order_id": "0xmatched-order",
                "authoritative_balance": "0.006664",
            },
            actor="test",
        )

        _first, second = confirm(
            ReconciliationWorker(base, adapter, strategy), position["token_id"]
        )
        assert second["status"] == "ok", second
        current = strategy.position_for_token(position["token_id"])
        repaired_intent = strategy.intent(intent["intent_id"])
        assert current["state"] == "DUST"
        assert current["remaining_shares_text"] == "0.006664"
        assert current["exit_value_text"] == "2.7324"
        assert repaired_intent["filled_shares_text"] == "5.06"
        assert repaired_intent["remaining_shares_text"] == "0.006664"
        assert strategy.quarantine_records() == []
        summary = strategy.fill_summary(intent["intent_id"])
        assert summary["shares"] == Decimal("5.06")
        assert summary["notional"] == Decimal("2.7324")
    finally:
        temporary.cleanup()


def test_unsafe_authoritative_exit_evidence_is_scoped_quarantined():
    temporary, base, strategy, adapter, position, _intent = build_matched_exit_case(
        valid_proof=False
    )
    try:
        _first, second = confirm(
            ReconciliationWorker(base, adapter, strategy), position["token_id"]
        )
        assert second["status"] == "ok", second
        assert strategy.position_for_token(position["token_id"])["state"] == "QUARANTINED"
        assert strategy.quarantine_records()[0]["position_id"] == position["position_id"]
        record = strategy.pause_record()
        assert record["incident_scope"] == "POSITION"
        assert record["global_entry_halt_required"] == "false"
        assert record["operator_action_required"] == "true"
        assert strategy.exposure() > 0
    finally:
        temporary.cleanup()

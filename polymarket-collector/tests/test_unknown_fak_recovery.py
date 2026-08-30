from decimal import Decimal

import pytest

from live.order_attempts import OrderAttemptRecorder
from live.repository import LiveRepository, now_iso
from live.strategy_repository import StrategyRepository


def _unknown_resolved_stop(tmp_path, *, winner_is_local=True):
    base = LiveRepository(tmp_path / "recovery.sqlite3")
    base.migrate(True)
    repo = StrategyRepository(base)
    repo.migrate(pause_entries_default=False)

    token_id = "winner-token"
    condition_id = "0xcondition"
    event_id = "event-1"
    base.upsert_market({
        "event_id": event_id,
        "condition_id": condition_id,
        "yes_token_id": token_id,
        "no_token_id": "loser-token",
        "accepting_orders": False,
        "market_resolved": True,
        "winning_asset_id": token_id if winner_is_local else "loser-token",
        "winning_outcome": "Up" if winner_is_local else "Down",
    })
    event = repo.reserve_event_entry(
        event_id=event_id,
        condition_id=condition_id,
        token_id=token_id,
        side="YES",
        simultaneous=False,
        reason_code="ENTRY_074",
    )
    position = repo.open_position(
        event_id=event_id,
        condition_id=condition_id,
        token_id=token_id,
        outcome="YES",
        shares=Decimal("5.205477"),
        average_price=Decimal("0.73"),
        cost_all_in=Decimal("3.8"),
        fees=Decimal("0"),
        sellable_shares=Decimal("5.2054"),
        entry_intent_id=str(event["entry_intent_id"]),
    )
    intent = repo.reserve_position_intent(
        position,
        action="EXIT",
        purpose="STOP_066",
        order_type="FAK",
        shares=Decimal("5.2054"),
        price_limit=Decimal("0.64"),
        book_hash="stop-frame",
    )
    repo.update_intent(
        str(intent["intent_id"]),
        state="RECONCILIATION_REQUIRED",
        submitted_at=now_iso(),
        normalized_error="TransportError: Request failed",
    )
    recorder = OrderAttemptRecorder(base)
    attempt = recorder.start(
        "CREATE_ORDER",
        {
            "intent_id": intent["intent_id"],
            "position_id": position["position_id"],
            "event_id": event_id,
            "condition_id": condition_id,
            "token_id": token_id,
            "purpose": "STOP_066",
            "side": "SELL",
            "order_type": "FAK",
        },
    )
    recorder.result(
        attempt,
        result_status="UNKNOWN",
        success=False,
        error_code="TransportError: Request failed",
    )
    # These rows are exactly the legacy damage this repair exists to undo: a
    # position marked a redeemable winner while its STOP FAK was still an
    # unresolved remote UNKNOWN. mark_position_resolved now refuses to create
    # that combination, so the fixture writes it directly rather than
    # pretending the guarded path can still produce it.
    ts = now_iso()
    with base.connect() as conn:
        conn.execute(
            "UPDATE live_strategy_positions "
            "SET state='REDEEM_PENDING',resolved_winner=1,updated_at=? "
            "WHERE position_id=?",
            (ts, str(position["position_id"])),
        )
        conn.execute(
            "UPDATE live_event_states SET status='REDEEM_PENDING',"
            "resolved_at=?,updated_at=? WHERE event_id=?",
            (ts, ts, event_id),
        )
        conn.commit()
    return base, repo, str(intent["intent_id"]), str(position["position_id"])


def test_verified_unknown_fak_zero_fill_preserves_redeem_pending(tmp_path):
    base, repo, intent_id, position_id = _unknown_resolved_stop(tmp_path)

    result = repo.resolve_unknown_closed_fak_zero_fill(
        intent_id,
        authoritative_balance=Decimal("5.205477"),
        identity_verified=True,
        matching_open_orders=0,
        matching_sell_trades=0,
    )

    assert result["after"]["intent_state"] == "ZERO_FILL"
    intent = repo.intent(intent_id)
    position = repo.position_for_token("winner-token")
    assert intent["state"] == "ZERO_FILL"
    assert intent["reason_code"] == "OPERATOR_VERIFIED_UNKNOWN_FAK_ZERO_FILL"
    assert intent["final_at"]
    assert position["state"] == "REDEEM_PENDING"
    assert position["active_exit_intent_id"] is None
    with base.connect() as conn:
        audit = conn.execute(
            "SELECT status,reason FROM live_audit_log "
            "WHERE action='resolve_unknown_closed_fak_zero_fill' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert tuple(audit) == (
        "ok",
        "OPERATOR_VERIFIED_UNKNOWN_FAK_ZERO_FILL",
    )


def test_verified_unknown_fak_corrects_false_winner_to_loser(tmp_path):
    base, repo, intent_id, _position_id = _unknown_resolved_stop(
        tmp_path,
        winner_is_local=False,
    )

    result = repo.resolve_unknown_closed_fak_zero_fill(
        intent_id,
        authoritative_balance=Decimal("5.205477"),
        identity_verified=True,
        matching_open_orders=0,
        matching_sell_trades=0,
    )

    assert result["before"]["official_winner"] is False
    assert result["after"]["intent_state"] == "ZERO_FILL"
    position = repo.position_for_token("winner-token")
    assert position["state"] == "RESOLVED_LOSER"
    assert position["resolved_winner"] == 0
    assert position["realized_pnl_text"] == "-3.8"
    assert position["active_exit_intent_id"] is None
    with base.connect() as conn:
        event_state = conn.execute(
            "SELECT status FROM live_event_states WHERE event_id='event-1'"
        ).fetchone()[0]
        deal = conn.execute(
            "SELECT state,realized_pnl_text,closed_at "
            "FROM live_strategy_deals WHERE event_id='event-1'"
        ).fetchone()
    assert event_state == "RESOLVED_LOSER"
    assert deal["state"] == "RESOLVED_LOSER"
    assert deal["realized_pnl_text"] == "-3.8"
    assert deal["closed_at"]


@pytest.mark.parametrize(
    "balance,open_orders,sell_trades",
    [
        (Decimal("5.0"), 0, 0),
        (Decimal("5.205477"), 1, 0),
        (Decimal("5.205477"), 0, 1),
    ],
)
def test_unknown_fak_recovery_refuses_incomplete_remote_proof(
    tmp_path, balance, open_orders, sell_trades
):
    _base, repo, intent_id, position_id = _unknown_resolved_stop(tmp_path)

    with pytest.raises(RuntimeError):
        repo.resolve_unknown_closed_fak_zero_fill(
            intent_id,
            authoritative_balance=balance,
            identity_verified=True,
            matching_open_orders=open_orders,
            matching_sell_trades=sell_trades,
        )

    assert repo.intent(intent_id)["state"] == "RECONCILIATION_REQUIRED"
    position = repo.position_for_token("winner-token")
    assert position["state"] == "REDEEM_PENDING"
    assert position["active_exit_intent_id"] == intent_id

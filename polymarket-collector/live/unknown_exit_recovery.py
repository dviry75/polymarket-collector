from __future__ import annotations

from decimal import Decimal
from typing import Any

from .order_book import canonical_decimal, decimal_value
from .repository import now_iso, row_to_dict
from .strategy_repository import StrategyRepository


def resolve_unknown_open_exit_zero_effect(
    repo: StrategyRepository,
    intent_id: str,
    *,
    authoritative_balance: Decimal,
    identity_verified: bool,
    matching_open_orders: int,
    matching_sell_trades: int,
    confirmations: int,
    actor: str = "automatic_reconciliation",
) -> dict[str, Any]:
    """Finalize an UNKNOWN open-market FAK only after read-after-write proof.

    The proof is deliberately stricter than order absence alone: verified
    identity, two complete observations, unchanged conditional-token balance,
    no matching open SELL, no matching SELL trade, no durable fill and an
    append-only UNKNOWN CREATE_ORDER result are all required in one transaction.
    """
    if not identity_verified:
        raise RuntimeError("remote identity is not verified")
    if confirmations < 2:
        raise RuntimeError("two authoritative confirmations are required")
    if matching_open_orders or matching_sell_trades:
        raise RuntimeError("matching remote exit evidence exists")

    ts = now_iso()
    with repo.base.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT i.*,p.state AS position_state,p.stop_stage,
                   p.remaining_shares_text AS position_remaining_shares_text,
                   p.active_exit_intent_id,p.tp_intent_id
            FROM live_strategy_intents AS i
            JOIN live_strategy_positions AS p ON p.position_id=i.position_id
            WHERE i.intent_id=?
            """,
            (intent_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise KeyError(intent_id)
        if (
            str(row["state"] or "").upper() == "ZERO_FILL"
            and str(row["active_exit_intent_id"] or "") != intent_id
            and str(row["tp_intent_id"] or "") != intent_id
        ):
            conn.rollback()
            return {"status": "already_resolved", "intent_id": intent_id}

        attempt = conn.execute(
            """
            SELECT result_status,success,remote_order_id
            FROM live_order_attempts
            WHERE intent_id=? AND operation='CREATE_ORDER' AND phase='RESULT'
            ORDER BY occurred_at DESC,record_id DESC LIMIT 1
            """,
            (intent_id,),
        ).fetchone()
        fill_count = int(conn.execute(
            "SELECT COUNT(*) FROM live_strategy_fills WHERE intent_id=?",
            (intent_id,),
        ).fetchone()[0] or 0)
        remaining = decimal_value(row["position_remaining_shares_text"]) or Decimal("0")
        failures: list[str] = []
        if str(row["state"] or "").upper() != "RECONCILIATION_REQUIRED":
            failures.append("intent is not RECONCILIATION_REQUIRED")
        if str(row["position_state"] or "").upper() != "EXIT_RECONCILIATION_REQUIRED":
            failures.append("position is not EXIT_RECONCILIATION_REQUIRED")
        if str(row["action"] or "").upper() not in {"EXIT", "TP"}:
            failures.append("intent is not an exit")
        if str(row["order_type"] or "").upper() != "FAK":
            failures.append("intent is not FAK")
        if not row["submitted_at"]:
            failures.append("intent was never submitted")
        if row["remote_order_id"] or (attempt and attempt["remote_order_id"]):
            failures.append("remote order identity exists")
        if (decimal_value(row["filled_shares_text"]) or Decimal("0")) != 0 or fill_count:
            failures.append("durable fill evidence exists")
        if attempt is None or str(attempt["result_status"] or "").upper() != "UNKNOWN":
            failures.append("latest CREATE_ORDER result is not UNKNOWN")
        if attempt is not None and attempt["success"] not in {None, 0}:
            failures.append("UNKNOWN result is marked successful")
        if authoritative_balance != remaining:
            failures.append("authoritative balance changed")
        if intent_id not in {
            str(row["active_exit_intent_id"] or ""),
            str(row["tp_intent_id"] or ""),
        }:
            failures.append("intent is not linked to the position")
        if failures:
            conn.rollback()
            raise RuntimeError("; ".join(failures))

        next_state = "EXITING" if int(row["stop_stage"] or 0) >= 1 else "OPEN"
        conn.execute(
            """
            UPDATE live_strategy_intents
            SET state='ZERO_FILL',reason_code='AUTHORITATIVE_UNKNOWN_ZERO_EFFECT',
                remaining_shares_text=?,final_at=?,updated_at=?
            WHERE intent_id=? AND state='RECONCILIATION_REQUIRED'
            """,
            (canonical_decimal(remaining), ts, ts, intent_id),
        )
        conn.execute(
            """
            UPDATE live_strategy_positions
            SET active_exit_intent_id=CASE WHEN active_exit_intent_id=? THEN NULL ELSE active_exit_intent_id END,
                tp_intent_id=CASE WHEN tp_intent_id=? THEN NULL ELSE tp_intent_id END,
                state=?,updated_at=?
            WHERE position_id=? AND state='EXIT_RECONCILIATION_REQUIRED'
            """,
            (intent_id, intent_id, next_state, ts, str(row["position_id"])),
        )
        conn.execute(
            "UPDATE live_event_states SET status=?,updated_at=? WHERE event_id=?",
            (next_state, ts, str(row["event_id"])),
        )
        after = conn.execute(
            "SELECT state,active_exit_intent_id,tp_intent_id FROM live_strategy_positions WHERE position_id=?",
            (str(row["position_id"]),),
        ).fetchone()
        if after is None or str(after["state"]) != next_state:
            conn.rollback()
            raise RuntimeError("UNKNOWN zero-effect postcondition failed")
        conn.commit()

    evidence = {
        "intent_id": intent_id,
        "position_id": str(row["position_id"]),
        "authoritative_balance": canonical_decimal(authoritative_balance),
        "identity_verified": identity_verified,
        "matching_open_orders": matching_open_orders,
        "matching_sell_trades": matching_sell_trades,
        "confirmations": confirmations,
        "after": row_to_dict(after) or {},
    }
    repo.base.audit(
        actor,
        "resolve_unknown_open_exit_zero_effect",
        "ok",
        "AUTHORITATIVE_UNKNOWN_ZERO_EFFECT",
        evidence,
    )
    repo.timeline(
        severity="WARNING", category="RECONCILIATION",
        component="automatic_unknown_recovery", source=actor,
        event_id=str(row["event_id"]), condition_id=str(row["condition_id"]),
        token_id=str(row["token_id"]), side="SELL",
        position_id=str(row["position_id"]), intent_id=intent_id,
        requested_action="READ_AFTER_WRITE_ZERO_EFFECT",
        reason_code="AUTHORITATIVE_UNKNOWN_ZERO_EFFECT",
        previous_state="RECONCILIATION_REQUIRED", new_state="ZERO_FILL",
        result_status="VERIFIED", filled_shares_text="0",
        remaining_shares_text=canonical_decimal(remaining),
        parameters_json=evidence,
    )
    return {"status": "resolved", **evidence}

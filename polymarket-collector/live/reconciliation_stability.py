from __future__ import annotations

from decimal import Decimal
import time
from typing import Any

from .order_book import canonical_decimal, decimal_value


MISSING_POSITION_GRACE_SECONDS = 15.0


async def authoritative_token_balance(
    adapter: Any, token_id: str
) -> Decimal | None:
    """Read conditional-token truth without treating positions as authoritative."""
    if hasattr(adapter, "get_token_balance"):
        payload = await adapter.get_token_balance(token_id)
    elif hasattr(adapter, "_balance_allowance"):
        payload = await adapter._balance_allowance(
            asset_type="CONDITIONAL", token_id=token_id
        )
    else:
        return None
    if str(payload.get("status") or "").lower() not in {"ok", "mock"}:
        return None
    return decimal_value(
        payload.get("balance_text")
        if payload.get("balance_text") is not None
        else payload.get("balance")
    )


def _maker_children(trade: dict[str, Any]) -> list[dict[str, Any]]:
    raw = trade.get("raw_message") or {}
    children = raw.get("maker_orders") or []
    return [item for item in children if isinstance(item, dict)]


def ingest_maker_exit_fills(
    repo: Any,
    strategy_repo: Any,
    remote_trades: list[dict[str, Any]],
    remote_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Persist and apply fills where our GTC exit is a maker child.

    Account trade records identify the taker order at the top level. The exact
    maker child's matched_amount is the only valid size for our local order.
    """
    affected: dict[str, dict[str, Any]] = {}
    evidence_by_intent: dict[str, list[dict[str, Any]]] = {}
    for trade in remote_trades:
        trade_id = str(trade.get("polymarket_trade_id") or "")
        for child in _maker_children(trade):
            order_id = str(child.get("order_id") or "")
            intent = strategy_repo.intent_by_remote_order(order_id) if order_id else None
            if not intent or str(intent.get("action") or "").upper() not in {"EXIT", "TP"}:
                continue
            shares = decimal_value(child.get("matched_amount")) or Decimal("0")
            price = decimal_value(child.get("price")) or Decimal("0")
            if shares <= 0 or price <= 0:
                continue
            intent_id = str(intent["intent_id"])
            # The durable fill table historically keyed user-WS fills by
            # account trade id. Reuse that canonical id so REST reconciliation
            # cannot duplicate a fill already persisted by user-WS.
            remote_fill_id = (
                trade_id if trade_id
                else f"maker:{order_id}:{shares}:{price}:{trade.get('matched_at')}"
            )
            inserted = strategy_repo.add_fill(
                intent_id=intent_id,
                remote_trade_id=remote_fill_id,
                shares=shares,
                price=price,
                fee=Decimal("0"),
                fee_verification_status="UNKNOWN",
                fee_source="maker_child_fee_not_reported",
                status=str(trade.get("status") or "MATCHED").upper(),
                transaction_hash=trade.get("transaction_hash"),
                matched_at=trade.get("matched_at"),
                raw={
                    "source": "account_trade_maker_child",
                    "trade_id": trade_id,
                    "order_id": order_id,
                    "maker_child": child,
                },
            )
            affected[intent_id] = intent
            evidence_by_intent.setdefault(intent_id, []).append({
                "trade_id": trade_id,
                "order_id": order_id,
                "shares": canonical_decimal(shares),
                "price": canonical_decimal(price),
                "inserted": inserted,
            })

    repairs: list[dict[str, Any]] = []
    for intent_id, original_intent in affected.items():
        intent = strategy_repo.intent(intent_id) or original_intent
        summary = strategy_repo.fill_summary(intent_id)
        prior = decimal_value(intent.get("filled_shares_text")) or Decimal("0")
        if summary["shares"] <= prior:
            continue
        position = strategy_repo.position_for_token(str(intent.get("token_id") or ""))
        if position is None:
            continue
        requested = decimal_value(intent.get("requested_shares_text")) or summary["shares"]
        order_id = str(intent.get("remote_order_id") or "")
        final_state = (
            "PARTIAL" if order_id in remote_by_id
            else "FILLED" if summary["shares"] >= requested
            else "PARTIAL_FINAL"
        )
        market = repo.latest_market(str(intent.get("condition_id") or "")) or {}
        updated = strategy_repo.apply_exit_fill(
            position_id=str(position["position_id"]),
            intent_id=intent_id,
            sold_shares=summary["shares"] - prior,
            average_price=summary["average_price"],
            fees=summary["fees"],
            final_state=final_state,
            min_sellable=decimal_value(market.get("min_order_size")) or Decimal("0.000001"),
            purpose=str(intent.get("purpose") or "RECONCILED_EXIT"),
            book_hash="account-reconciliation-maker-fill",
            cumulative_filled_shares=summary["shares"],
            cumulative_notional=summary["notional"],
            cumulative_fees=summary["fees"],
        )
        repairs.append({
            "type": "maker_exit_fill_applied",
            "intent_id": intent_id,
            "position_id": position["position_id"],
            "token_id": intent.get("token_id"),
            "filled_shares": canonical_decimal(summary["shares"]),
            "remaining_shares": updated.get("remaining_shares_text"),
            "state": updated.get("state"),
            "evidence": evidence_by_intent[intent_id],
        })
    return repairs


async def classify_missing_position(
    adapter: Any,
    local: dict[str, Any],
    suspects: dict[str, float],
) -> dict[str, Any]:
    token_id = str(local.get("token_id") or "")
    local_shares = decimal_value(local.get("remaining_shares_text")) or Decimal("0")
    supports_balance = (
        hasattr(adapter, "get_token_balance")
        or hasattr(adapter, "_balance_allowance")
    )
    balance = await authoritative_token_balance(adapter, token_id)
    if balance is not None:
        suspects.pop(token_id, None)
        return {
            "status": "confirmed_active" if balance == local_shares else "contradiction",
            "balance": balance,
        }
    if not supports_balance:
        return {"status": "unknown", "balance": None}
    now = time.monotonic()
    first_seen = suspects.setdefault(token_id, now)
    if now - first_seen < MISSING_POSITION_GRACE_SECONDS:
        return {"status": "suspect", "balance": None}
    return {"status": "unknown", "balance": None}

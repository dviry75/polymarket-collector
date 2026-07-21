from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .repository import LiveRepository, now_iso
from .risk_manager import RiskManager


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)
    return result if result.is_finite() else Decimal(default)


@dataclass
class DryRunService:
    repo: LiveRepository
    risk: RiskManager

    def preview(self, intent: dict[str, Any], *, actor: str = "operator") -> dict[str, Any]:
        market = self.repo.latest_market(str(intent.get("condition_id") or "")) if intent.get("condition_id") else None
        requested_amount = _dec(intent.get("requested_amount_usd"), "1")
        price = _dec(intent.get("requested_price") or intent.get("reference_price") or "0.5")
        estimated_shares = requested_amount / price if price > 0 else Decimal("0")
        order_type = str(intent.get("order_type") or "FOK").upper()
        purpose = str(intent.get("purpose") or "entry").lower()
        worst_price = price
        if purpose == "stop_loss":
            worst_price = max(Decimal("0"), price - _dec(intent.get("slippage"), "0.02"))
            order_type = "FAK"
        elif purpose in {"entry", "take_profit", "manual_exit"}:
            worst_price = price + _dec(intent.get("slippage"), "0.01") if intent.get("side", "buy") == "buy" else max(Decimal("0"), price - _dec(intent.get("slippage"), "0.01"))
        risk = self.risk.check_order({
            "condition_id": intent.get("condition_id"),
            "token_id": intent.get("token_id"),
            "requested_amount_usd": requested_amount,
            "requested_price": price,
            "order_type": order_type,
        })
        preview = {
            "timestamp": now_iso(),
            "rule_or_intent": intent.get("rule") or intent.get("idempotency_key") or "manual-dry-run",
            "market_slug": intent.get("market_slug"),
            "market_title": intent.get("market_title"),
            "condition_id": intent.get("condition_id"),
            "side": intent.get("side", "buy"),
            "outcome": intent.get("outcome"),
            "token_id": intent.get("token_id"),
            "requested_amount_usd": str(requested_amount),
            "estimated_shares": str(estimated_shares),
            "order_type": order_type,
            "reference_best_bid": market.get("best_bid") if market else None,
            "reference_best_ask": market.get("best_ask") if market else None,
            "worst_acceptable_price": str(worst_price),
            "orderbook_depth_used": market.get("orderbook_depth_json") if market else None,
            "estimated_average_fill": str(price),
            "estimated_fee": "0",
            "estimated_slippage": str(abs(worst_price - price)),
            "minimum_order_size": market.get("min_order_size") if market else None,
            "tick_size": market.get("min_tick_size") if market else None,
            "balance_status": "NOT_CONFIGURED",
            "allowance_status": "NOT_CONFIGURED",
            "market_websocket_health": self.repo.get_state("market_ws_status", "UNKNOWN"),
            "user_websocket_health": self.repo.get_state("user_ws_status", "NOT_CONFIGURED"),
            "reconciliation_age_status": self.repo.get_state("last_reconciliation_at", "never"),
            "exposure_before": self.risk.current_exposure_usd(),
            "exposure_after": str(_dec(self.risk.current_exposure_usd()) + requested_amount),
            "risk_checks": risk.__dict__,
            "final_decision": "ALLOWED" if risk.allowed else "BLOCKED",
            "reason_codes": ["ALLOWED"] if risk.allowed else [risk.reason_code],
        }
        self.repo.store_dry_run(preview, actor)
        return preview

from __future__ import annotations

from typing import Any

from .adapters.base import TradingAdapter
from .repository import LiveRepository, json_dumps, now_iso
from .risk_manager import RiskManager


class OrderManager:
    def __init__(self, repo: LiveRepository, risk: RiskManager, adapter: TradingAdapter):
        self.repo = repo
        self.risk = risk
        self.adapter = adapter

    async def submit_order(self, order: dict[str, Any], *, actor: str = "system") -> dict[str, Any]:
        local = self.repo.create_order(order)
        if local.get("_duplicate"):
            self.repo.audit(actor, "live_order_duplicate", "blocked", "DUPLICATE_IDEMPOTENCY", {"idempotency_key": order["idempotency_key"]})
            return {
                "order": {key: value for key, value in local.items() if key != "_duplicate"},
                "risk": {"allowed": False, "reason_code": "DUPLICATE_IDEMPOTENCY", "message": "Idempotency key already exists"},
            }
        risk = self.risk.check_order({**order, "local_order_id": local["local_order_id"]})
        if not risk.allowed:
            updated = self.repo.update_order(local["local_order_id"], {
                "status": "blocked",
                "failure_reason": risk.reason_code,
                "raw_response": json_dumps({"message": risk.message}),
            })
            if local.get("live_deal_id"):
                self.repo.fail_deal(int(local["live_deal_id"]), risk.reason_code)
            self.repo.audit(actor, "live_order_blocked", "blocked", risk.reason_code, {"local_order_id": local["local_order_id"]})
            return {"order": updated, "risk": risk.__dict__}

        self.repo.update_order(local["local_order_id"], {"status": "validated"})
        response = await self.adapter.create_order({**order, "local_order_id": local["local_order_id"]})
        status = response.get("status") or ("submitted" if response.get("success") else "failed")
        updates = {
            "status": status,
            "polymarket_order_id": response.get("polymarket_order_id"),
            "submitted_at": now_iso(),
            "failure_reason": response.get("failure_reason"),
            "raw_response": json_dumps(response),
        }
        if status == "failed":
            updates["failed_at"] = now_iso()
        updated = self.repo.update_order(local["local_order_id"], updates)
        for fill in response.get("fills") or []:
            self.repo.add_fill(local["local_order_id"], fill)
        updated = self.repo.update_order(local["local_order_id"], {})
        self.repo.audit(actor, "live_order_submitted", status, details={"local_order_id": local["local_order_id"], "adapter": self.adapter.name})
        return {"order": updated, "adapter_response": response, "risk": risk.__dict__}

    async def cancel_order(self, local_order_id: int, *, actor: str = "operator") -> dict[str, Any]:
        order = self.repo.update_order(local_order_id, {"status": "cancel_requested", "cancel_requested_at": now_iso()})
        remote_id = order.get("polymarket_order_id") or str(local_order_id)
        response = await self.adapter.cancel_order_with_context(
            str(remote_id) if remote_id is not None else None,
            {
                "event_id": order.get("event_id"),
                "condition_id": order.get("condition_id"),
                "token_id": order.get("token_id"),
                "intent_id": order.get("intent_id"),
                "position_id": order.get("position_id"),
                "deal_id": order.get("live_deal_id"),
                "purpose": order.get("purpose") or "LEGACY_ORDER_CANCEL",
                "side": order.get("side"),
                "order_type": order.get("order_type"),
            },
        )
        status = "cancelled" if response.get("success") else "failed"
        updated = self.repo.update_order(local_order_id, {
            "status": status,
            "cancelled_at": now_iso() if status == "cancelled" else None,
            "failed_at": now_iso() if status == "failed" else None,
            "failure_reason": response.get("failure_reason"),
            "raw_response": json_dumps(response),
        })
        self.repo.audit(actor, "live_order_cancel", status, details={"local_order_id": local_order_id})
        return {"order": updated, "adapter_response": response}

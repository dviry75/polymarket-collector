from __future__ import annotations

from decimal import Decimal
from typing import Any

from .order_manager import OrderManager
from .repository import LiveRepository


class TradingEngine:
    def __init__(self, repo: LiveRepository, order_manager: OrderManager):
        self.repo = repo
        self.order_manager = order_manager
        self._locks: set[str] = set()

    async def entry_intent(self, intent: dict[str, Any], *, actor: str = "system") -> dict[str, Any]:
        lock_key = f"entry:{intent.get('live_rule_id')}:{intent.get('event_id')}"
        if lock_key in self._locks:
            return {"status": "blocked", "reason": "DUPLICATE_ENTRY_LOCK"}
        self._locks.add(lock_key)
        try:
            deal_id = self.repo.create_deal({
                "live_rule_id": intent.get("live_rule_id"),
                "event_id": intent.get("event_id"),
                "condition_id": intent.get("condition_id"),
                "token_id": intent.get("token_id"),
                "outcome": intent.get("outcome"),
                "side": "buy",
                "requested_amount_usd": intent.get("requested_amount_usd", 1),
                "requested_size": intent.get("requested_size") or self._size_from_amount(intent),
                "trigger_price": intent.get("trigger_price"),
            })
            order = {
                "idempotency_key": intent["idempotency_key"],
                "live_deal_id": deal_id,
                "live_rule_id": intent.get("live_rule_id"),
                "event_id": intent.get("event_id"),
                "condition_id": intent.get("condition_id"),
                "token_id": intent.get("token_id"),
                "outcome": intent.get("outcome"),
                "side": "buy",
                "order_type": intent.get("order_type", "FOK"),
                "purpose": "entry",
                "requested_price": intent.get("requested_price"),
                "requested_amount_usd": intent.get("requested_amount_usd", 1),
                "requested_size": intent.get("requested_size") or self._size_from_amount(intent),
                "mock_scenario": intent.get("mock_scenario"),
            }
            return await self.order_manager.submit_order(order, actor=actor)
        finally:
            self._locks.discard(lock_key)

    async def exit_intent(self, intent: dict[str, Any], *, actor: str = "system") -> dict[str, Any]:
        lock_key = f"exit:{intent.get('live_deal_id')}"
        if lock_key in self._locks:
            return {"status": "blocked", "reason": "DUPLICATE_EXIT_LOCK"}
        self._locks.add(lock_key)
        try:
            order = {
                "idempotency_key": intent["idempotency_key"],
                "live_deal_id": intent.get("live_deal_id"),
                "live_rule_id": intent.get("live_rule_id"),
                "event_id": intent.get("event_id"),
                "condition_id": intent.get("condition_id"),
                "token_id": intent.get("token_id"),
                "outcome": intent.get("outcome"),
                "side": "sell",
                "order_type": intent.get("order_type", "FOK"),
                "purpose": intent.get("purpose", "exit"),
                "requested_price": intent.get("requested_price"),
                "requested_amount_usd": intent.get("requested_amount_usd"),
                "requested_size": intent.get("requested_size", 0),
                "mock_scenario": intent.get("mock_scenario"),
            }
            return await self.order_manager.submit_order(order, actor=actor)
        finally:
            self._locks.discard(lock_key)

    def _size_from_amount(self, intent: dict[str, Any]) -> float:
        amount = Decimal(str(intent.get("requested_amount_usd", 1)))
        price = Decimal(str(intent.get("requested_price") or intent.get("trigger_price") or "0.5"))
        return float(amount / price) if price > 0 else 0.0


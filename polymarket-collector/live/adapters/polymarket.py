from __future__ import annotations

from typing import Any

from .base import TradingAdapter
from ..config import LiveConfig


class RealPolymarketTradingAdapter(TradingAdapter):
    """Future CLOB V2 adapter.

    The read-only/public shape is present, but write operations are intentionally
    blocked in this task. This class must not send orders until a later task
    wires real credentials and passes every safety condition.
    """

    name = "polymarket"

    def __init__(self, config: LiveConfig):
        self.config = config

    async def get_market_info(self, condition_id: str) -> dict[str, Any]:
        return {"condition_id": condition_id, "status": "not_configured", "reason": "Real adapter read requires future credential setup"}

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        return {"asset_id": token_id, "status": "not_configured"}

    async def get_balance(self) -> dict[str, Any]:
        return {"status": "not_configured"}

    async def get_allowances(self) -> dict[str, Any]:
        return {"status": "not_configured"}

    async def get_open_orders(self) -> list[dict[str, Any]]:
        return []

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        return None

    async def get_trades(self) -> list[dict[str, Any]]:
        return []

    async def get_positions(self) -> list[dict[str, Any]]:
        return []

    async def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": False,
            "status": "blocked",
            "failure_reason": "REAL_POLYMARKET_ORDER_SUBMISSION_DISABLED_IN_THIS_BUILD",
        }

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return {"success": False, "status": "blocked", "failure_reason": "REAL_CANCEL_DISABLED_IN_THIS_BUILD"}

    async def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        return {"success": False, "status": "blocked", "failure_reason": "REAL_CANCEL_DISABLED_IN_THIS_BUILD"}

    async def cancel_all_orders(self) -> dict[str, Any]:
        return {"success": False, "status": "blocked", "failure_reason": "REAL_CANCEL_DISABLED_IN_THIS_BUILD"}


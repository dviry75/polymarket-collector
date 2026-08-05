from __future__ import annotations

from typing import Any

from .base import TradingAdapter
from ..repository import now_iso


class MockTradingAdapter(TradingAdapter):
    name = "mock"

    def __init__(self, scenario: str = "filled"):
        self.scenario = scenario
        self.orders: dict[str, dict[str, Any]] = {}
        self.positions: list[dict[str, Any]] = []

    async def get_market_info(self, condition_id: str) -> dict[str, Any]:
        return {"condition_id": condition_id, "mos": 1, "mts": 0.01, "fd": {"r": 0.07, "e": 2, "to": True}}

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        return {"asset_id": token_id, "bids": [{"price": "0.49", "size": "100"}], "asks": [{"price": "0.51", "size": "100"}]}

    async def get_balance(self) -> dict[str, Any]:
        return {"balance_usd": 10.0, "status": "mock"}

    async def get_allowances(self) -> dict[str, Any]:
        return {"allowance_usd": 10.0, "status": "mock"}

    async def get_open_orders(self) -> list[dict[str, Any]]:
        return [order for order in self.orders.values() if order.get("status") in {"live", "delayed", "retrying"}]

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        return self.orders.get(order_id)

    async def get_trades(self) -> list[dict[str, Any]]:
        trades: list[dict[str, Any]] = []
        for order in self.orders.values():
            trades.extend(order.get("fills", []))
        return trades

    async def get_positions(self) -> list[dict[str, Any]]:
        return list(self.positions)

    async def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        order_id = f"mock-{order['idempotency_key']}"
        scenario = order.get("mock_scenario") or self.scenario
        response: dict[str, Any] = {
            "polymarket_order_id": order_id,
            "status": "live",
            "success": True,
            "fills": [],
            "raw": {"adapter": "mock", "scenario": scenario},
        }
        if scenario == "fok_unfilled":
            response.update({"status": "unmatched", "success": False, "failure_reason": "FOK_NOT_FILLED"})
        elif scenario == "live":
            response.update({"status": "live"})
        elif scenario == "delayed":
            response.update({"status": "delayed"})
        elif scenario == "failed":
            response.update({"status": "failed", "success": False, "failure_reason": "MOCK_FAILURE"})
        elif scenario == "partial":
            size = float(order.get("requested_size") or 1)
            fill_size = size / 2
            response.update({
                "status": "partially_filled",
                "fills": [{
                    "polymarket_trade_id": f"{order_id}-fill-1",
                    "price": float(order.get("requested_price") or 0.5),
                    "size": fill_size,
                    "fee": 0.0,
                    "status": "matched",
                    "matched_at": now_iso(),
                    "raw_message": {"order_id": order_id, "partial": True},
                }],
            })
        else:
            size = float(order.get("requested_size") or 1)
            response.update({
                "status": "filled",
                "fills": [{
                    "polymarket_trade_id": f"{order_id}-fill-1",
                    "price": float(order.get("requested_price") or 0.5),
                    "size": size,
                    "fee": 0.0,
                    "status": "matched",
                    "matched_at": now_iso(),
                    "raw_message": {"order_id": order_id},
                }],
            })
        self.orders[order_id] = {**order, **response}
        return response

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        order = self.orders.get(order_id)
        if not order:
            return {"status": "not_found", "success": False}
        order["status"] = "cancelled"
        return {"status": "cancelled", "success": True, "polymarket_order_id": order_id}

    async def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        return {"results": [await self.cancel_order(order_id) for order_id in order_ids]}

    async def cancel_all_orders(self) -> dict[str, Any]:
        ids = list(self.orders)
        return await self.cancel_orders(ids)

    async def cancel_market_orders(
        self, condition_id: str, token_id: str | None = None
    ) -> dict[str, Any]:
        canceled = []
        for order_id, order in self.orders.items():
            if (
                order.get("condition_id") == condition_id
                and (token_id is None or order.get("token_id") == token_id)
                and order.get("status") in {"live", "delayed", "retrying"}
            ):
                order["status"] = "cancelled"
                canceled.append(order_id)
        return {"success": True, "status": "cancelled", "canceled": canceled}

    async def heartbeat(self) -> dict[str, Any]:
        return {"success": True, "status": "ok"}

    async def redeem(
        self, condition_id: str, *, authorized_intent: bool = False
    ) -> dict[str, Any]:
        return {
            "success": bool(authorized_intent),
            "status": "confirmed" if authorized_intent else "blocked",
            "transaction_hash": f"mock-redeem-{condition_id}" if authorized_intent else None,
        }


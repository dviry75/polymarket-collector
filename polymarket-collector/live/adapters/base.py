from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class TradingAdapter(ABC):
    name = "base"

    @abstractmethod
    async def get_market_info(self, condition_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def get_balance(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def get_allowances(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def get_open_orders(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    @abstractmethod
    async def get_trades(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def get_trades_page(
        self,
        *,
        after: str | None = None,
        before: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read one page of account trades, optionally time-filtered.

        ``after``/``before`` are unix-second strings, matching the CLOB
        ``/data/trades`` contract. An adapter without a paginated remote answers
        the whole set in one page; callers deduplicate by Trade ID, so an
        adapter that ignores the filters stays correct -- just not cheaper.
        """
        return {
            "trades": await self.get_trades(),
            "has_more": False,
            "next_cursor": None,
        }

    @abstractmethod
    async def get_positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        raise NotImplementedError

    async def cancel_order_with_context(
        self, order_id: str | None, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.cancel_order(str(order_id or ""))

    @abstractmethod
    async def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def cancel_all_orders(self) -> dict[str, Any]:
        raise NotImplementedError

    async def cancel_market_orders(
        self, condition_id: str, token_id: str | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def heartbeat(self) -> dict[str, Any]:
        return {"success": False, "status": "disabled"}

    async def redeem(
        self, condition_id: str, *, authorized_intent: bool = False
    ) -> dict[str, Any]:
        return {"success": False, "status": "blocked"}


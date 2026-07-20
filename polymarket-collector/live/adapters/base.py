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

    @abstractmethod
    async def get_positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def cancel_all_orders(self) -> dict[str, Any]:
        raise NotImplementedError


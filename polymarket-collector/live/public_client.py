from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import httpx

from .repository import now_iso


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _best_bid(book: dict[str, Any]) -> Optional[float]:
    prices = [_decimal(item.get("price")) for item in book.get("bids") or []]
    values = [price for price in prices if price is not None]
    return float(max(values)) if values else None


def _best_ask(book: dict[str, Any]) -> Optional[float]:
    prices = [_decimal(item.get("price")) for item in book.get("asks") or []]
    values = [price for price in prices if price is not None]
    return float(min(values)) if values else None


class PublicClobClient:
    """Read-only CLOB V2 public client.

    It uses public endpoints only and never needs account credentials.
    """

    def __init__(self, host: str = "https://clob.polymarket.com", timeout: float = 10):
        self.host = host.rstrip("/")
        self.timeout = timeout

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.host}/book", params={"token_id": token_id})
            response.raise_for_status()
            return response.json()

    async def get_market_info(self, condition_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.host}/markets/{condition_id}")
            if response.status_code == 404:
                response = await client.get(f"{self.host}/market", params={"condition_id": condition_id})
            response.raise_for_status()
            return response.json()

    async def build_metadata(
        self,
        *,
        condition_id: str,
        event_id: str | None = None,
        gamma_yes_token_id: str | None = None,
        gamma_no_token_id: str | None = None,
    ) -> dict[str, Any]:
        info = await self.get_market_info(condition_id)
        tokens = info.get("t") or info.get("tokens") or []
        token_by_outcome = {str(item.get("o") or item.get("outcome") or "").lower(): str(item.get("t") or item.get("token_id") or "") for item in tokens}
        yes_token = token_by_outcome.get("yes") or token_by_outcome.get("up") or (str(tokens[0].get("t")) if tokens else None)
        no_token = token_by_outcome.get("no") or token_by_outcome.get("down") or (str(tokens[1].get("t")) if len(tokens) > 1 else None)
        mapping_ok = (
            bool(yes_token and no_token)
            and (not gamma_yes_token_id or str(gamma_yes_token_id) == str(yes_token))
            and (not gamma_no_token_id or str(gamma_no_token_id) == str(no_token))
        )
        book = await self.get_order_book(str(yes_token)) if yes_token else {}
        min_order_size = _decimal(info.get("mos") or book.get("min_order_size"))
        min_tick_size = _decimal(info.get("mts") or book.get("tick_size"))
        best_ask = _best_ask(book)
        one_dollar_valid = bool(min_order_size is None or Decimal("1") >= min_order_size)
        return {
            "event_id": event_id,
            "condition_id": condition_id,
            "yes_token_id": yes_token,
            "no_token_id": no_token,
            "gamma_yes_token_id": gamma_yes_token_id,
            "gamma_no_token_id": gamma_no_token_id,
            "token_mapping_status": "matched" if mapping_ok else "mismatch",
            "min_order_size": float(min_order_size) if min_order_size is not None else None,
            "min_tick_size": float(min_tick_size) if min_tick_size is not None else None,
            "maker_base_fee": info.get("mbf"),
            "taker_base_fee": info.get("tbf"),
            "fee_details": info.get("fd"),
            "rfq_enabled": bool(info.get("rfqe")),
            "itode": bool(info.get("itode")),
            "accepting_orders": bool(info.get("accepting_orders", True)),
            "one_dollar_valid": one_dollar_valid,
            "minimum_viable_amount_usd": float(min_order_size) if min_order_size else None,
            "best_bid": _best_bid(book),
            "best_ask": best_ask,
            "orderbook_depth": {"bids": book.get("bids") or [], "asks": book.get("asks") or []},
            "market_resolved": bool(info.get("market_resolved") or info.get("resolved")),
            "winning_asset_id": info.get("winning_asset_id"),
            "winning_outcome": info.get("winning_outcome"),
            "source": "public_rest",
            "last_update_at": now_iso(),
            "raw_market_info": info,
            "raw_orderbook": book,
        }


class MockPublicClobClient(PublicClobClient):
    def __init__(self):
        super().__init__("mock://clob")

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        return {
            "asset_id": token_id,
            "bids": [{"price": "0.49", "size": "100"}],
            "asks": [{"price": "0.51", "size": "100"}],
            "min_order_size": "1",
            "tick_size": "0.01",
            "timestamp": "mock",
        }

    async def get_market_info(self, condition_id: str) -> dict[str, Any]:
        return {
            "condition_id": condition_id,
            "t": [{"t": "yes-token", "o": "Yes"}, {"t": "no-token", "o": "No"}],
            "mos": 1,
            "mts": 0.01,
            "mbf": 0,
            "tbf": 0.07,
            "fd": {"r": 0.07, "e": 2, "to": True},
            "rfqe": False,
            "itode": False,
            "accepting_orders": True,
        }


from __future__ import annotations

import base64
import os
import time
from typing import Any

import httpx
from py_clob_client_v2.signing.hmac import build_hmac_signature

from .base import TradingAdapter
from ..config import LiveConfig


INITIAL_CURSOR = "MA=="
END_CURSOR = "LTE="


def _first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class RealPolymarketTradingAdapter(TradingAdapter):
    """Read-only CLOB V2 adapter.

    This adapter intentionally implements authenticated GET requests only.
    Order creation and cancellation remain blocked even when real credentials
    are configured.
    """

    name = "polymarket"

    def __init__(self, config: LiveConfig, timeout: float = 20):
        self.config = config
        self.timeout = timeout

    def _credentials(self) -> dict[str, str]:
        values = {
            "api_key": os.getenv("POLYMARKET_API_KEY", "").strip(),
            "api_secret": os.getenv("POLYMARKET_API_SECRET", "").strip(),
            "api_passphrase": os.getenv("POLYMARKET_API_PASSPHRASE", "").strip(),
            "signer_address": self.config.signer_address.strip(),
        }
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise RuntimeError(f"Polymarket read-only adapter missing: {', '.join(missing)}")
        secret = values["api_secret"]
        try:
            decoded = base64.b64decode(secret, altchars=b"-_", validate=True)
        except Exception as exc:
            raise RuntimeError("Polymarket API secret is not valid base64") from exc
        if len(decoded) != 32:
            raise RuntimeError("Polymarket API secret must decode to 32 bytes")
        return values

    def _headers(self, method: str, path: str) -> dict[str, str]:
        creds = self._credentials()
        timestamp = int(time.time())
        return {
            "POLY_ADDRESS": creds["signer_address"],
            "POLY_SIGNATURE": build_hmac_signature(creds["api_secret"], timestamp, method, path),
            "POLY_TIMESTAMP": str(timestamp),
            "POLY_API_KEY": creds["api_key"],
            "POLY_PASSPHRASE": creds["api_passphrase"],
        }

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
        host: str | None = None,
    ) -> Any:
        headers = self._headers("GET", path) if authenticated else None
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            response = await client.get(
                f"{(host or self.config.clob_host).rstrip('/')}{path}",
                headers=headers,
                params=params or {},
            )
            response.raise_for_status()
            return response.json()

    async def _paginated_get(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor = INITIAL_CURSOR
        base_params = dict(params or {})
        while cursor != END_CURSOR:
            payload = await self._get_json(path, params={**base_params, "next_cursor": cursor})
            if isinstance(payload, list):
                results.extend(item for item in payload if isinstance(item, dict))
                break
            if not isinstance(payload, dict):
                break
            data = payload.get("data") or []
            results.extend(item for item in data if isinstance(item, dict))
            next_cursor = payload.get("next_cursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = str(next_cursor)
        return results

    async def get_market_info(self, condition_id: str) -> dict[str, Any]:
        payload = await self._get_json(f"/markets/{condition_id}", authenticated=False)
        if isinstance(payload, dict):
            payload.setdefault("condition_id", condition_id)
            payload.setdefault("source", "polymarket_public_clob")
            return payload
        return {"condition_id": condition_id, "status": "unexpected_response", "raw": payload}

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        payload = await self._get_json("/book", params={"token_id": token_id}, authenticated=False)
        if isinstance(payload, dict):
            payload.setdefault("asset_id", token_id)
            payload.setdefault("source", "polymarket_public_clob")
            return payload
        return {"asset_id": token_id, "status": "unexpected_response", "raw": payload}

    async def get_balance(self) -> dict[str, Any]:
        payload = await self._get_json(
            "/balance-allowance",
            params={"asset_type": "COLLATERAL", "signature_type": str(self.config.signature_type)},
        )
        balance = _first_value(payload if isinstance(payload, dict) else {}, "balance", "available_balance", "cash")
        return {"status": "ok", "balance_usd": _as_float(balance), "raw": payload}

    async def get_allowances(self) -> dict[str, Any]:
        payload = await self._get_json(
            "/balance-allowance",
            params={"asset_type": "COLLATERAL", "signature_type": str(self.config.signature_type)},
        )
        allowance = _first_value(payload if isinstance(payload, dict) else {}, "allowance", "allowance_usdc")
        return {"status": "ok", "allowance_usd": _as_float(allowance), "raw": payload}

    async def get_open_orders(self) -> list[dict[str, Any]]:
        return [self._normalize_order(order) for order in await self._paginated_get("/data/orders")]

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        payload = await self._get_json(f"/data/order/{order_id}")
        return self._normalize_order(payload) if isinstance(payload, dict) else None

    async def get_trades(self) -> list[dict[str, Any]]:
        return [self._normalize_trade(trade) for trade in await self._paginated_get("/data/trades")]

    async def get_positions(self) -> list[dict[str, Any]]:
        address = self.config.funder_address or self.config.profile_address
        if not address:
            return []
        payload = await self._get_json(
            "/positions",
            params={"user": address, "limit": 500, "sizeThreshold": 0},
            authenticated=False,
            host=self.config.data_api_host,
        )
        if not isinstance(payload, list):
            return []
        return [self._normalize_position(position) for position in payload if isinstance(position, dict)]

    def _normalize_order(self, order: dict[str, Any]) -> dict[str, Any]:
        order_id = _first_value(order, "id", "order_id", "orderID", "hash")
        return {
            **order,
            "polymarket_order_id": str(order_id) if order_id is not None else None,
            "condition_id": _first_value(order, "market", "condition_id", "conditionId"),
            "token_id": _first_value(order, "asset_id", "token_id", "assetId"),
            "side": str(_first_value(order, "side") or "").lower() or None,
            "price": _as_float(_first_value(order, "price", "original_price")),
            "size": _as_float(_first_value(order, "size", "original_size")),
            "status": str(_first_value(order, "status", "state") or "live").lower(),
            "raw": order,
        }

    def _normalize_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
        trade_id = _first_value(trade, "id", "trade_id", "tradeID", "transaction_hash")
        order_id = _first_value(trade, "order_id", "orderID", "orderId", "maker_order_id", "taker_order_id")
        timestamp = _first_value(trade, "match_time", "matched_at", "timestamp", "created_at")
        return {
            **trade,
            "polymarket_trade_id": str(trade_id) if trade_id is not None else None,
            "polymarket_order_id": str(order_id) if order_id is not None else None,
            "condition_id": _first_value(trade, "market", "condition_id", "conditionId"),
            "token_id": _first_value(trade, "asset_id", "token_id", "assetId"),
            "side": str(_first_value(trade, "side") or "").lower() or None,
            "price": _as_float(_first_value(trade, "price")),
            "size": _as_float(_first_value(trade, "size", "amount")),
            "fee": _as_float(_first_value(trade, "fee")) or 0,
            "status": str(_first_value(trade, "status") or "matched").lower(),
            "matched_at": str(timestamp) if timestamp is not None else None,
            "raw_message": trade,
        }

    def _normalize_position(self, position: dict[str, Any]) -> dict[str, Any]:
        size = _as_float(_first_value(position, "size", "quantity", "shares")) or 0
        return {
            **position,
            "condition_id": _first_value(position, "conditionId", "condition_id", "market"),
            "token_id": _first_value(position, "asset", "asset_id", "token_id"),
            "outcome": _first_value(position, "outcome"),
            "size": size,
            "average_price": _as_float(_first_value(position, "avgPrice", "average_price", "price")),
            "orphan": abs(size) > 0,
            "raw": position,
        }

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

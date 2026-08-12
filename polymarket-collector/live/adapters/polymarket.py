from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import asyncio
from typing import Any, Protocol

from eth_account import Account

from .base import TradingAdapter
from ..config import LiveConfig
from ..order_book import canonical_decimal, decimal_value
from ..secrets import (
    EnvSecretProvider,
    GoogleSecretManagerProvider,
    PrivateKeySecretProvider,
    SecretProvider,
)
from ..strategy import fee_amount
from ..strategy_repository import sanitize


PUSD_SCALE = Decimal("1000000")
WALLET_TYPES = {
    0: "EOA",
    1: "POLY_PROXY",
    2: "GNOSIS_SAFE",
    3: "DEPOSIT_WALLET",
}


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    return value


def _d(value: Any) -> Decimal:
    return decimal_value(value) or Decimal("0")


class SecureClientLike(Protocol):
    wallet: Any
    signer: Any
    wallet_type: Any

    async def get_balance_allowance(self, **kwargs: Any) -> Any: ...
    async def create_market_order(self, **kwargs: Any) -> Any: ...
    async def create_limit_order(self, **kwargs: Any) -> Any: ...
    async def post_order(self, signed_order: Any) -> Any: ...
    async def get_order(self, **kwargs: Any) -> Any: ...
    def list_open_orders(self, **kwargs: Any) -> Any: ...
    def list_account_trades(self, **kwargs: Any) -> Any: ...
    def list_positions(self, **kwargs: Any) -> Any: ...
    async def cancel_order(self, **kwargs: Any) -> Any: ...
    async def cancel_orders(self, **kwargs: Any) -> Any: ...
    async def cancel_market_orders(self, **kwargs: Any) -> Any: ...
    async def redeem_positions(self, **kwargs: Any) -> Any: ...


class RealPolymarketTradingAdapter(TradingAdapter):
    """Unified SDK adapter with explicit allowance preflight and no auto-approval path."""

    name = "polymarket"

    def __init__(
        self,
        config: LiveConfig,
        *,
        secret_provider: SecretProvider | None = None,
        secure_client: SecureClientLike | None = None,
        public_client: Any | None = None,
    ):
        self.config = config
        self.secret_provider = secret_provider or (
            PrivateKeySecretProvider(
                GoogleSecretManagerProvider(
                    config.google_project_id,
                    config.google_secret_prefix,
                    config.google_private_key_secret_version,
                ),
                EnvSecretProvider(),
            )
            if config.google_project_id
            else EnvSecretProvider()
        )
        self._secure_client = secure_client
        self._public_client = public_client
        self._client_lock = asyncio.Lock()
        self.identity: dict[str, Any] = {
            "status": "NOT_INITIALIZED",
            "signer": None,
            "wallet": None,
            "wallet_type": None,
        }

    def _secret(self, name: str) -> str:
        try:
            value = self.secret_provider.get_secret(name)
        except Exception as exc:
            raise RuntimeError(f"Secret Manager access failed for {name}: {type(exc).__name__}") from exc
        if not value:
            raise RuntimeError(f"required secret is missing: {name}")
        return value.strip()

    async def _client(self) -> SecureClientLike:
        if self._secure_client is not None:
            return self._secure_client
        if not (
            self.config.real_submission_armed()
            or self.config.private_signing_readiness_enabled
        ):
            raise RuntimeError("private signing was not requested for this execution mode")
        async with self._client_lock:
            if self._secure_client is not None:
                return self._secure_client
            from polymarket import ApiKeyCreds, AsyncSecureClient

            private_key = self._secret("POLYMARKET_PRIVATE_KEY")
            api_key = self._secret("POLYMARKET_API_KEY")
            api_secret = self._secret("POLYMARKET_API_SECRET")
            passphrase = self._secret("POLYMARKET_API_PASSPHRASE")
            derived_signer = Account.from_key(private_key).address
            if (
                self.config.signer_address
                and derived_signer.lower() != self.config.signer_address.lower()
            ):
                self.identity["status"] = "SIGNER_MISMATCH"
                raise RuntimeError("signer address does not match configured expected signer")
            wallet = self.config.funder_address or self.config.profile_address or None
            if not wallet:
                raise RuntimeError("configured account wallet/funder address is missing")
            credentials = ApiKeyCreds(
                apiKey=api_key, secret=api_secret, passphrase=passphrase
            )
            if self.config.real_submission_armed():
                client = await AsyncSecureClient.create(
                    private_key=private_key,
                    wallet=wallet,
                    credentials=credentials,
                )
            else:
                # Explicit readiness must never deploy or mutate a wallet.
                client = await AsyncSecureClient._create(
                    private_key=private_key,
                    wallet=wallet,
                    credentials=credentials,
                    validate_credentials=True,
                )
            actual_wallet = str(client.wallet)
            actual_signer = str(client.signer)
            actual_type = str(client.wallet_type)
            expected_type = WALLET_TYPES.get(self.config.signature_type)
            if actual_signer.lower() != derived_signer.lower():
                raise RuntimeError("SDK signer identity mismatch")
            if actual_wallet.lower() != wallet.lower():
                raise RuntimeError("SDK wallet identity mismatch")
            if expected_type and actual_type != expected_type:
                raise RuntimeError("SDK wallet type mismatch")
            self.identity = {
                "status": "VERIFIED",
                "signer": actual_signer,
                "wallet": actual_wallet,
                "wallet_type": actual_type,
            }
            self._secure_client = client
            return client

    async def _public(self) -> Any:
        if self._public_client is None:
            from polymarket import AsyncPublicClient

            self._public_client = AsyncPublicClient()
        return self._public_client

    async def identity_preflight(self) -> dict[str, Any]:
        try:
            await self._client()
            return dict(self.identity)
        except Exception as exc:
            return {
                **self.identity,
                "status": self.identity.get("status")
                if self.identity.get("status") != "NOT_INITIALIZED"
                else "FAILED",
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def get_closed_only_mode(self) -> dict[str, Any]:
        try:
            closed_only = bool(await (await self._client()).get_closed_only_mode())
            return {
                "status": "CLOSED_ONLY" if closed_only else "FULL_TRADING",
                "closed_only": closed_only,
            }
        except Exception as exc:
            return {
                "status": "FAILED", "closed_only": None,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }

    async def get_market_info(self, condition_id: str) -> dict[str, Any]:
        public = await self._public()
        pages = public.list_markets(condition_ids=[condition_id], page_size=1)
        page = await pages.first_page()
        if not page.items:
            return {"condition_id": condition_id, "status": "not_found"}
        payload = _dump(page.items[0])
        return {**payload, "condition_id": condition_id, "source": "polymarket-client"}

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        book = await (await self._public()).get_order_book(token_id=token_id)
        payload = _dump(book)
        payload.setdefault("asset_id", token_id)
        payload["source"] = "polymarket-client"
        return payload

    async def _balance_allowance(
        self, *, asset_type: str, token_id: str | None = None
    ) -> dict[str, Any]:
        result = await (await self._client()).get_balance_allowance(
            asset_type=asset_type, token_id=token_id
        )
        payload = _dump(result)
        balance_raw = int(payload.get("balance") or 0)
        allowances = {
            str(address): int(amount)
            for address, amount in (payload.get("allowances") or {}).items()
        }
        return {
            "status": "ok",
            "asset_type": asset_type,
            "token_id": token_id,
            "balance_raw": balance_raw,
            "balance_text": canonical_decimal(Decimal(balance_raw) / PUSD_SCALE),
            "allowances_raw": allowances,
            "minimum_allowance_raw": min(allowances.values()) if allowances else 0,
        }

    async def get_balance(self) -> dict[str, Any]:
        payload = await self._balance_allowance(asset_type="COLLATERAL")
        return {
            "status": payload["status"],
            "balance_usd": payload["balance_text"],
            "balance_raw": payload["balance_raw"],
        }

    async def get_allowances(self) -> dict[str, Any]:
        payload = await self._balance_allowance(asset_type="COLLATERAL")
        return {
            "status": payload["status"],
            "allowance_usd": canonical_decimal(
                Decimal(payload["minimum_allowance_raw"]) / PUSD_SCALE
            ),
            "allowances_raw": payload["allowances_raw"],
        }

    async def get_open_orders(self) -> list[dict[str, Any]]:
        pages = (await self._client()).list_open_orders()
        return [self._normalize_order(order) async for order in pages.iter_items()]

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        try:
            result = await (await self._client()).get_order(order_id=order_id)
        except Exception as exc:
            if "404" in str(exc) or "not found" in str(exc).lower():
                return None
            raise
        return self._normalize_order(result)

    async def get_trades(self) -> list[dict[str, Any]]:
        pages = (await self._client()).list_account_trades()
        return [self._normalize_trade(trade) async for trade in pages.iter_items()]

    async def get_positions(self) -> list[dict[str, Any]]:
        pages = (await self._client()).list_positions(
            user=self.config.funder_address or self.config.profile_address,
            size_threshold=0,
            page_size=100,
        )
        return [self._normalize_position(position) async for position in pages.iter_items()]

    async def _assert_allowance(
        self,
        *,
        side: str,
        token_id: str,
        requested_amount: Decimal,
        requested_shares: Decimal,
    ) -> None:
        asset_type = "COLLATERAL" if side == "BUY" else "CONDITIONAL"
        payload = await self._balance_allowance(
            asset_type=asset_type,
            token_id=token_id if asset_type == "CONDITIONAL" else None,
        )
        required = requested_amount if side == "BUY" else requested_shares
        required_raw = int((required * PUSD_SCALE).to_integral_value(rounding="ROUND_CEILING"))
        if payload["balance_raw"] < required_raw:
            raise RuntimeError("INSUFFICIENT_BALANCE")
        if not payload["allowances_raw"] or payload["minimum_allowance_raw"] < required_raw:
            # Do not call place_*: those methods perform automatic allowance recovery.
            raise RuntimeError("INSUFFICIENT_ALLOWANCE_APPROVAL_REQUIRED")

    async def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        if not self.config.real_submission_armed():
            return {
                "success": False,
                "status": "blocked",
                "failure_reason": "REAL_SUBMISSION_NOT_ARMED",
            }
        if not order.get("durable_intent_reserved"):
            return {
                "success": False,
                "status": "blocked",
                "failure_reason": "DURABLE_INTENT_REQUIRED",
            }
        side = str(order.get("side") or "").upper()
        token_id = str(order.get("token_id") or "")
        order_type = str(order.get("order_type") or "").upper()
        requested_amount = _d(order.get("requested_amount_usd"))
        requested_shares = _d(order.get("requested_size"))
        if side == "BUY":
            max_price = _d(order.get("max_price"))
            max_tokens = _d(order.get("max_tokens"))
            max_spend = _d(order.get("max_spend"))
            if (
                max_tokens <= 0
                or max_tokens > self.config.max_trade_tokens
                or max_price <= 0
                or requested_amount > max_tokens * max_price
                or max_spend > self.config.max_trade_amount_usd
            ):
                return {
                    "success": False,
                    "status": "blocked",
                    "failure_reason": "CANARY_EXPOSURE_CAP_EXCEEDED",
                }
        try:
            await self._assert_allowance(
                side=side,
                token_id=token_id,
                requested_amount=requested_amount,
                requested_shares=requested_shares,
            )
            client = await self._client()
            if order_type == "FAK":
                if side == "BUY":
                    signed = await client.create_limit_order(
                        token_id=token_id,
                        price=canonical_decimal(_d(order.get("max_price"))),
                        size=canonical_decimal(max_tokens),
                        side="BUY",
                    )
                    if isinstance(signed, dict):
                        signed = {**signed, "order_type": "FAK"}
                    else:
                        signed = replace(signed, order_type="FAK")
                    maker_amount = getattr(signed, "maker_amount", None)
                    taker_amount = getattr(signed, "taker_amount", None)
                    if (
                        maker_amount is not None
                        and taker_amount is not None
                        and (
                            int(maker_amount) > int(self.config.max_trade_amount_usd * PUSD_SCALE)
                            or int(taker_amount) > int(self.config.max_trade_tokens * PUSD_SCALE)
                        )
                    ):
                        raise RuntimeError("SIGNED_CANARY_CAP_EXCEEDED")
                elif side == "SELL":
                    signed = await client.create_market_order(
                        token_id=token_id,
                        side="SELL",
                        shares=canonical_decimal(requested_shares),
                        min_price=canonical_decimal(_d(order.get("min_price"))),
                        order_type="FAK",
                    )
                else:
                    raise ValueError("invalid order side")
            elif order_type == "GTC" and side == "SELL":
                signed = await client.create_limit_order(
                    token_id=token_id,
                    price=canonical_decimal(_d(order.get("requested_price"))),
                    size=canonical_decimal(requested_shares),
                    side="SELL",
                )
            else:
                raise ValueError("unsupported order contract")
            response = await client.post_order(signed)
            return self._normalize_response(response)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            status = "blocked" if "ALLOWANCE" in message or "BALANCE" in message else "unknown"
            return {
                "success": False,
                "status": status,
                "failure_reason": message[:300],
            }

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not self.config.real_submission_armed():
            return {"success": False, "status": "blocked", "failure_reason": "REAL_MANAGEMENT_NOT_ARMED"}
        try:
            response = await (await self._client()).cancel_order(order_id=order_id)
            payload = _dump(response)
            canceled = set(payload.get("canceled") or [])
            not_canceled = payload.get("not_canceled") or {}
            return {
                "success": order_id in canceled and not not_canceled,
                "status": "cancelled" if order_id in canceled else "uncertain",
                "canceled": sorted(canceled),
                "not_canceled": not_canceled,
            }
        except Exception as exc:
            return {
                "success": False,
                "status": "uncertain",
                "failure_reason": f"{type(exc).__name__}: {exc}"[:300],
            }

    async def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        if not self.config.real_submission_armed():
            return {"success": False, "status": "blocked", "failure_reason": "REAL_MANAGEMENT_NOT_ARMED"}
        response = await (await self._client()).cancel_orders(order_ids=order_ids)
        payload = _dump(response)
        return {
            "success": not bool(payload.get("not_canceled")),
            "status": "cancelled" if not payload.get("not_canceled") else "partial",
            **payload,
        }

    async def cancel_market_orders(
        self, condition_id: str, token_id: str | None = None
    ) -> dict[str, Any]:
        if not self.config.real_submission_armed():
            return {"success": False, "status": "blocked", "failure_reason": "REAL_MANAGEMENT_NOT_ARMED"}
        response = await (await self._client()).cancel_market_orders(
            market=condition_id, token_id=token_id
        )
        payload = _dump(response)
        return {
            "success": not bool(payload.get("not_canceled")),
            "status": "cancelled" if not payload.get("not_canceled") else "partial",
            **payload,
        }

    async def cancel_all_orders(self) -> dict[str, Any]:
        return {
            "success": False,
            "status": "blocked",
            "failure_reason": "GLOBAL_CANCEL_ALL_PROHIBITED",
        }

    async def heartbeat(self) -> dict[str, Any]:
        if not self.config.real_submission_armed():
            return {"success": False, "status": "disabled"}
        try:
            client = await self._client()
            # Unified SDK b21 has no public heartbeat wrapper. Its authenticated
            # transport signs this official endpoint with the owning credentials.
            payload = await client._ctx.secure_clob.post_json("/heartbeats", json=None)  # type: ignore[attr-defined]
            return {
                "success": isinstance(payload, dict) and payload.get("status") == "ok",
                "status": str(payload.get("status") if isinstance(payload, dict) else "unknown"),
            }
        except Exception as exc:
            return {
                "success": False,
                "status": "failed",
                "failure_reason": f"{type(exc).__name__}: {exc}"[:300],
            }

    async def redeem(self, condition_id: str, *, authorized_intent: bool = False) -> dict[str, Any]:
        if not self.config.real_submission_armed() or not authorized_intent:
            return {"success": False, "status": "blocked", "failure_reason": "REDEMPTION_NOT_AUTHORIZED"}
        try:
            handle = await (await self._client()).redeem_positions(condition_id=condition_id)
            outcome = await handle.wait()
            payload = _dump(outcome)
            return {
                "success": True,
                "status": "confirmed",
                "transaction_hash": payload.get("transaction_hash"),
                "raw": sanitize(payload),
            }
        except Exception as exc:
            return {
                "success": False,
                "status": "failed",
                "failure_reason": f"{type(exc).__name__}: {exc}"[:300],
            }

    @staticmethod
    def _normalize_response(response: Any) -> dict[str, Any]:
        payload = _dump(response)
        if payload.get("ok") is True:
            status = str(payload.get("status") or "unknown").lower()
            return {
                "success": True,
                "status": status,
                "polymarket_order_id": payload.get("order_id"),
                "making_amount": payload.get("making_amount"),
                "taking_amount": payload.get("taking_amount"),
                "trade_ids": payload.get("trade_ids") or [],
                "transaction_hashes": payload.get("transactions_hashes") or [],
                "fills": [],
            }
        return {
            "success": False,
            "status": "rejected",
            "failure_reason": payload.get("code") or "unknown",
            "message": payload.get("message") or "",
            "fills": [],
        }

    @staticmethod
    def _normalize_order(value: Any) -> dict[str, Any]:
        payload = _dump(value)
        return {
            "polymarket_order_id": payload.get("id"),
            "condition_id": payload.get("market"),
            "token_id": payload.get("token_id"),
            "side": str(payload.get("side") or "").lower(),
            "price": payload.get("price"),
            "size": payload.get("original_size"),
            "filled_size": payload.get("size_matched"),
            "status": str(payload.get("status") or "").lower(),
            "raw": sanitize(payload),
        }

    @staticmethod
    def _normalize_trade(value: Any) -> dict[str, Any]:
        payload = _dump(value)
        maker_orders = payload.get("maker_orders") or []
        order_id = payload.get("taker_order_id")
        fee_rate_bps = decimal_value(payload.get("fee_rate_bps"))
        price = decimal_value(payload.get("price"))
        size = decimal_value(payload.get("size"))
        fee = (
            fee_amount(size, price, fee_rate_bps / Decimal("10000"))
            if fee_rate_bps is not None and price is not None and size is not None
            else None
        )
        return {
            "polymarket_trade_id": payload.get("id"),
            "polymarket_order_id": order_id,
            "condition_id": payload.get("market"),
            "token_id": payload.get("token_id"),
            "side": str(payload.get("side") or "").lower(),
            "price": payload.get("price"),
            "size": payload.get("size"),
            "fee": canonical_decimal(fee) if fee is not None else None,
            "fee_rate_bps": canonical_decimal(fee_rate_bps) if fee_rate_bps is not None else None,
            "fee_source": "polymarket_fee_rate_bps" if fee is not None else None,
            "fee_verification_status": "VERIFIED" if fee is not None else "UNKNOWN",
            "status": str(payload.get("status") or "matched").lower(),
            "matched_at": payload.get("matched_at"),
            "transaction_hash": payload.get("transaction_hash"),
            "maker_order_ids": [
                item.get("order_id") for item in maker_orders if isinstance(item, dict)
            ],
            "raw_message": sanitize(payload),
        }

    @staticmethod
    def _normalize_position(value: Any) -> dict[str, Any]:
        payload = _dump(value)
        return {
            "condition_id": payload.get("condition_id"),
            "token_id": payload.get("token_id"),
            "outcome": payload.get("outcome"),
            "size": payload.get("size") or "0",
            "average_price": payload.get("avg_price"),
            "redeemable": payload.get("redeemable"),
            "current_value": payload.get("current_value"),
            "orphan": _d(payload.get("size")) > 0,
            "raw": sanitize(payload),
        }

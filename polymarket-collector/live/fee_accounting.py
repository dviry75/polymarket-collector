"""Authoritative fee-truth resolution for account trades.

The exchange trade feed can return ``fee_rate_bps: 0`` for a fill that in fact
incurred a fee (crypto fee-enabled markets compute the taker fee from a formula
rather than a flat bps on the trade message). Trusting that zero and writing it
as ``fee=0`` / ``VERIFIED`` understates realised cost.

Truth hierarchy (task P1 / #28):
  1. authoritative fee from the trade API (``fee_rate_bps > 0``)      -> VERIFIED
  2. deterministic computation when the market fee rule is complete    -> COMPUTED
  3. an authoritative zero (market has fees disabled)                  -> VERIFIED
  4. anything else                                                     -> UNKNOWN

``0`` + ``VERIFIED`` is only ever produced when the market authoritatively has
no fees.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from .order_book import decimal_value
from .strategy import fee_amount


FEE_STATUS_VERIFIED = "VERIFIED"
FEE_STATUS_COMPUTED = "COMPUTED"
FEE_STATUS_UNKNOWN = "UNKNOWN"


def _market_fee_details(market: dict[str, Any] | None) -> dict[str, Any]:
    if not market:
        return {}
    raw = market.get("fee_details")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def market_fees_enabled(market: dict[str, Any] | None) -> bool | None:
    """True/False when the market states it explicitly, else None (unknown)."""
    details = _market_fee_details(market)
    if "fees_enabled" in details:
        return bool(details.get("fees_enabled"))
    taker = decimal_value((market or {}).get("taker_base_fee"))
    maker = decimal_value((market or {}).get("maker_base_fee"))
    if taker is not None or maker is not None:
        return bool((taker or Decimal("0")) > 0 or (maker or Decimal("0")) > 0)
    return None


def _market_taker_rate(market: dict[str, Any] | None) -> Decimal | None:
    details = _market_fee_details(market)
    rate = decimal_value(details.get("rate") or details.get("r"))
    if rate is not None and rate > 0:
        return rate
    direct = decimal_value((market or {}).get("taker_base_fee"))
    if direct is not None and direct > 0:
        return direct
    return None


def _is_taker(trade: dict[str, Any]) -> bool:
    role = str(
        trade.get("liquidity_role")
        or trade.get("trader_side")
        or (trade.get("raw_message") or {}).get("trader_side")
        or ""
    ).upper()
    return role == "TAKER"


def resolve_trade_fee(
    trade: dict[str, Any],
    market: dict[str, Any] | None,
) -> tuple[Decimal, str, str | None]:
    """Return ``(fee, fee_verification_status, fee_source)`` for one trade."""
    price = decimal_value(trade.get("price"))
    size = decimal_value(trade.get("size") or trade.get("shares_text"))
    fee_rate_bps = decimal_value(trade.get("fee_rate_bps"))

    # An explicit, already-verified fee on the normalized trade (the adapter
    # only sets this from a positive bps) is authoritative.
    explicit_fee = decimal_value(trade.get("fee"))
    explicit_status = str(trade.get("fee_verification_status") or "").upper()
    if explicit_fee is not None and explicit_status == FEE_STATUS_VERIFIED:
        return (
            explicit_fee,
            FEE_STATUS_VERIFIED,
            trade.get("fee_source") or "polymarket_fee_rate_bps",
        )

    if (
        fee_rate_bps is not None
        and fee_rate_bps > 0
        and price is not None
        and size is not None
    ):
        fee = fee_amount(size, price, fee_rate_bps / Decimal("10000"))
        return fee, FEE_STATUS_VERIFIED, "polymarket_fee_rate_bps"

    if explicit_fee is not None and explicit_fee > 0:
        # A concrete positive fee with no verification tag (test fixtures,
        # legacy feeds): trust the amount but do not over-claim VERIFIED.
        return explicit_fee, FEE_STATUS_COMPUTED, (
            trade.get("fee_source") or "reported_trade_fee"
        )

    fees_enabled = market_fees_enabled(market)

    if fees_enabled is True and _is_taker(trade) and price is not None and size is not None:
        rate = _market_taker_rate(market)
        if rate is not None:
            fee = fee_amount(size, price, rate)
            return fee, FEE_STATUS_COMPUTED, "crypto_fees_v2_deterministic"
        return Decimal("0"), FEE_STATUS_UNKNOWN, None

    if fees_enabled is False:
        # The market authoritatively has no fees: a real, verified zero.
        return Decimal("0"), FEE_STATUS_VERIFIED, "fees_disabled_market"

    if (
        fee_rate_bps is not None
        and fee_rate_bps == 0
        and not _is_taker(trade)
        and fees_enabled is not True
    ):
        # Maker fill in a market with no evidence of maker fees.
        return Decimal("0"), FEE_STATUS_VERIFIED, "maker_fill_zero_fee"

    return Decimal("0"), FEE_STATUS_UNKNOWN, None

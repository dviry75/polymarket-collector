"""Order-book ladder walk for exit-price estimation.

Pure functions, no I/O. Shared by the PAPER_TRADING engine (executable exit
price from a persisted snapshot) and the live exit-forensics drain task
(expected VWAP at SELL submit, walked for our own size).
"""

from __future__ import annotations

from decimal import Decimal
import json
from typing import Any

_ZERO = Decimal("0")
_ONE = Decimal("1")


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


def normalize_bid_levels(raw_levels: Any) -> list[tuple[Decimal, Decimal]]:
    """Parse ``[{"price": .., "size": ..}, ...]`` into ``(price, size)`` tuples
    sorted best-first (price descending).

    Accepts an already-parsed list, or a JSON string. Malformed entries and
    non-positive / out-of-[0,1] levels are silently dropped.
    """
    if isinstance(raw_levels, str):
        try:
            raw_levels = json.loads(raw_levels)
        except (TypeError, ValueError):
            raw_levels = []
    levels: list[tuple[Decimal, Decimal]] = []
    for level in raw_levels or []:
        if not isinstance(level, dict):
            continue
        price = _decimal(level.get("price"))
        size = _decimal(level.get("size"))
        if (
            price is not None
            and size is not None
            and size > _ZERO
            and _ZERO <= price <= _ONE
        ):
            levels.append((price, size))
    levels.sort(key=lambda item: item[0], reverse=True)
    return levels


def book_walk_vwap(
    bid_levels: Any,
    size: Any,
    *,
    best_bid: Any = None,
) -> tuple[Decimal | None, str, Decimal]:
    """Sell ``size`` shares from the top of a bid ladder.

    Returns ``(vwap, method, fillable_shares)``:

    - ``(<vwap>, "ORDER_BOOK_VWAP", <size>)`` — fully fillable from visible depth
    - ``(<vwap>, "ORDER_BOOK_VWAP_PARTIAL", <n>)`` — only ``<n>`` shares of depth;
      vwap is over those ``<n>`` shares
    - ``(<best_bid>, "BEST_BID_FALLBACK", 0)`` — no usable depth, ``best_bid`` given
    - ``(None, "NONE", 0)`` — nothing usable
    """
    requested = _decimal(size)
    fallback_bid = _decimal(best_bid)
    levels = normalize_bid_levels(bid_levels)

    def _fallback() -> tuple[Decimal | None, str, Decimal]:
        if fallback_bid is not None:
            return fallback_bid, "BEST_BID_FALLBACK", _ZERO
        return None, "NONE", _ZERO

    if requested is None or requested <= _ZERO or not levels:
        return _fallback()

    remaining = requested
    notional = _ZERO
    for price, available in levels:
        filled = min(remaining, available)
        notional += filled * price
        remaining -= filled
        if remaining <= _ZERO:
            return notional / requested, "ORDER_BOOK_VWAP", requested

    filled_shares = requested - remaining
    if filled_shares > _ZERO:
        return notional / filled_shares, "ORDER_BOOK_VWAP_PARTIAL", filled_shares
    return _fallback()

"""Order-book ladder walk for exit-price estimation.

Pure functions, no I/O. Shared by the PAPER_TRADING engine (executable exit
price from a persisted snapshot), the live exit-forensics drain task (expected
VWAP at SELL submit, walked for our own size) and the entry-liquidity drain
task (simulated exit at entry time, walked for our own size and buffer
multiples).
"""

from __future__ import annotations

from collections import namedtuple
from decimal import Decimal
import json
from typing import Any

_ZERO = Decimal("0")
_ONE = Decimal("1")


BookWalk = namedtuple(
    "BookWalk", "vwap method fillable_shares worst_price levels_consumed"
)


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


def book_walk_detail(
    bid_levels: Any,
    size: Any,
    *,
    best_bid: Any = None,
) -> BookWalk:
    """Sell ``size`` shares from the top of a bid ladder.

    Same walk as :func:`book_walk_vwap`, but also reports ``worst_price`` (the
    price of the lowest bid level we would have to hit) and ``levels_consumed``
    (how many distinct price levels the fill touches).

    ``method`` values match :func:`book_walk_vwap`:

    - ``"ORDER_BOOK_VWAP"`` — fully fillable from visible depth
    - ``"ORDER_BOOK_VWAP_PARTIAL"`` — only ``fillable_shares`` of depth
    - ``"BEST_BID_FALLBACK"`` — no usable depth, ``best_bid`` given
    - ``"NONE"`` — nothing usable

    For the two fallback methods ``worst_price`` is ``None`` and
    ``levels_consumed`` is ``0``.
    """
    requested = _decimal(size)
    fallback_bid = _decimal(best_bid)
    levels = normalize_bid_levels(bid_levels)

    def _fallback() -> BookWalk:
        if fallback_bid is not None:
            return BookWalk(fallback_bid, "BEST_BID_FALLBACK", _ZERO, None, 0)
        return BookWalk(None, "NONE", _ZERO, None, 0)

    if requested is None or requested <= _ZERO or not levels:
        return _fallback()

    remaining = requested
    notional = _ZERO
    worst_price: Decimal | None = None
    consumed = 0
    for price, available in levels:
        filled = min(remaining, available)
        notional += filled * price
        remaining -= filled
        worst_price = price
        consumed += 1
        if remaining <= _ZERO:
            return BookWalk(
                notional / requested, "ORDER_BOOK_VWAP", requested,
                worst_price, consumed,
            )

    filled_shares = requested - remaining
    if filled_shares > _ZERO:
        return BookWalk(
            notional / filled_shares, "ORDER_BOOK_VWAP_PARTIAL", filled_shares,
            worst_price, consumed,
        )
    return _fallback()


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
    detail = book_walk_detail(bid_levels, size, best_bid=best_bid)
    return detail.vwap, detail.method, detail.fillable_shares


def cumulative_bid_depth(
    bid_levels: Any, price_levels: Any
) -> dict[Decimal, Decimal]:
    """Total shares resting on bids priced at/above each ``price_levels`` value.

    Returns ``{price_level: cumulative_size}``. A price level below every bid
    maps to the full visible bid depth; one above the best bid maps to ``0``.
    """
    levels = normalize_bid_levels(bid_levels)
    out: dict[Decimal, Decimal] = {}
    for raw in price_levels or []:
        floor = _decimal(raw)
        if floor is None:
            continue
        out[floor] = sum(
            (size for price, size in levels if price >= floor), _ZERO
        )
    return out

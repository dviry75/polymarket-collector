from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Iterable


def decimal_value(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def canonical_decimal(value: Decimal | Any) -> str:
    result = value if isinstance(value, Decimal) else decimal_value(value)
    if result is None:
        raise ValueError("invalid decimal")
    text = format(result, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass
class OrderBookState:
    asset_id: str
    generation: int = 0
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    ready: bool = False
    reason: str = "AWAITING_SNAPSHOT"
    last_timestamp: Decimal | None = None
    last_message_hash: str = ""
    update_number: int = 0

    def clear_for_resync(self, generation: int, reason: str) -> None:
        self.generation = generation
        self.bids.clear()
        self.asks.clear()
        self.ready = False
        self.reason = reason
        self.last_timestamp = None
        self.last_message_hash = ""
        self.update_number += 1

    @property
    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks else None

    def levels(self, side: str) -> list[dict[str, str]]:
        source = self.bids if side == "bids" else self.asks
        reverse = side == "bids"
        return [
            {"price": canonical_decimal(price), "size": canonical_decimal(source[price])}
            for price in sorted(source, reverse=reverse)
        ]

    def view(self, *, event_type: str, timestamp: str | None, message_hash: str) -> dict[str, Any]:
        best_bid = self.best_bid
        best_ask = self.best_ask
        return {
            "asset_id": self.asset_id,
            "event_type": event_type,
            "best_bid": canonical_decimal(best_bid) if best_bid is not None else None,
            "best_ask": canonical_decimal(best_ask) if best_ask is not None else None,
            "best_bid_size": canonical_decimal(self.bids[best_bid]) if best_bid is not None else None,
            "best_ask_size": canonical_decimal(self.asks[best_ask]) if best_ask is not None else None,
            "bids": self.levels("bids"),
            "asks": self.levels("asks"),
            "market_timestamp": timestamp,
            "book_ready": self.ready,
            "readiness_reason": self.reason,
            "generation": self.generation,
            "update_number": self.update_number,
            "message_hash": message_hash,
        }


@dataclass(frozen=True)
class AtomicBookFrame:
    event_type: str
    timestamp: str | None
    message_hash: str
    updates: tuple[dict[str, Any], ...]
    duplicate: bool = False
    out_of_order: bool = False

    def assets_at_exact_ask(self, price: Decimal) -> tuple[str, ...]:
        return tuple(
            str(update["asset_id"])
            for update in self.updates
            if update.get("book_ready") and decimal_value(update.get("best_ask")) == price
        )


class OrderBookSet:
    """Applies official CLOB snapshots and deltas without erasing untouched levels."""

    def __init__(self, asset_ids: Iterable[str] = ()):
        self.generation = 1
        self.books = {str(asset): OrderBookState(str(asset), self.generation) for asset in asset_ids}
        self._recent_hashes: deque[str] = deque(maxlen=4096)
        self._recent_hash_set: set[str] = set()

    def ensure_assets(self, asset_ids: Iterable[str]) -> None:
        wanted = {str(asset) for asset in asset_ids if asset}
        for asset in wanted:
            self.books.setdefault(asset, OrderBookState(asset, self.generation))
        for asset in list(self.books):
            if asset not in wanted:
                del self.books[asset]

    def mark_not_ready(self, reason: str) -> None:
        self.generation += 1
        for book in self.books.values():
            book.clear_for_resync(self.generation, reason)

    def event_ready(self, asset_ids: Iterable[str]) -> tuple[bool, str]:
        assets = [str(asset) for asset in asset_ids if asset]
        if not assets:
            return False, "NO_ASSETS"
        for asset in assets:
            book = self.books.get(asset)
            if book is None or not book.ready:
                return False, book.reason if book else "UNKNOWN_ASSET"
            if book.generation != self.generation:
                return False, "GENERATION_MISMATCH"
        return True, "READY"

    def apply(self, message: dict[str, Any]) -> AtomicBookFrame:
        event_type = str(message.get("event_type") or message.get("type") or "").lower()
        identity = json.dumps(message, sort_keys=True, separators=(",", ":"), default=str)
        message_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        timestamp_raw = message.get("timestamp")
        timestamp = str(timestamp_raw) if timestamp_raw not in (None, "") else None
        if message_hash in self._recent_hash_set:
            return AtomicBookFrame(event_type, timestamp, message_hash, (), duplicate=True)
        self._remember_hash(message_hash)

        if event_type == "book":
            asset = str(message.get("asset_id") or "")
            book = self.books.get(asset)
            if not book:
                return AtomicBookFrame(event_type, timestamp, message_hash, ())
            if self._is_out_of_order(book, timestamp):
                book.ready = False
                book.reason = "OUT_OF_ORDER"
                return AtomicBookFrame(event_type, timestamp, message_hash, (book.view(
                    event_type=event_type, timestamp=timestamp, message_hash=message_hash
                ),), out_of_order=True)
            bids = self._parse_levels(message.get("bids"))
            asks = self._parse_levels(message.get("asks"))
            book.bids = bids
            book.asks = asks
            book.ready = True
            book.reason = "READY"
            book.last_timestamp = decimal_value(timestamp)
            book.last_message_hash = message_hash
            book.update_number += 1
            return AtomicBookFrame(event_type, timestamp, message_hash, (book.view(
                event_type=event_type, timestamp=timestamp, message_hash=message_hash
            ),))

        if event_type == "price_change":
            changes = message.get("price_changes")
            if not isinstance(changes, list):
                changes = []
            grouped: dict[str, list[dict[str, Any]]] = {}
            for change in changes:
                if not isinstance(change, dict):
                    continue
                asset = str(change.get("asset_id") or message.get("asset_id") or "")
                if asset in self.books:
                    grouped.setdefault(asset, []).append(change)
            updates: list[dict[str, Any]] = []
            out_of_order = False
            for asset, asset_changes in grouped.items():
                book = self.books[asset]
                if self._is_out_of_order(book, timestamp):
                    book.ready = False
                    book.reason = "OUT_OF_ORDER"
                    out_of_order = True
                    updates.append(book.view(
                        event_type=event_type, timestamp=timestamp, message_hash=message_hash
                    ))
                    continue
                if not book.ready:
                    book.reason = "DELTA_BEFORE_SNAPSHOT"
                    updates.append(book.view(
                        event_type=event_type, timestamp=timestamp, message_hash=message_hash
                    ))
                    continue
                malformed = False
                for change in asset_changes:
                    side = str(change.get("side") or "").upper()
                    price = decimal_value(change.get("price"))
                    size = decimal_value(change.get("size"))
                    if side not in {"BUY", "SELL"} or price is None or size is None or price < 0 or size < 0:
                        malformed = True
                        break
                    levels = book.bids if side == "BUY" else book.asks
                    if size == 0:
                        levels.pop(price, None)
                    else:
                        levels[price] = size
                if malformed:
                    book.ready = False
                    book.reason = "MALFORMED_DELTA"
                else:
                    advertised_bid = decimal_value(asset_changes[-1].get("best_bid"))
                    advertised_ask = decimal_value(asset_changes[-1].get("best_ask"))
                    if (
                        (advertised_bid is not None and advertised_bid != book.best_bid)
                        or (advertised_ask is not None and advertised_ask != book.best_ask)
                    ):
                        book.ready = False
                        book.reason = "BEST_PRICE_MISMATCH"
                    else:
                        book.reason = "READY"
                    book.last_timestamp = decimal_value(timestamp) or book.last_timestamp
                    book.last_message_hash = message_hash
                    book.update_number += 1
                updates.append(book.view(
                    event_type=event_type, timestamp=timestamp, message_hash=message_hash
                ))
            return AtomicBookFrame(
                event_type, timestamp, message_hash, tuple(updates), out_of_order=out_of_order
            )

        if event_type == "best_bid_ask":
            asset = str(message.get("asset_id") or "")
            book = self.books.get(asset)
            if not book:
                return AtomicBookFrame(event_type, timestamp, message_hash, ())
            if self._is_out_of_order(book, timestamp):
                book.ready = False
                book.reason = "OUT_OF_ORDER"
                return AtomicBookFrame(event_type, timestamp, message_hash, (book.view(
                    event_type=event_type, timestamp=timestamp, message_hash=message_hash
                ),), out_of_order=True)
            advertised_bid = decimal_value(message.get("best_bid"))
            advertised_ask = decimal_value(message.get("best_ask"))
            if not book.ready:
                book.reason = "BEST_UPDATE_BEFORE_SNAPSHOT"
            elif advertised_bid != book.best_bid or advertised_ask != book.best_ask:
                book.ready = False
                book.reason = "BEST_PRICE_MISMATCH"
            book.last_timestamp = decimal_value(timestamp) or book.last_timestamp
            book.last_message_hash = message_hash
            book.update_number += 1
            return AtomicBookFrame(event_type, timestamp, message_hash, (book.view(
                event_type=event_type, timestamp=timestamp, message_hash=message_hash
            ),))

        return AtomicBookFrame(event_type, timestamp, message_hash, ())

    @staticmethod
    def _parse_levels(raw: Any) -> dict[Decimal, Decimal]:
        result: dict[Decimal, Decimal] = {}
        if not isinstance(raw, list):
            return result
        for level in raw:
            if not isinstance(level, dict):
                continue
            price = decimal_value(level.get("price"))
            size = decimal_value(level.get("size"))
            if price is not None and size is not None and price >= 0 and size > 0:
                result[price] = size
        return result

    @staticmethod
    def _is_out_of_order(book: OrderBookState, timestamp: str | None) -> bool:
        incoming = decimal_value(timestamp)
        return incoming is not None and book.last_timestamp is not None and incoming < book.last_timestamp

    def _remember_hash(self, message_hash: str) -> None:
        if len(self._recent_hashes) == self._recent_hashes.maxlen:
            expired = self._recent_hashes.popleft()
            self._recent_hash_set.discard(expired)
        self._recent_hashes.append(message_hash)
        self._recent_hash_set.add(message_hash)

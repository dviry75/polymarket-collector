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
    _best_bid_cache: Decimal | None = field(default=None, repr=False)
    _best_ask_cache: Decimal | None = field(default=None, repr=False)
    ready: bool = False
    snapshot_loaded: bool = False
    reason: str = "AWAITING_SNAPSHOT"
    # Exchange timestamps are not a total order across event types. This
    # timestamp tracks depth-bearing book/price_change messages only.
    last_timestamp: Decimal | None = None
    last_exchange_timestamp_ms: int | None = None
    last_received_at_ms: int | None = None
    receive_latency_ms: int | None = None
    last_message_hash: str = ""
    update_number: int = 0
    source_generation: int = 0
    last_wire_sequence: int = 0
    last_snapshot_timestamp_ms: int | None = None
    last_snapshot_received_at_ms: int | None = None
    last_delta_timestamp_ms: int | None = None
    last_delta_received_at_ms: int | None = None
    last_mutation_type: str = ""
    last_mutation_hash: str = ""
    advertised_best_bid: Decimal | None = None
    advertised_best_ask: Decimal | None = None
    advertised_timestamp_ms: int | None = None
    advertised_received_at_ms: int | None = None
    advertised_wire_sequence: int = 0
    advertised_source: str = ""
    advertised_message_hash: str = ""
    alignment_pending: bool = False
    alignment_correlation_id: str = ""
    alignment_started_update: int = 0
    _levels_cache: dict[str, list[dict[str, str]]] = field(
        default_factory=dict, repr=False
    )

    def clear_for_resync(self, generation: int, reason: str) -> None:
        self.generation = generation
        self.source_generation = generation
        self.bids.clear()
        self.asks.clear()
        self._best_bid_cache = None
        self._best_ask_cache = None
        self._levels_cache.clear()
        self.ready = False
        self.snapshot_loaded = False
        self.reason = reason
        self.last_timestamp = None
        self.last_exchange_timestamp_ms = None
        self.last_received_at_ms = None
        self.receive_latency_ms = None
        self.last_message_hash = ""
        self.last_wire_sequence = 0
        self.last_snapshot_timestamp_ms = None
        self.last_snapshot_received_at_ms = None
        self.last_delta_timestamp_ms = None
        self.last_delta_received_at_ms = None
        self.last_mutation_type = ""
        self.last_mutation_hash = ""
        self.advertised_best_bid = None
        self.advertised_best_ask = None
        self.advertised_timestamp_ms = None
        self.advertised_received_at_ms = None
        self.advertised_wire_sequence = 0
        self.advertised_source = ""
        self.advertised_message_hash = ""
        self.alignment_pending = False
        self.alignment_correlation_id = ""
        self.alignment_started_update = 0
        self.update_number += 1

    @property
    def best_bid(self) -> Decimal | None:
        return self._best_bid_cache

    @property
    def best_ask(self) -> Decimal | None:
        return self._best_ask_cache

    def refresh_best_prices(self) -> None:
        """Rebuild cached top-of-book after a full snapshot or structural prune."""
        self._best_bid_cache = max(self.bids) if self.bids else None
        self._best_ask_cache = min(self.asks) if self.asks else None
    def top_matches(
        self, bid: Decimal | None, ask: Decimal | None
    ) -> bool:
        return self.best_bid == bid and self.best_ask == ask

    def remember_advertised(
        self, *, bid: Decimal | None, ask: Decimal | None,
        timestamp_ms: int | None, received_at_ms: int | None,
        wire_sequence: int, source: str, message_hash: str,
    ) -> None:
        self.advertised_best_bid = bid
        self.advertised_best_ask = ask
        self.advertised_timestamp_ms = timestamp_ms
        self.advertised_received_at_ms = received_at_ms
        self.advertised_wire_sequence = wire_sequence
        self.advertised_source = source
        self.advertised_message_hash = message_hash
        self.alignment_pending = not self.top_matches(bid, ask)
        if self.alignment_pending:
            identity = (
                f"{self.generation}:{self.source_generation}:{self.asset_id}:"
                f"{wire_sequence}:{message_hash}:{bid}:{ask}"
            )
            self.alignment_correlation_id = hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest()[:24]
            self.alignment_started_update = self.update_number
        else:
            self.clear_alignment()

    def clear_alignment(self) -> None:
        self.alignment_pending = False
        self.alignment_correlation_id = ""
        self.alignment_started_update = 0

    def alignment_view(self) -> dict[str, Any]:
        return {
            "advertised_best_bid": (
                canonical_decimal(self.advertised_best_bid)
                if self.advertised_best_bid is not None else None
            ),
            "advertised_best_ask": (
                canonical_decimal(self.advertised_best_ask)
                if self.advertised_best_ask is not None else None
            ),
            "advertised_timestamp_ms": self.advertised_timestamp_ms,
            "advertised_received_at_ms": self.advertised_received_at_ms,
            "advertised_wire_sequence": self.advertised_wire_sequence,
            "advertised_source": self.advertised_source,
            "advertised_message_hash": self.advertised_message_hash,
            "alignment_pending": self.alignment_pending,
            "correlation_id": self.alignment_correlation_id,
        }

    def apply_level(
        self, *, side: str, price: Decimal, size: Decimal
    ) -> str:
        """Apply one L2 mutation while keeping top-of-book cached.

        Normal inserts/updates are O(1). A side is scanned only when the
        currently cached best level itself is removed.
        """
        if side == "BUY":
            levels = self.bids
            cache_name = "_best_bid_cache"
            changed_side = "bids"
            better = lambda candidate, current: candidate > current
            fallback = max
        elif side == "SELL":
            levels = self.asks
            cache_name = "_best_ask_cache"
            changed_side = "asks"
            better = lambda candidate, current: candidate < current
            fallback = min
        else:
            raise ValueError("invalid order-book side")

        current = getattr(self, cache_name)

        if size == 0:
            existed = price in levels
            levels.pop(price, None)

            if existed and current == price:
                setattr(
                    self,
                    cache_name,
                    fallback(levels) if levels else None,
                )
        else:
            levels[price] = size

            if current is None or better(price, current):
                setattr(self, cache_name, price)

        self.invalidate_levels(changed_side)
        return changed_side

    def levels(self, side: str) -> list[dict[str, str]]:
        cached = self._levels_cache.get(side)
        if cached is not None:
            return cached
        source = self.bids if side == "bids" else self.asks
        reverse = side == "bids"
        levels = [
            {"price": canonical_decimal(price), "size": canonical_decimal(source[price])}
            for price in sorted(source, reverse=reverse)
        ]
        self._levels_cache[side] = levels
        return levels

    def invalidate_levels(self, *sides: str) -> None:
        for side in sides:
            self._levels_cache.pop(side, None)

    def top_view(self, *, event_type: str, timestamp: str | None, message_hash: str,
                 now_ms: int | None = None) -> dict[str, Any]:
        """Lightweight view for latency-sensitive trading decisions.

        Full depth is deliberately excluded so normal market updates do not
        sort and serialize the entire order book.
        """
        best_bid = self.best_bid
        best_ask = self.best_ask
        return {
            "asset_id": self.asset_id,
            "event_type": event_type,
            "best_bid": canonical_decimal(best_bid) if best_bid is not None else None,
            "best_ask": canonical_decimal(best_ask) if best_ask is not None else None,
            "best_bid_size": canonical_decimal(self.bids[best_bid]) if best_bid is not None else None,
            "best_ask_size": canonical_decimal(self.asks[best_ask]) if best_ask is not None else None,
            "market_timestamp": timestamp,
            "exchange_timestamp_ms": self.last_exchange_timestamp_ms,
            "exchange_age_ms": (
                now_ms - self.last_exchange_timestamp_ms
                if now_ms is not None and self.last_exchange_timestamp_ms is not None else None
            ),
            "receive_latency_ms": self.receive_latency_ms,
            "book_ready": self.ready,
            "readiness_reason": self.reason,
            "generation": self.generation,
            "update_number": self.update_number,
            "message_hash": message_hash,
            "source_generation": self.source_generation,
            "wire_sequence": self.last_wire_sequence,
            "last_snapshot_timestamp_ms": self.last_snapshot_timestamp_ms,
            "last_snapshot_received_at_ms": self.last_snapshot_received_at_ms,
            "last_delta_timestamp_ms": self.last_delta_timestamp_ms,
            "last_delta_received_at_ms": self.last_delta_received_at_ms,
            "last_mutation_type": self.last_mutation_type,
            "last_mutation_hash": self.last_mutation_hash,
            **self.alignment_view(),
        }

    def view(self, *, event_type: str, timestamp: str | None, message_hash: str,
             now_ms: int | None = None) -> dict[str, Any]:
        """Full-depth view for persistence, diagnostics and paper execution."""
        result = self.top_view(
            event_type=event_type,
            timestamp=timestamp,
            message_hash=message_hash,
            now_ms=now_ms,
        )
        result["bids"] = self.levels("bids")
        result["asks"] = self.levels("asks")
        return result


@dataclass(frozen=True)
class AtomicBookFrame:
    event_type: str
    timestamp: str | None
    message_hash: str
    updates: tuple[dict[str, Any], ...]
    duplicate: bool = False
    out_of_order: bool = False
    rejected_reason: str = ""
    exchange_timestamp_ms: int | None = None
    exchange_age_ms: int | None = None
    receive_latency_ms: int | None = None

    # Ordered intermediate top-of-book states observed while applying one
    # raw price_change message. These are strategy-only observations; the
    # normal updates tuple continues to represent the final atomic book.
    top_transitions: tuple[dict[str, Any], ...] = ()

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
        self.source_generation = 0
        self._wire_sequence = 0
        self.books = {str(asset): OrderBookState(str(asset), self.generation) for asset in asset_ids}
        for book in self.books.values():
            book.source_generation = self.source_generation
        self._recent_hashes: deque[str] = deque(maxlen=4096)
        self._recent_hash_set: set[str] = set()

    def ensure_assets(self, asset_ids: Iterable[str]) -> None:
        wanted = {str(asset) for asset in asset_ids if asset}
        for asset in wanted:
            if asset not in self.books:
                book = OrderBookState(asset, self.generation)
                book.source_generation = self.source_generation
                self.books[asset] = book
        for asset in list(self.books):
            if asset not in wanted:
                del self.books[asset]

    def mark_not_ready(
        self, reason: str, *, source_generation: int | None = None
    ) -> None:
        self.generation += 1
        if source_generation is not None:
            self.source_generation = int(source_generation)
        self._recent_hashes.clear()
        self._recent_hash_set.clear()
        for book in self.books.values():
            book.clear_for_resync(self.generation, reason)
            book.source_generation = self.source_generation

    def event_ready(
        self, asset_ids: Iterable[str], *, now_ms: int | None = None,
        max_age_ms: int | None = None, future_tolerance_ms: int = 0,
    ) -> tuple[bool, str]:
        assets = [str(asset) for asset in asset_ids if asset]
        if not assets:
            return False, "NO_ASSETS"
        for asset in assets:
            book = self.books.get(asset)
            if book is None or not book.ready:
                return False, book.reason if book else "UNKNOWN_ASSET"
            if book.generation != self.generation:
                return False, "GENERATION_MISMATCH"
            if book.source_generation != self.source_generation:
                return False, "STALE_CONNECTION_GENERATION"
            # Freshness is decided when each message arrives in apply(). The
            # stored timestamp marks the last book change; it is not a lease
            # that expires while a connected, unchanged book is idle.
            if book.last_exchange_timestamp_ms is None:
                return False, "MISSING_EXCHANGE_TIMESTAMP"
        return True, "READY"

    @staticmethod
    def _render_book(
        book: OrderBookState, *, include_depth: bool,
        event_type: str, timestamp: str | None,
        message_hash: str, now_ms: int | None = None,
    ) -> dict[str, Any]:
        renderer = book.view if include_depth else book.top_view
        return renderer(
            event_type=event_type,
            timestamp=timestamp,
            message_hash=message_hash,
            now_ms=now_ms,
        )
    @staticmethod
    def _record_depth_mutation(
        book: OrderBookState, *, event_type: str, timestamp: str | None,
        exchange_ms: int | None, received_at_ms: int | None,
        receive_latency_ms: int | None, message_hash: str, wire_sequence: int,
    ) -> None:
        incoming = decimal_value(timestamp)
        if incoming is not None:
            book.last_timestamp = incoming
        if exchange_ms is not None:
            book.last_exchange_timestamp_ms = exchange_ms
        book.last_received_at_ms = received_at_ms
        book.receive_latency_ms = receive_latency_ms
        book.last_message_hash = message_hash
        book.last_wire_sequence = wire_sequence
        book.last_mutation_type = event_type
        book.last_mutation_hash = message_hash
        if event_type == "book":
            book.last_snapshot_timestamp_ms = exchange_ms
            book.last_snapshot_received_at_ms = received_at_ms
        elif event_type == "price_change":
            book.last_delta_timestamp_ms = exchange_ms
            book.last_delta_received_at_ms = received_at_ms
        book.update_number += 1

    @staticmethod
    def _resolve_after_depth(
        book: OrderBookState, *, event_type: str,
        exchange_ms: int | None, received_at_ms: int | None,
        wire_sequence: int, message_hash: str,
        advertised_bid: Decimal | None = None,
        advertised_ask: Decimal | None = None,
        advertised_present: bool = False,
    ) -> str:
        if book.alignment_pending:
            if book.top_matches(
                book.advertised_best_bid, book.advertised_best_ask
            ):
                book.clear_alignment()
                book.ready = True
                book.reason = "READY"
            elif (
                exchange_ms is not None
                and book.advertised_timestamp_ms is not None
                and (
                    (
                        event_type == "book"
                        and exchange_ms >= book.advertised_timestamp_ms
                    )
                    or (
                        event_type == "price_change"
                        and exchange_ms > book.advertised_timestamp_ms
                    )
                )
            ):
                # A full snapshot at/after the advertised target is a semantic
                # barrier. A later depth delta is also a barrier: the earlier
                # advertised transition can no longer legitimately be pending.
                book.ready = False
                book.reason = "BEST_PRICE_MISMATCH"
            else:
                book.ready = False
                book.reason = "BEST_PRICE_PENDING_DEPTH"
            return book.reason

        if advertised_present:
            book.remember_advertised(
                bid=advertised_bid, ask=advertised_ask,
                timestamp_ms=exchange_ms, received_at_ms=received_at_ms,
                wire_sequence=wire_sequence, source=event_type,
                message_hash=message_hash,
            )
            if book.alignment_pending:
                # price_change carries the advertised top for the transaction,
                # but Polymarket may split the depth mutations that realize it
                # across consecutive same-timestamp messages. Keep the book
                # fail-closed until those companion deltas arrive. A later
                # timestamp or authoritative snapshot is the barrier above.
                book.ready = False
                book.reason = "BEST_PRICE_PENDING_DEPTH"
                return book.reason

        book.ready = True
        book.reason = "READY"
        return book.reason


    def apply(
        self, message: dict[str, Any], *, now_ms: int | None = None,
        max_age_ms: int | None = None, future_tolerance_ms: int = 0,
        include_depth: bool = True, source_generation: int | None = None,
        wire_sequence: int | None = None,
    ) -> AtomicBookFrame:
        event_type = str(message.get("event_type") or message.get("type") or "").lower()
        identity = json.dumps(message, sort_keys=True, separators=(",", ":"), default=str)
        message_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        timestamp_raw = message.get("timestamp")
        timestamp = str(timestamp_raw) if timestamp_raw not in (None, "") else None
        if wire_sequence is None:
            self._wire_sequence += 1
            sequence = self._wire_sequence
        else:
            sequence = int(wire_sequence)
            self._wire_sequence = max(self._wire_sequence, sequence)
        incoming_generation = (
            self.source_generation
            if source_generation is None else int(source_generation)
        )
        if incoming_generation != self.source_generation:
            return AtomicBookFrame(
                event_type, timestamp, message_hash, (),
                rejected_reason="STALE_CONNECTION_GENERATION",
            )
        if message_hash in self._recent_hash_set:
            return AtomicBookFrame(event_type, timestamp, message_hash, (), duplicate=True)
        self._remember_hash(message_hash)

        exchange_ms, timestamp_error = self._exchange_timestamp_ms(timestamp)
        exchange_age_ms = (
            int(now_ms) - exchange_ms
            if now_ms is not None and exchange_ms is not None else None
        )
        structural_only = False
        if max_age_ms is not None:
            rejected_reason = timestamp_error
            if not rejected_reason and exchange_age_ms is not None:
                if exchange_age_ms < -future_tolerance_ms:
                    rejected_reason = "FUTURE_EXCHANGE_TIMESTAMP"
                elif exchange_age_ms > max_age_ms:
                    rejected_reason = "STALE_EXCHANGE_TIMESTAMP"
            if rejected_reason:
                # An initial CLOB `book` event is an authoritative structural
                # snapshot even when its exchange timestamp reflects an older
                # last change. Load it only as an unready base. It cannot reach
                # strategy, and a stale snapshot can never replace an existing
                # generation snapshot.
                if rejected_reason == "STALE_EXCHANGE_TIMESTAMP" and event_type == "book":
                    asset = str(message.get("asset_id") or "")
                    book = self.books.get(asset)
                    if book is not None and not book.snapshot_loaded:
                        book.bids = self._parse_levels(message.get("bids"))
                        book.asks = self._parse_levels(message.get("asks"))
                        book.refresh_best_prices()
                        book.invalidate_levels("bids", "asks")
                        book.snapshot_loaded = True
                        book.ready = False
                        book.reason = rejected_reason
                        book.last_timestamp = decimal_value(timestamp)
                        book.last_exchange_timestamp_ms = exchange_ms
                        book.last_received_at_ms = now_ms
                        book.receive_latency_ms = exchange_age_ms
                        book.last_message_hash = message_hash
                        book.last_wire_sequence = sequence
                        book.last_snapshot_timestamp_ms = exchange_ms
                        book.last_snapshot_received_at_ms = now_ms
                        book.last_mutation_type = event_type
                        book.last_mutation_hash = message_hash
                        book.update_number += 1
                        return AtomicBookFrame(
                            event_type, timestamp, message_hash, (),
                            rejected_reason=rejected_reason,
                            exchange_timestamp_ms=exchange_ms,
                            exchange_age_ms=exchange_age_ms,
                            receive_latency_ms=exchange_age_ms,
                        )
                if rejected_reason == "STALE_EXCHANGE_TIMESTAMP" and event_type in {
                    "price_change", "best_bid_ask",
                }:
                    structural_only = True
                else:
                    return AtomicBookFrame(
                        event_type, timestamp, message_hash, (),
                        rejected_reason=rejected_reason,
                        exchange_timestamp_ms=exchange_ms,
                        exchange_age_ms=exchange_age_ms,
                        receive_latency_ms=exchange_age_ms,
                    )

        if event_type == "book":
            asset = str(message.get("asset_id") or "")
            book = self.books.get(asset)
            if not book:
                return AtomicBookFrame(event_type, timestamp, message_hash, ())
            if self._is_out_of_order(book, timestamp):
                return AtomicBookFrame(
                    event_type, timestamp, message_hash, (), out_of_order=True,
                    rejected_reason="OUT_OF_ORDER_EXCHANGE_TIMESTAMP",
                    exchange_timestamp_ms=exchange_ms, exchange_age_ms=exchange_age_ms,
                    receive_latency_ms=exchange_age_ms,
                )
            bids = self._parse_levels(message.get("bids"))
            asks = self._parse_levels(message.get("asks"))
            book.bids = bids
            book.asks = asks
            book.refresh_best_prices()
            book.invalidate_levels("bids", "asks")
            book.snapshot_loaded = True
            self._record_depth_mutation(
                book, event_type=event_type, timestamp=timestamp,
                exchange_ms=exchange_ms, received_at_ms=now_ms,
                receive_latency_ms=exchange_age_ms, message_hash=message_hash,
                wire_sequence=sequence,
            )
            self._resolve_after_depth(
                book, event_type=event_type, exchange_ms=exchange_ms,
                received_at_ms=now_ms, wire_sequence=sequence,
                message_hash=message_hash,
            )
            return AtomicBookFrame(
                event_type, timestamp, message_hash, (self._render_book(book, include_depth=include_depth,
                    event_type=event_type, timestamp=timestamp,
                    message_hash=message_hash, now_ms=now_ms
                ),),
                exchange_timestamp_ms=exchange_ms,
                exchange_age_ms=exchange_age_ms,
                receive_latency_ms=exchange_age_ms,
            )

        if event_type == "price_change":
            changes = message.get("price_changes")
            if not isinstance(changes, list):
                changes = []
            grouped: dict[
                str,
                list[tuple[int, dict[str, Any]]],
            ] = {}

            for raw_index, change in enumerate(changes):
                if not isinstance(change, dict):
                    continue

                asset = str(
                    change.get("asset_id")
                    or message.get("asset_id")
                    or ""
                )

                if asset in self.books:
                    grouped.setdefault(
                        asset,
                        [],
                    ).append(
                        (raw_index, change)
                    )

            updates: list[dict[str, Any]] = []
            top_transitions: list[dict[str, Any]] = []
            out_of_order = False

            for asset, indexed_changes in grouped.items():
                asset_changes = [
                    change
                    for _index, change in indexed_changes
                ]
                book = self.books[asset]
                if self._is_out_of_order(book, timestamp):
                    out_of_order = True
                    continue
                if not book.snapshot_loaded:
                    book.reason = "DELTA_BEFORE_SNAPSHOT"
                    updates.append(self._render_book(book, include_depth=include_depth,
                        event_type=event_type, timestamp=timestamp, message_hash=message_hash, now_ms=now_ms
                    ))
                    continue
                if not book.ready and book.reason not in {
                    "STALE_EXCHANGE_TIMESTAMP", "BEST_PRICE_PENDING_DEPTH",
                }:
                    updates.append(self._render_book(book, include_depth=include_depth,
                        event_type=event_type, timestamp=timestamp,
                        message_hash=message_hash, now_ms=now_ms,
                    ))
                    continue
                malformed = False
                changed_sides: set[str] = set()
                asset_top_transitions: list[dict[str, Any]] = []

                previous_observed_bid = book.best_bid
                previous_observed_ask = book.best_ask

                for raw_index, change in indexed_changes:
                    side = str(
                        change.get("side") or ""
                    ).upper()
                    price = decimal_value(
                        change.get("price")
                    )
                    size = decimal_value(
                        change.get("size")
                    )

                    if (
                        side not in {"BUY", "SELL"}
                        or price is None
                        or size is None
                        or price < 0
                        or size < 0
                    ):
                        malformed = True
                        break

                    changed_sides.add(
                        book.apply_level(
                            side=side,
                            price=price,
                            size=size,
                        )
                    )

                    raw_bid_present = (
                        change.get("best_bid")
                        not in (None, "")
                    )
                    raw_ask_present = (
                        change.get("best_ask")
                        not in (None, "")
                    )

                    advertised_bid_now = (
                        self._advertised_best(
                            change.get("best_bid"),
                            side="bid",
                        )
                        if raw_bid_present
                        else None
                    )

                    advertised_ask_now = (
                        self._advertised_best(
                            change.get("best_ask"),
                            side="ask",
                        )
                        if raw_ask_present
                        else None
                    )

                    observed_bid = (
                        advertised_bid_now
                        if raw_bid_present
                        else book.best_bid
                    )

                    observed_ask = (
                        advertised_ask_now
                        if raw_ask_present
                        else book.best_ask
                    )

                    if (
                        observed_bid != previous_observed_bid
                        or observed_ask != previous_observed_ask
                    ):
                        bid_size = (
                            book.bids.get(observed_bid)
                            if observed_bid is not None
                            else None
                        )
                        ask_size = (
                            book.asks.get(observed_ask)
                            if observed_ask is not None
                            else None
                        )

                        asset_top_transitions.append({
                            "asset_id": asset,
                            "event_type": event_type,
                            "best_bid": (
                                canonical_decimal(observed_bid)
                                if observed_bid is not None
                                else None
                            ),
                            "best_ask": (
                                canonical_decimal(observed_ask)
                                if observed_ask is not None
                                else None
                            ),
                            "best_bid_size": (
                                canonical_decimal(bid_size)
                                if bid_size is not None
                                else None
                            ),
                            "best_ask_size": (
                                canonical_decimal(ask_size)
                                if ask_size is not None
                                else None
                            ),
                            "market_timestamp": timestamp,
                            "exchange_timestamp_ms": exchange_ms,
                            "exchange_age_ms": exchange_age_ms,
                            "receive_latency_ms": exchange_age_ms,
                            "book_ready": True,
                            "readiness_reason": "READY",
                            "generation": book.generation,
                            "update_number": book.update_number + 1,
                            "message_hash": message_hash,
                            "source_generation": book.source_generation,
                            "wire_sequence": sequence,
                            "correlation_id": (
                                book.alignment_correlation_id
                                or hashlib.sha256(
                                    f"{book.generation}:{asset}:{sequence}:"
                                    f"{message_hash}:{raw_index}".encode("utf-8")
                                ).hexdigest()[:24]
                            ),
                            "_raw_change_index": raw_index,
                            "_intermediate_top_transition": True,
                        })

                    previous_observed_bid = observed_bid
                    previous_observed_ask = observed_ask
                if malformed:
                    book.ready = False
                    book.reason = "MALFORMED_DELTA"
                else:
                    advertised_bid = self._advertised_best(
                        asset_changes[-1].get("best_bid"), side="bid"
                    )
                    advertised_ask = self._advertised_best(
                        asset_changes[-1].get("best_ask"), side="ask"
                    )
                    advertised_present = any(
                        asset_changes[-1].get(key) not in (None, "")
                        for key in ("best_bid", "best_ask")
                    )
                    self._record_depth_mutation(
                        book, event_type=event_type, timestamp=timestamp,
                        exchange_ms=exchange_ms, received_at_ms=now_ms,
                        receive_latency_ms=exchange_age_ms,
                        message_hash=message_hash, wire_sequence=sequence,
                    )
                    self._resolve_after_depth(
                        book, event_type=event_type, exchange_ms=exchange_ms,
                        received_at_ms=now_ms, wire_sequence=sequence,
                        message_hash=message_hash, advertised_bid=advertised_bid,
                        advertised_ask=advertised_ask,
                        advertised_present=advertised_present,

                    )
                    # Preserve the ordered, server-advertised top transitions
                    # even when the final reconstructed depth fails integrity.
                    # Event readiness remains NOT_READY, so these observations
                    # can only reach a fail-closed terminal strategy decision.
                    top_transitions.extend(
                        asset_top_transitions
                    )

                updates.append(self._render_book(book, include_depth=include_depth,
                    event_type=event_type, timestamp=timestamp, message_hash=message_hash, now_ms=now_ms
                ))
            if structural_only:
                for asset in grouped:
                    book = self.books[asset]
                    if book.reason == "READY":
                        book.ready = False
                        book.reason = "STALE_EXCHANGE_TIMESTAMP"
                return AtomicBookFrame(
                    event_type, timestamp, message_hash, (),
                    rejected_reason="STALE_EXCHANGE_TIMESTAMP",
                    exchange_timestamp_ms=exchange_ms,
                    exchange_age_ms=exchange_age_ms,
                    receive_latency_ms=exchange_age_ms,
                )
            return AtomicBookFrame(
                event_type,
                timestamp,
                message_hash,
                tuple(updates),
                out_of_order=out_of_order,
                rejected_reason=(
                    "OUT_OF_ORDER_EXCHANGE_TIMESTAMP"
                    if out_of_order and not updates
                    else ""
                ),
                exchange_timestamp_ms=exchange_ms,
                exchange_age_ms=exchange_age_ms,
                receive_latency_ms=exchange_age_ms,
                top_transitions=tuple(
                    sorted(
                        top_transitions,
                        key=lambda item: int(
                            item.get(
                                "_raw_change_index",
                                -1,
                            )
                        ),
                    )
                ),
            )

        if event_type == "best_bid_ask":
            asset = str(message.get("asset_id") or "")
            book = self.books.get(asset)
            if not book:
                return AtomicBookFrame(event_type, timestamp, message_hash, ())
            advertised_bid = self._advertised_best(
                message.get("best_bid"), side="bid"
            )
            advertised_ask = self._advertised_best(
                message.get("best_ask"), side="ask"
            )
            book.remember_advertised(
                bid=advertised_bid, ask=advertised_ask,
                timestamp_ms=exchange_ms, received_at_ms=now_ms,
                wire_sequence=sequence, source=event_type,
                message_hash=message_hash,
            )
            book.last_wire_sequence = sequence
            book.last_received_at_ms = now_ms
            book.receive_latency_ms = exchange_age_ms
            book.last_message_hash = message_hash
            if exchange_ms is not None:
                book.last_exchange_timestamp_ms = max(
                    exchange_ms, book.last_exchange_timestamp_ms or exchange_ms
                )
            book.update_number += 1
            if not book.snapshot_loaded:
                book.ready = False
                book.reason = "BEST_PRICE_PENDING_DEPTH"
            elif book.alignment_pending:
                book.ready = False
                book.reason = "BEST_PRICE_PENDING_DEPTH"
            else:
                book.ready = True
                book.reason = "READY"

            transition = {
                "asset_id": asset,
                "event_type": event_type,
                "best_bid": (
                    canonical_decimal(advertised_bid)
                    if advertised_bid is not None else None
                ),
                "best_ask": (
                    canonical_decimal(advertised_ask)
                    if advertised_ask is not None else None
                ),
                "best_bid_size": (
                    canonical_decimal(book.bids[advertised_bid])
                    if advertised_bid in book.bids else None
                ),
                "best_ask_size": (
                    canonical_decimal(book.asks[advertised_ask])
                    if advertised_ask in book.asks else None
                ),
                "market_timestamp": timestamp,
                "exchange_timestamp_ms": exchange_ms,
                "exchange_age_ms": exchange_age_ms,
                "receive_latency_ms": exchange_age_ms,
                "book_ready": book.ready,
                "readiness_reason": book.reason,
                "generation": book.generation,
                "source_generation": book.source_generation,
                "wire_sequence": sequence,
                "update_number": book.update_number,
                "message_hash": message_hash,
                "correlation_id": book.alignment_correlation_id,
                "_advertised_top_transition": True,
            }
            if structural_only:
                book.ready = False
                book.reason = "STALE_EXCHANGE_TIMESTAMP"
                return AtomicBookFrame(
                    event_type, timestamp, message_hash, (),
                    rejected_reason="STALE_EXCHANGE_TIMESTAMP",
                    exchange_timestamp_ms=exchange_ms,
                    exchange_age_ms=exchange_age_ms,
                    receive_latency_ms=exchange_age_ms,
                )
            return AtomicBookFrame(
                event_type, timestamp, message_hash,
                (self._render_book(
                    book, include_depth=include_depth,
                    event_type=event_type, timestamp=timestamp,
                    message_hash=message_hash, now_ms=now_ms,
                ),),
                exchange_timestamp_ms=exchange_ms,
                exchange_age_ms=exchange_age_ms,
                receive_latency_ms=exchange_age_ms,
                top_transitions=(transition,),
            )

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
    def _advertised_best(value: Any, *, side: str) -> Decimal | None:
        """Normalize CLOB empty-side sentinels before integrity comparison."""
        parsed = decimal_value(value)
        if parsed is None:
            return None
        if (side == "bid" and parsed == 0) or (side == "ask" and parsed == 1):
            return None
        return parsed

    @staticmethod
    def _is_out_of_order(book: OrderBookState, timestamp: str | None) -> bool:
        incoming = decimal_value(timestamp)
        return incoming is not None and book.last_timestamp is not None and incoming < book.last_timestamp

    @staticmethod
    def _exchange_timestamp_ms(timestamp: str | None) -> tuple[int | None, str]:
        if timestamp is None:
            return None, "MISSING_EXCHANGE_TIMESTAMP"
        value = decimal_value(timestamp)
        if value is None or value <= 0:
            return None, "INVALID_EXCHANGE_TIMESTAMP"
        if value < Decimal("10000000000"):
            value *= 1000
        if value >= Decimal("100000000000000"):
            return None, "INVALID_EXCHANGE_TIMESTAMP"
        return int(value), ""

    def _remember_hash(self, message_hash: str) -> None:
        if len(self._recent_hashes) == self._recent_hashes.maxlen:
            expired = self._recent_hashes.popleft()
            self._recent_hash_set.discard(expired)
        self._recent_hashes.append(message_hash)
        self._recent_hash_set.add(message_hash)

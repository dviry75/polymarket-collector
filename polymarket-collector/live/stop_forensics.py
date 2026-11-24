"""Bounded, off-path forensic capture for STOP exit episodes.

The strategy thread only builds an immutable, bounded record and attempts a
non-blocking queue put.  JSON encoding and SQLite I/O happen on a dedicated
writer thread against a database that is deliberately separate from the live
state database.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


LOGGER = logging.getLogger(__name__)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def bounded_ladder(
    levels: Iterable[Any],
    *,
    target_shares: Decimal,
    buffer_multiple: Decimal = Decimal("1.25"),
    max_levels: int = 32,
) -> tuple[tuple[str, str], ...]:
    """Copy enough levels for a buffered fill simulation, with a hard cap."""

    target = max(Decimal("0"), target_shares) * max(
        Decimal("1"), buffer_multiple
    )
    cumulative = Decimal("0")
    copied: list[tuple[str, str]] = []
    for raw in levels:
        if len(copied) >= max(1, max_levels):
            break
        if isinstance(raw, Mapping):
            price_raw = raw.get("price")
            size_raw = raw.get("size")
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            price_raw = raw[0] if len(raw) > 0 else None
            size_raw = raw[1] if len(raw) > 1 else None
        else:
            continue
        price = _decimal(price_raw)
        size = _decimal(size_raw)
        if price is None or size is None or size <= 0:
            continue
        copied.append((_text(price), _text(size)))
        cumulative += size
        if target > 0 and cumulative >= target:
            break
    return tuple(copied)


def _timing_tuple(timing: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    if not timing:
        return ()
    allowed = (
        "socket_receive_wall_ms",
        "receive_wall_ns",
        "recv_return_monotonic",
        "ingress_enqueued_monotonic",
        "ingress_dequeued_monotonic",
        "processing_wall_ns",
        "processing_monotonic_ns",
        "book_update_monotonic",
        "book_applied_monotonic",
        "strategy_scheduled_monotonic",
        "exchange_timestamp",
    )
    return tuple((key, _text(timing.get(key))) for key in allowed if key in timing)


@dataclass(frozen=True, slots=True)
class StopForensicRecord:
    stage: str
    episode_id: str
    position_id: str
    event_id: str
    condition_id: str
    token_id: str
    intent_id: str
    purpose: str
    captured_wall_ns: int
    captured_monotonic_ns: int
    capture_cost_ns: int
    exchange_event_ts: str
    generation: str
    update_number: str
    frame_hash: str
    book_hash: str
    event_ready: str
    integrity_status: str
    reconciliation_readiness: str
    best_bid: str
    best_bid_size: str
    best_ask: str
    best_ask_size: str
    remaining_shares: str
    sellable_shares: str
    requested_shares: str
    min_price: str
    critical_queue_depth: int
    stop_queue_depth: int
    bids: tuple[tuple[str, str], ...]
    asks: tuple[tuple[str, str], ...]
    timing: tuple[tuple[str, str], ...]
    result_status: str
    remote_order_id: str

    @classmethod
    def capture(
        cls,
        *,
        stage: str,
        episode_id: str,
        position: Mapping[str, Any],
        update: Mapping[str, Any],
        timing: Mapping[str, Any] | None = None,
        intent_id: str = "",
        purpose: str = "",
        frame_hash: str = "",
        book_hash: str = "",
        event_ready: Any = "",
        integrity_status: Any = "",
        reconciliation_readiness: Any = "",
        critical_queue_depth: int = 0,
        stop_queue_depth: int = 0,
        requested_shares: Any = "",
        min_price: Any = "",
        result_status: str = "",
        remote_order_id: str = "",
        ladder_buffer_multiple: Decimal = Decimal("1.25"),
        max_ladder_levels: int = 32,
    ) -> "StopForensicRecord":
        started = time.perf_counter_ns()
        remaining = _decimal(position.get("remaining_shares_text")) or Decimal("0")
        requested = _decimal(requested_shares) or remaining
        bids = bounded_ladder(
            update.get("bids") or (),
            target_shares=requested,
            buffer_multiple=ladder_buffer_multiple,
            max_levels=max_ladder_levels,
        )
        asks = bounded_ladder(
            update.get("asks") or (),
            target_shares=requested,
            buffer_multiple=ladder_buffer_multiple,
            max_levels=max_ladder_levels,
        )
        captured_monotonic_ns = time.perf_counter_ns()
        captured_wall_ns = time.time_ns()
        return cls(
            stage=stage,
            episode_id=episode_id,
            position_id=_text(position.get("position_id")),
            event_id=_text(position.get("event_id") or update.get("event_id")),
            condition_id=_text(
                position.get("condition_id") or update.get("condition_id")
            ),
            token_id=_text(position.get("token_id") or update.get("asset_id")),
            intent_id=_text(intent_id),
            purpose=_text(purpose),
            captured_wall_ns=captured_wall_ns,
            captured_monotonic_ns=captured_monotonic_ns,
            capture_cost_ns=captured_monotonic_ns - started,
            exchange_event_ts=_text(
                update.get("timestamp")
                or update.get("exchange_timestamp")
                or (timing or {}).get("exchange_timestamp")
            ),
            generation=_text(update.get("generation")),
            update_number=_text(
                update.get("update_number") or update.get("sequence")
            ),
            frame_hash=_text(frame_hash or update.get("message_hash")),
            book_hash=_text(book_hash or update.get("book_hash")),
            event_ready=_text(event_ready),
            integrity_status=_text(integrity_status),
            reconciliation_readiness=_text(reconciliation_readiness),
            best_bid=_text(update.get("best_bid")),
            best_bid_size=_text(update.get("best_bid_size")),
            best_ask=_text(update.get("best_ask")),
            best_ask_size=_text(update.get("best_ask_size")),
            remaining_shares=_text(position.get("remaining_shares_text")),
            sellable_shares=_text(position.get("sellable_shares_text")),
            requested_shares=_text(requested_shares),
            min_price=_text(min_price),
            critical_queue_depth=max(0, int(critical_queue_depth)),
            stop_queue_depth=max(0, int(stop_queue_depth)),
            bids=bids,
            asks=asks,
            timing=_timing_tuple(timing),
            result_status=_text(result_status),
            remote_order_id=_text(remote_order_id),
        )


def _simulate_sell(
    bids: tuple[tuple[str, str], ...], requested_text: str, min_price_text: str
) -> dict[str, str]:
    requested = _decimal(requested_text) or Decimal("0")
    min_price = _decimal(min_price_text) or Decimal("0")
    remaining = requested
    filled = Decimal("0")
    proceeds = Decimal("0")
    worst: Decimal | None = None
    for price_text, size_text in bids:
        price = _decimal(price_text)
        size = _decimal(size_text)
        if price is None or size is None or price < min_price or remaining <= 0:
            continue
        take = min(size, remaining)
        filled += take
        proceeds += take * price
        remaining -= take
        worst = price
    return {
        "simulated_filled_shares": _text(filled),
        "simulated_remaining_shares": _text(max(Decimal("0"), remaining)),
        "simulated_vwap": _text(proceeds / filled if filled > 0 else Decimal("0")),
        "simulated_worst_price": _text(worst),
    }


class StopForensicWriter:
    """Dedicated best-effort writer; failures are observable but never fatal."""

    _SENTINEL = object()

    def __init__(
        self,
        db_path: str,
        *,
        queue_capacity: int = 512,
        batch_size: int = 64,
        flush_interval_seconds: float = 0.1,
    ) -> None:
        self.db_path = str(db_path)
        self._queue: queue.Queue[StopForensicRecord | object] = queue.Queue(
            maxsize=max(1, queue_capacity)
        )
        self._batch_size = max(1, batch_size)
        self._flush_interval_seconds = max(0.01, flush_interval_seconds)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.enqueued = 0
        self.persisted = 0
        self.dropped = 0
        self.failed_batches = 0
        self.max_queue_depth = 0
        self.capture_cost_ns_total = 0
        self.capture_cost_ns_max = 0
        self.last_error = ""

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="stop-forensic-writer",
                daemon=True,
            )
            self._thread.start()

    def publish(self, record: StopForensicRecord) -> bool:
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return False
        depth = self._queue.qsize()
        with self._lock:
            self.enqueued += 1
            self.max_queue_depth = max(self.max_queue_depth, depth)
            self.capture_cost_ns_total += record.capture_cost_ns
            self.capture_cost_ns_max = max(
                self.capture_cost_ns_max, record.capture_cost_ns
            )
        return True

    def stop(self, timeout: float = 2.0) -> bool:
        self._stop.set()
        try:
            self._queue.put_nowait(self._SENTINEL)
        except queue.Full:
            pass
        thread = self._thread
        if thread:
            thread.join(timeout=max(0.0, timeout))
        return not bool(thread and thread.is_alive())

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            enqueued = self.enqueued
            total = self.capture_cost_ns_total
            return {
                "enqueued": enqueued,
                "persisted": self.persisted,
                "dropped": self.dropped,
                "failed_batches": self.failed_batches,
                "queue_depth": self._queue.qsize(),
                "max_queue_depth": self.max_queue_depth,
                "capture_cost_ns_avg": total // enqueued if enqueued else 0,
                "capture_cost_ns_max": self.capture_cost_ns_max,
                "writer_alive": bool(self._thread and self._thread.is_alive()),
                "last_error": self.last_error,
            }

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=1.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stop_forensic_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stage TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                position_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                captured_wall_ns INTEGER NOT NULL,
                captured_monotonic_ns INTEGER NOT NULL,
                capture_cost_ns INTEGER NOT NULL,
                best_bid TEXT NOT NULL,
                remaining_shares TEXT NOT NULL,
                requested_shares TEXT NOT NULL,
                result_status TEXT NOT NULL,
                simulated_filled_shares TEXT NOT NULL,
                simulated_vwap TEXT NOT NULL,
                simulated_worst_price TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_stop_forensics_episode "
            "ON stop_forensic_records(episode_id, captured_monotonic_ns)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_stop_forensics_stage "
            "ON stop_forensic_records(stage, captured_wall_ns)"
        )
        connection.commit()
        return connection

    def _run(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            while True:
                batch: list[StopForensicRecord] = []
                try:
                    first = self._queue.get(timeout=self._flush_interval_seconds)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                if first is self._SENTINEL:
                    if self._queue.empty():
                        break
                else:
                    batch.append(first)  # type: ignore[arg-type]
                while len(batch) < self._batch_size:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is self._SENTINEL:
                        continue
                    batch.append(item)  # type: ignore[arg-type]
                if batch:
                    self._persist_batch(connection, batch)
                if self._stop.is_set() and self._queue.empty():
                    break
        except Exception as exc:  # telemetry must not affect trading
            with self._lock:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.failed_batches += 1
            LOGGER.exception("STOP forensic writer disabled after failure")
        finally:
            if connection is not None:
                connection.close()

    def _persist_batch(
        self, connection: sqlite3.Connection, records: list[StopForensicRecord]
    ) -> None:
        rows: list[tuple[Any, ...]] = []
        for record in records:
            simulation = _simulate_sell(
                record.bids, record.requested_shares, record.min_price
            )
            payload = asdict(record)
            payload.update(simulation)
            rows.append(
                (
                    record.stage,
                    record.episode_id,
                    record.position_id,
                    record.event_id,
                    record.condition_id,
                    record.token_id,
                    record.intent_id,
                    record.captured_wall_ns,
                    record.captured_monotonic_ns,
                    record.capture_cost_ns,
                    record.best_bid,
                    record.remaining_shares,
                    record.requested_shares,
                    record.result_status,
                    simulation["simulated_filled_shares"],
                    simulation["simulated_vwap"],
                    simulation["simulated_worst_price"],
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                )
            )
        connection.executemany(
            """
            INSERT INTO stop_forensic_records (
                stage, episode_id, position_id, event_id, condition_id,
                token_id, intent_id, captured_wall_ns, captured_monotonic_ns,
                capture_cost_ns, best_bid, remaining_shares, requested_shares,
                result_status, simulated_filled_shares, simulated_vwap,
                simulated_worst_price, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.commit()
        with self._lock:
            self.persisted += len(rows)

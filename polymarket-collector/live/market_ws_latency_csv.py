from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any


CSV_FIELDS = (
    "sample_number", "connection_id", "reconnect_generation",
    "websocket_connected", "message_type", "event_id", "market_id",
    "token_id", "side", "raw_exchange_timestamp",
    "detected_timestamp_unit", "normalized_exchange_timestamp_utc",
    "receive_timestamp_utc", "processing_timestamp_utc",
    "transport_latency_ms", "queue_wait_ms", "total_age_at_processing_ms",
    "frame_size_bytes", "batch_size", "item_index_in_batch", "queue_depth",
    "best_bid", "best_ask", "readiness", "stale_classification",
    "exact_block_reason", "occurred_after_reconnect", "notes",
)


def utc_iso_from_ns(value_ns: int) -> str:
    return datetime.fromtimestamp(value_ns / 1_000_000_000, timezone.utc).isoformat()


def raw_timestamp_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, separators=(",", ":"), ensure_ascii=True)


def normalize_exchange_timestamp(
    raw: Any, *, receive_wall_ns: int,
) -> tuple[str, str, str, int | None, str]:
    """Detect epoch unit by plausibility against receipt time, not magnitude alone."""
    raw_text = raw_timestamp_text(raw)
    if raw in (None, ""):
        return raw_text, "missing", "", None, "timestamp_missing"
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            value_ns = int(parsed.timestamp() * 1_000_000_000)
            return raw_text, "iso8601", utc_iso_from_ns(value_ns), value_ns, "unit_validated=iso8601_parse"
        except (OverflowError, TypeError, ValueError):
            return raw_text, "other_invalid", "", None, "timestamp_unparseable"
    if not value.is_finite() or value <= 0:
        return raw_text, "other_invalid", "", None, "timestamp_nonpositive"

    candidates = (
        ("unix_seconds", Decimal("1000000000")),
        ("unix_milliseconds", Decimal("1000000")),
        ("unix_microseconds", Decimal("1000")),
        ("unix_nanoseconds", Decimal("1")),
    )
    minimum_ns = int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp() * 1e9)
    maximum_ns = receive_wall_ns + 86_400 * 1_000_000_000
    plausible: list[tuple[int, str, int]] = []
    for unit, multiplier in candidates:
        candidate_ns = int(value * multiplier)
        if minimum_ns <= candidate_ns <= maximum_ns:
            plausible.append((abs(receive_wall_ns - candidate_ns), unit, candidate_ns))
    if not plausible:
        return raw_text, "other_unplausible", "", None, "no_plausible_epoch_unit"
    _distance, unit, value_ns = min(plausible, key=lambda item: item[0])
    return (
        raw_text, unit, utc_iso_from_ns(value_ns), value_ns,
        "unit_validated=receipt_clock_plausibility+official_market_channel_schema",
    )


class MarketWsLatencyCsvDiagnostic:
    """Bounded, non-blocking CSV sampler with separate stale/fresh quotas."""

    def __init__(self, path: Path, *, duration_seconds: int = 300, max_rows: int = 2_000, stale_quota: int = 1_000) -> None:
        self.path = Path(path)
        self.duration_seconds = min(300, max(1, int(duration_seconds)))
        self.max_rows = min(2_000, max(1, int(max_rows)))
        self.stale_quota = min(self.max_rows, max(0, int(stale_quota)))
        self.fresh_quota = self.max_rows - self.stale_quota
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self.max_rows)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_monotonic = 0.0
        self._accepting = False
        self._sample_number = 0
        self._stale_rows = 0
        self._fresh_rows = 0
        self.dropped_rows = 0

    @property
    def accepting(self) -> bool:
        return self._accepting and time.monotonic() < self._started_monotonic + self.duration_seconds

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_monotonic = time.monotonic()
        self._accepting = True
        self._thread = threading.Thread(target=self._writer_main, name="market-ws-latency-csv", daemon=True)
        self._thread.start()

    def submit(self, row: dict[str, Any], *, stale: bool) -> bool:
        if not self.accepting:
            self._accepting = False
            self._stop.set()
            return False
        if stale and self._stale_rows >= self.stale_quota:
            return False
        if not stale and self._fresh_rows >= self.fresh_quota:
            return False
        candidate = {field: row.get(field, "") for field in CSV_FIELDS}
        candidate["sample_number"] = self._sample_number + 1
        try:
            self._queue.put_nowait(candidate)
        except queue.Full:
            self.dropped_rows += 1
            return False
        self._sample_number += 1
        if stale:
            self._stale_rows += 1
        else:
            self._fresh_rows += 1
        if self._sample_number >= self.max_rows:
            self._accepting = False
            self._stop.set()
        return True

    def close(self, timeout: float = 5.0) -> None:
        self._accepting = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _writer_main(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows_since_flush = 0
        last_flush = time.monotonic()
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            os.chmod(self.path, 0o600)
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            handle.flush()
            while True:
                if time.monotonic() >= self._started_monotonic + self.duration_seconds:
                    self._accepting = False
                    self._stop.set()
                try:
                    row = self._queue.get(timeout=0.2)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                writer.writerow(row)
                self._queue.task_done()
                rows_since_flush += 1
                now = time.monotonic()
                if rows_since_flush >= 50 or now - last_flush >= 1.0:
                    handle.flush()
                    rows_since_flush = 0
                    last_flush = now
                if self._stop.is_set() and self._queue.empty():
                    break
            handle.flush()
            os.fsync(handle.fileno())

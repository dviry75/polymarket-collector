from __future__ import annotations

from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
import asyncio
import array
import fcntl
import hashlib
import json
import logging
import os
import random
import socket
import statistics
import time
from pathlib import Path

from .repository import LiveRepository, now_iso
from .order_book import OrderBookSet, canonical_decimal, decimal_value
from .market_ws_latency_csv import (
    MarketWsLatencyCsvDiagnostic, normalize_exchange_timestamp,
    utc_iso_from_ns,
)


@dataclass
class WebSocketStatus:
    channel: str
    status: str = "NOT_CONNECTED"
    last_message_at: str | None = None
    reconnect_attempts: int = 0
    stale: bool = True
    error: str | None = None


class MarketWebSocketManager:
    def __init__(
        self,
        repo: LiveRepository,
        stale_after_seconds: int = 30,
        on_snapshot: Callable[[dict[str, Any]], Any] | None = None,
        on_atomic_frame: Callable[[dict[str, Any]], Any] | None = None,
        persist_raw_payloads: bool = False,
        snapshot_min_interval_seconds: float = 0.5,
        on_reconnect: Callable[[], Awaitable[Any]] | None = None,
        persistence_queue_capacity: int = 64,
        ingress_queue_capacity: int = 32,
        include_depth_in_callback: bool = True,
        future_tolerance_ms: int = 1_000,
        clock_ms: Callable[[], int] | None = None,
    ):
        self.repo = repo
        self._logger = logging.getLogger("uvicorn.error")
        self.stale_after_seconds = stale_after_seconds
        self.status = WebSocketStatus(channel="market")
        self.on_snapshot = on_snapshot
        self.on_atomic_frame = on_atomic_frame
        self.persist_raw_payloads = persist_raw_payloads
        self.snapshot_min_interval_seconds = max(0.0, snapshot_min_interval_seconds)
        self.on_reconnect = on_reconnect
        self.subscribed_asset_ids: list[str] = []
        self.order_books = OrderBookSet()
        self._last_snapshot_monotonic: dict[str, float] = {}
        self._last_snapshot_signature: dict[str, str] = {}
        self.messages_received = 0
        self.snapshots_received = 0
        self.last_ping_at = self.last_pong_at = None
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._ws = None
        self._lock = asyncio.Lock()
        self.future_tolerance_ms = max(0, int(future_tolerance_ms))
        self._clock_ms = clock_ms or (
            lambda: int(datetime.now(timezone.utc).timestamp() * 1000)
        )
        self.persistence_queue_capacity = max(2, int(persistence_queue_capacity))
        self.ingress_queue_capacity = max(2, int(ingress_queue_capacity))
        self.include_depth_in_callback = bool(include_depth_in_callback)
        self._ingress_queue: asyncio.Queue[Any] | None = None
        self.max_ingress_queue_depth = 0
        self.ingress_frames_enqueued = 0
        self.ingress_frames_dequeued = 0
        self.ingress_queue_saturations = 0
        self.ingress_resyncs = 0
        self.ingress_market_frames_discarded = 0
        self.ingress_critical_frames_preserved = 0
        self.ingress_resync_reasons: dict[str, int] = {}
        self._integrity_resync_reason = ""
        self.unsubscribed_market_frames_ignored = 0
        self.unsubscribed_asset_counts: dict[str, int] = {}
        self._pending_snapshots: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._persistence_event = asyncio.Event()
        self._persistence_task: asyncio.Task[Any] | None = None

        # One dedicated thread owns the long-lived SQLite writer connection.
        # It never competes with the event loop or the default asyncio
        # thread pool used by unrelated background operations.
        self._persistence_executor: ThreadPoolExecutor | None = None
        self._persistence_connection: Any | None = None
        self.persistence_batches = 0
        self.persistence_failures = 0
        self.persistence_last_error = ""

        self._markets_by_asset: dict[str, dict[str, Any]] = {}
        self._markets_by_condition: dict[str, dict[str, Any]] = {}
        self.snapshots_coalesced = 0
        self.snapshots_dropped = 0
        self.dynamic_subscriptions = 0
        self.dynamic_subscription_fallbacks = 0
        self.rejected_frames = 0
        self.out_of_order_frames = 0
        self.rejection_reasons: dict[str, int] = {}
        self.idle_ready_checks_over_threshold = 0
        self.last_exchange_age_ms: int | None = None
        self.last_receive_latency_ms: int | None = None
        self.last_message_processing_ms = 0.0
        self.max_message_processing_ms = 0.0
        self.max_persistence_queue_depth = 0
        self._pending_states: dict[str, str] = {}
        self._last_queued_state_values: dict[str, str] = {}
        self._last_message_state_monotonic = 0.0
        self._readiness_state = ""
        self._not_ready_started_monotonic: float | None = None
        self.not_ready_transitions = 0
        self.not_ready_total_seconds = 0.0
        self.not_ready_max_seconds = 0.0
        self._latency_records: deque[dict[str, Any]] = deque(maxlen=10_000)
        self._event_loop_lag_samples: deque[float] = deque(maxlen=10_000)
        self._event_loop_lag_ms = 0.0
        self._event_loop_lag_max_ms = 0.0
        self._event_loop_watchdog_task: asyncio.Task[Any] | None = None
        self._diagnostics_task: asyncio.Task[Any] | None = None
        self._last_handler_end_monotonic: float | None = None
        self._connection_generation = 0
        self._connection_diagnostics: dict[str, Any] = {}
        self._connection_started_monotonic = 0.0
        self._last_subscription_change_monotonic = 0.0
        self._connection_frame_index = 0
        latency_csv_path = os.getenv("LIVE_MARKET_WS_LATENCY_CSV_PATH", "").strip()
        self._latency_csv = (
            MarketWsLatencyCsvDiagnostic(
                Path(latency_csv_path), duration_seconds=300,
                max_rows=2_000, stale_quota=1_000,
            )
            if latency_csv_path else None
        )
        diagnostics_default = (
            Path(getattr(self.repo, "db_path", "/opt/polymarket-btc-live/poly_live.sqlite3")).parent
            / "output" / "market_ws_latency_diagnostics.json"
        )
        self._diagnostics_path = Path(
            os.getenv("LIVE_MARKET_WS_DIAGNOSTICS_PATH", str(diagnostics_default))
        )

    def subscription_message(self, asset_ids: list[str]) -> dict[str, Any]:
        return {"type": "market", "assets_ids": asset_ids, "custom_feature_enabled": True}

    def dynamic_subscription_message(self, asset_ids: list[str], operation: str = "subscribe") -> dict[str, Any]:
        if operation not in {"subscribe", "unsubscribe"}:
            raise ValueError("invalid subscription operation")
        message: dict[str, Any] = {"operation": operation, "assets_ids": asset_ids}
        # The official SDK intentionally omits custom_feature_enabled from
        # unsubscribe updates. Some CLOB deployments reject or ignore the
        # unsubscribe frame when this subscribe-only field is present.
        if operation == "subscribe":
            message["custom_feature_enabled"] = True
        return message

    async def start(self, url: str) -> None:
        async with self._lock:
            if self._task and not self._task.done():
                return
            if self._latency_csv is not None:
                self._latency_csv.start()
            self._stop.clear()
            self._ensure_persistence_writer()
            self._event_loop_watchdog_task = asyncio.create_task(
                self._event_loop_watchdog(), name="market-ws-event-loop-watchdog"
            )
            self._diagnostics_task = asyncio.create_task(
                self._diagnostics_loop(), name="market-ws-latency-diagnostics"
            )
            self._task = asyncio.create_task(self.run(url), name="polymarket-market-ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()
        if self._task:
            try:
                await asyncio.wait_for(self._task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        if self._pending_snapshots or self._pending_states:
            self._persistence_event.set()
        if self._persistence_task:
            try:
                await asyncio.wait_for(
                    self._persistence_task,
                    5,
                )
            except (
                asyncio.TimeoutError,
                asyncio.CancelledError,
            ):
                self._persistence_task.cancel()

        if self._persistence_executor is not None:
            executor = self._persistence_executor

            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    executor,
                    self._close_persistence_connection_sync,
                )
            finally:
                self._persistence_executor = None
                await asyncio.to_thread(
                    executor.shutdown,
                    True,
                )

        for task in (
            self._event_loop_watchdog_task,
            self._diagnostics_task,
        ):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._latency_csv is not None:
            await asyncio.to_thread(self._latency_csv.close)
        self._event_loop_watchdog_task = self._diagnostics_task = None
        self.status.status = "STOPPED"
        self.status.stale = True

    async def run(self, url: str, connect=None) -> None:
        if connect is None:
            try:
                import websockets
            except Exception:
                self.mark_disconnect("websockets package unavailable")
                return
            connector = websockets.connect
        else:
            connector = connect
        attempt = 0
        while not self._stop.is_set():
            asset_ids = await asyncio.to_thread(self.repo.market_ws_asset_ids)
            if not asset_ids:
                self.status.status = "WAITING_FOR_MARKETS"
                try:
                    await asyncio.wait_for(self._stop.wait(), 1)
                except asyncio.TimeoutError:
                    pass
                continue
            self.status.status = "CONNECTING" if attempt == 0 else "RECONNECTING"
            try:
                async with connector(
                    url,
                    ping_interval=None,
                    close_timeout=5,
                    max_queue=(4, 1),
                    max_size=2 * 1024 * 1024,
                    compression=None,
                ) as ws:
                    self._ws = ws
                    self._connection_generation += 1
                    self._connection_started_monotonic = time.monotonic()
                    self._last_subscription_change_monotonic = self._connection_started_monotonic
                    self._connection_frame_index = 0
                    self._connection_diagnostics = self._connection_metadata(ws, url)
                    self._logger.info(
                        "MARKET_WS_CONNECTED generation=%s remote=%s assets=%s",
                        self._connection_generation, getattr(ws, "remote_address", None),
                        len(asset_ids),
                    )
                    self.order_books.ensure_assets(asset_ids)
                    self.order_books.mark_not_ready("RECONNECT_AWAITING_FRESH_BOOKS")
                    await asyncio.to_thread(self._refresh_market_cache, asset_ids)
                    await ws.send(json.dumps(self.subscription_message(asset_ids)))
                    self.subscribed_asset_ids = asset_ids
                    self.status.status = "SUBSCRIBED"
                    self.status.error = None
                    attempt = 0
                    if self.on_reconnect is not None:
                        await self.on_reconnect()
                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    subscriptions = asyncio.create_task(self._subscription_loop(ws))
                    try:
                        await self._run_ingress_pipeline(ws)
                    finally:
                        heartbeat.cancel()
                        subscriptions.cancel()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._ws = None
                attempt += 1
                self.mark_disconnect(f"{type(exc).__name__}: {exc}"[:500])
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), min(30.0, 2 ** min(attempt, 5)) + random.random()
                    )
                except asyncio.TimeoutError:
                    pass
        self.status.status = "STOPPED"

    async def _run_ingress_pipeline(self, ws: Any) -> None:
        """Drain the websocket continuously while processing book events in order."""
        queue: asyncio.Queue[Any] = asyncio.Queue(self.ingress_queue_capacity)
        self._ingress_queue = queue
        self._integrity_resync_reason = ""
        reader_done = asyncio.Event()
        resync = asyncio.Event()
        reader = asyncio.create_task(
            self._market_frame_reader(ws, queue, reader_done, resync),
            name="market-ws-reader",
        )
        processor = asyncio.create_task(
            self._market_frame_processor(ws, queue, reader_done, resync),
            name="market-ws-frame-processor",
        )
        try:
            done, _pending = await asyncio.wait(
                {reader, processor}, return_when=asyncio.FIRST_COMPLETED
            )
            if processor in done and not reader.done():
                processor_error = processor.exception()
                await ws.close()
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                if processor_error is not None:
                    raise processor_error
                return
            reader_done.set()
            reader_error = reader.exception()
            await processor
            if reader_error is not None:
                raise reader_error
        finally:
            reader_done.set()
            for task in (reader, processor):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader, processor, return_exceptions=True)
            self._ingress_queue = None

    async def _market_frame_reader(
        self, ws: Any, queue: asyncio.Queue[Any], reader_done: asyncio.Event,
        resync: asyncio.Event,
    ) -> None:
        try:
            while not self._stop.is_set() and not resync.is_set():
                before_recv = time.perf_counter()
                between_recv_gap_ms = (
                    (before_recv - self._last_handler_end_monotonic) * 1000
                    if self._last_handler_end_monotonic is not None else None
                )
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=max(15, self.stale_after_seconds)
                )
                # Earliest receipt boundary: before frame sizing or JSON parsing.
                receive_wall_ns = time.time_ns()
                receive_monotonic_ns = time.monotonic_ns()
                recv_return = time.perf_counter()
                socket_receive_wall_ms = receive_wall_ns // 1_000_000
                frame_size_bytes = (
                    len(raw) if isinstance(raw, bytes)
                    else len(str(raw).encode("utf-8"))
                )
                library_queue_depth = self._ws_internal_queue_depth(ws)
                tcp_recv_q_bytes = self._tcp_recv_q_bytes(ws)
                if raw == "PONG" or raw == b"PONG":
                    self.last_pong_at = now_iso()
                    continue
                parse_start = time.perf_counter()
                self._connection_frame_index += 1
                receive_monotonic = receive_monotonic_ns / 1_000_000_000
                payload = json.loads(raw)
                parse_end = time.perf_counter()
                messages = payload if isinstance(payload, list) else [payload]
                base_timing = {
                    "connection_generation": self._connection_generation,
                    "before_recv_monotonic": before_recv,
                    "recv_return_monotonic": recv_return,
                    "socket_receive_wall_ms": socket_receive_wall_ms,
                    "receive_wall_ns": receive_wall_ns,
                    "receive_monotonic_ns": receive_monotonic_ns,
                    "frame_size_bytes": frame_size_bytes,
                    "connection_id": (
                        f"market-ws-{self._connection_generation}-"
                        f"{self._connection_diagnostics.get('connected_at', '')}"
                    ),
                    "connection_frame_index": self._connection_frame_index,
                    "occurred_after_reconnect": (
                        receive_monotonic - self._connection_started_monotonic <= 5.0
                    ),
                    "occurred_after_resubscribe": (
                        receive_monotonic - self._last_subscription_change_monotonic
                        <= 5.0
                    ),
                    "recv_wait_ms": (recv_return - before_recv) * 1000,
                    "between_recv_gap_ms": between_recv_gap_ms,
                    "parse_start_monotonic": parse_start,
                    "parse_end_monotonic": parse_end,
                    "parse_ms": (parse_end - parse_start) * 1000,
                    "ws_internal_queue_depth": library_queue_depth,
                    "tcp_recv_q_bytes": tcp_recv_q_bytes,
                    "event_loop_lag_ms": self._event_loop_lag_ms,
                }
                for index, message in enumerate(messages):
                    if resync.is_set():
                        break
                    if not isinstance(message, dict):
                        continue
                    timing = dict(base_timing)
                    timing["batch_index"] = index
                    timing["batch_size"] = len(messages)
                    timing["ingress_enqueued_monotonic"] = time.perf_counter()
                    item = (message, timing)
                    try:
                        queue.put_nowait(item)
                    except asyncio.QueueFull:
                        self._handle_ingress_saturation(queue, item, resync)
                        await ws.close()
                        raise ConnectionError("MARKET_WS_INGRESS_QUEUE_SATURATED")
                    self.ingress_frames_enqueued += 1
                    self.max_ingress_queue_depth = max(
                        self.max_ingress_queue_depth, queue.qsize()
                    )
                    if queue.qsize() >= max(2, self.ingress_queue_capacity // 2):
                        await asyncio.sleep(0)
        finally:
            reader_done.set()

    async def _market_frame_processor(
        self, ws: Any, queue: asyncio.Queue[Any], reader_done: asyncio.Event,
        resync: asyncio.Event,
    ) -> None:
        resync_error = ""
        while not (reader_done.is_set() and queue.empty()):
            try:
                message, timing = await asyncio.wait_for(queue.get(), 0.1)
            except asyncio.TimeoutError:
                continue
            try:
                event_type = str(
                    message.get("event_type") or message.get("type") or ""
                ).lower()
                if resync.is_set() and event_type != "market_resolved":
                    self.ingress_market_frames_discarded += 1
                    continue
                asset_ids = self._message_asset_ids(message)
                unexpected = [
                    asset for asset in asset_ids
                    if asset not in self.subscribed_asset_ids
                ]
                if unexpected and event_type != "market_resolved":
                    for asset in unexpected:
                        self.unsubscribed_asset_counts[asset] = (
                            self.unsubscribed_asset_counts.get(asset, 0) + 1
                        )
                    if event_type == "price_change":
                        retained = [
                            change for change in message.get("price_changes") or []
                            if isinstance(change, dict)
                            and str(change.get("asset_id") or "")
                            in self.subscribed_asset_ids
                        ]
                        if retained:
                            message = {**message, "price_changes": retained}
                        else:
                            self.unsubscribed_market_frames_ignored += 1
                            continue
                    elif asset_ids and len(unexpected) == len(asset_ids):
                        self.unsubscribed_market_frames_ignored += 1
                        continue
                timing["ingress_dequeued_monotonic"] = time.perf_counter()
                timing["ingress_queue_wait_ms"] = (
                    timing["ingress_dequeued_monotonic"]
                    - timing["ingress_enqueued_monotonic"]
                ) * 1000
                timing["ingress_queue_depth"] = queue.qsize()
                self.ingress_frames_dequeued += 1
                self.process_message(message, timing=timing)
                if self._integrity_resync_reason and not resync.is_set():
                    resync_error = self._integrity_resync_reason
                    latest = None
                    if isinstance(message.get("price_changes"), list):
                        latest = next((
                            item for item in reversed(message["price_changes"])
                            if isinstance(item, dict)
                        ), None)
                    self._logger.warning(
                        "MARKET_WS_BOOK_INTEGRITY_FAILURE generation=%s reason=%s "
                        "event_type=%s assets=%s timestamp=%s advertised_bid=%s "
                        "advertised_ask=%s computed_top=%s changes=%s exchange_age_ms=%s",
                        self._connection_generation, resync_error, event_type,
                        self._message_asset_ids(message), message.get("timestamp"),
                        (latest or message).get("best_bid"),
                        (latest or message).get("best_ask"),
                        timing.get("book_top"),
                        [
                            {
                                key: change.get(key) for key in (
                                    "asset_id", "side", "price", "size",
                                    "best_bid", "best_ask",
                                )
                            }
                            for change in (message.get("price_changes") or [])[-16:]
                            if isinstance(change, dict)
                        ],
                        timing.get("exchange_to_socket_receive_ms"),
                    )
                    self._begin_ingress_resync(resync_error, resync)
                    await ws.close()
            finally:
                queue.task_done()
        if resync_error:
            raise ConnectionError(f"MARKET_WS_BOOK_RESYNC:{resync_error}")

    def _begin_ingress_resync(self, reason: str, resync: asyncio.Event) -> None:
        self.ingress_resyncs += 1
        self.ingress_resync_reasons[reason] = (
            self.ingress_resync_reasons.get(reason, 0) + 1
        )
        resync.set()
        self.order_books.mark_not_ready(reason)
        self._queue_states({
            "strategy_readiness": "NOT_READY",
            "strategy_block_reason": reason,
        })
        self._logger.warning(
            "MARKET_WS_BOOK_RESYNC generation=%s reason=%s",
            self._connection_generation, reason,
        )

    def _handle_ingress_saturation(
        self, queue: asyncio.Queue[Any], current: tuple[Any, Any],
        resync: asyncio.Event,
    ) -> None:
        self.ingress_queue_saturations += 1
        self._begin_ingress_resync("INGRESS_QUEUE_SATURATED_RESYNC", resync)
        retained: list[tuple[Any, Any]] = []
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            queue.task_done()
            event_type = str(
                item[0].get("event_type") or item[0].get("type") or ""
            ).lower()
            if event_type == "market_resolved":
                retained.append(item)
                self.ingress_critical_frames_preserved += 1
            else:
                self.ingress_market_frames_discarded += 1
        current_type = str(
            current[0].get("event_type") or current[0].get("type") or ""
        ).lower()
        if current_type == "market_resolved":
            retained.append(current)
            self.ingress_critical_frames_preserved += 1
        else:
            self.ingress_market_frames_discarded += 1
        for item in retained:
            queue.put_nowait(item)
        self._logger.warning(
            "MARKET_WS_INGRESS_QUEUE_SATURATED generation=%s capacity=%s "
            "critical_preserved=%s discarded_total=%s action=RECONNECT_RESYNC",
            self._connection_generation, self.ingress_queue_capacity,
            len(retained), self.ingress_market_frames_discarded,
        )

    async def _heartbeat(self, ws) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(10)
            await ws.send("PING")
            self.last_ping_at = now_iso()

    async def _subscription_loop(self, ws) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1)
            wanted = await asyncio.to_thread(self.repo.market_ws_asset_ids)
            add = [asset for asset in wanted if asset not in self.subscribed_asset_ids]
            remove = [asset for asset in self.subscribed_asset_ids if asset not in wanted]
            if add:
                rotation_started = time.perf_counter()
                prior_generation = self._connection_generation
                self._logger.info(
                    "MARKET_WS_DYNAMIC_SUBSCRIBE_START generation=%s add=%s remove=%s",
                    prior_generation, add, remove,
                )
                combined = list(dict.fromkeys([*self.subscribed_asset_ids, *add]))
                self.order_books.ensure_assets(combined)
                await asyncio.to_thread(self._refresh_market_cache, combined)
                await ws.send(json.dumps(self.dynamic_subscription_message(add, "subscribe")))
                self._last_subscription_change_monotonic = time.monotonic()
                self.subscribed_asset_ids = combined
                self.dynamic_subscriptions += 1
                # Dynamic subscribe is supported by the market channel. Keep the old
                # assets until every added token receives a new snapshot; otherwise
                # fail closed and reconnect for a full generation warm-up.
                deadline = time.monotonic() + 5.0
                missing = list(add)
                while time.monotonic() < deadline and not self._stop.is_set():
                    missing = [
                        asset for asset in add
                        if not self.order_books.books.get(asset)
                        or not self.order_books.books[asset].ready
                    ]
                    if not missing:
                        break
                    await asyncio.sleep(0.05)
                if missing:
                    self._logger.warning(
                        "MARKET_WS_DYNAMIC_SUBSCRIBE_TIMEOUT generation=%s missing=%s elapsed_ms=%.3f",
                        prior_generation, missing,
                        (time.perf_counter() - rotation_started) * 1000,
                    )
                    self.dynamic_subscription_fallbacks += 1
                    await ws.close()
                    return
            if add:
                self._logger.info(
                    "MARKET_WS_DYNAMIC_SUBSCRIBE_READY generation=%s assets=%s elapsed_ms=%.3f",
                    self._connection_generation, add,
                    (time.perf_counter() - rotation_started) * 1000,
                )
            if remove:
                await ws.send(json.dumps(self.dynamic_subscription_message(remove, "unsubscribe")))
                self._last_subscription_change_monotonic = time.monotonic()
            if add or remove:
                self.subscribed_asset_ids = list(wanted)
                self.order_books.ensure_assets(wanted)
                await asyncio.to_thread(self._refresh_market_cache, wanted)
                self._logger.info(
                    "MARKET_WS_ROTATION_COMPLETE generation_before=%s generation_after=%s subscribed=%s removed=%s",
                    prior_generation if add else self._connection_generation,
                    self._connection_generation, wanted, remove,
                )

    async def connect_for_messages(self, url: str, asset_ids: list[str], *, max_messages: int = 1, timeout_seconds: float = 20.0) -> dict[str, Any]:
        """Bounded public smoke connection. It never uses credentials or trading APIs."""
        try:
            import websockets
        except Exception as exc:
            self.mark_disconnect(f"websockets unavailable: {exc}")
            return {"connected": False, "messages": 0, "error": "websockets package is not available"}

        received = 0
        self.status.status = "CONNECTING"
        try:
            async with websockets.connect(url, ping_interval=None, close_timeout=2) as ws:
                self.status.status = "CONNECTED"
                await ws.send(json.dumps(self.subscription_message(asset_ids)))

                async def heartbeat() -> None:
                    while True:
                        await asyncio.sleep(10)
                        await ws.send("PING")

                heartbeat_task = asyncio.create_task(heartbeat())
                try:
                    deadline = asyncio.get_running_loop().time() + timeout_seconds
                    while received < max_messages and asyncio.get_running_loop().time() < deadline:
                        remaining = max(0.1, deadline - asyncio.get_running_loop().time())
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                        if raw == "PONG":
                            continue
                        try:
                            payload = json.loads(raw)
                        except Exception:
                            payload = {"event_type": "raw", "payload": str(raw)}
                        if isinstance(payload, list):
                            for item in payload:
                                if isinstance(item, dict):
                                    self.process_message(item)
                                    received += 1
                        elif isinstance(payload, dict):
                            self.process_message(payload)
                            received += 1
                finally:
                    heartbeat_task.cancel()
            return {"connected": True, "messages": received, "error": ""}
        except Exception as exc:
            self.mark_disconnect(f"{type(exc).__name__}: {exc}")
            return {"connected": False, "messages": received, "error": f"{type(exc).__name__}: {exc}"}

    def reconnect_delay_seconds(self) -> float:
        base = min(30.0, 2 ** max(0, self.status.reconnect_attempts))
        return base + random.uniform(0, 1)

    def process_message(
        self, message: dict[str, Any], timing: dict[str, Any] | None = None
    ) -> bool:
        started = time.perf_counter()
        diagnostic_timing = timing is not None
        timing = timing if timing is not None else {}
        timing["handler_entry_monotonic"] = started
        event_type = str(message.get("event_type") or message.get("type") or "").lower()
        timing["event_type"] = event_type
        timestamp_raw = message.get("timestamp")
        exchange_ms, _timestamp_error = self.order_books._exchange_timestamp_ms(
            str(timestamp_raw) if timestamp_raw not in (None, "") else None
        )
        timing["exchange_timestamp_ms"] = exchange_ms
        if exchange_ms is not None and timing.get("socket_receive_wall_ms") is not None:
            timing["exchange_to_socket_receive_ms"] = (
                timing["socket_receive_wall_ms"] - exchange_ms
            )
        if timing.get("recv_return_monotonic") is not None:
            timing["socket_receive_to_handler_ms"] = (
                started - timing["recv_return_monotonic"]
            ) * 1000
        # Second boundary: immediately before book/readiness processing.
        processing_wall_ns = time.time_ns()
        processing_monotonic_ns = time.monotonic_ns()
        timing["processing_wall_ns"] = processing_wall_ns
        timing["processing_monotonic_ns"] = processing_monotonic_ns
        now_ms = self._clock_ms()
        frame = None
        # Local receipt time is observability/persistence only. It is never used
        # for market-data freshness or entry authorization.
        received_at = now_iso()
        callback_updates: list[dict[str, Any]] = []
        stored_critical = False

        if event_type == "market_resolved":
            # Resolution is critical and must never enter the lossy snapshot queue.
            for candidate in self._normalize_snapshots(message):
                snapshot = self.repo.store_market_snapshot(candidate)
                callback = snapshot or candidate
                callback_updates.append(callback)
                if snapshot is not None:
                    stored_critical = True
                    self.snapshots_received += 1
                    if self.on_snapshot is not None:
                        self.on_snapshot(snapshot)
            condition_id = message.get("condition_id") or message.get("market")
            if condition_id:
                self.repo.mark_market_resolved(
                    str(condition_id), message.get("winning_asset_id"),
                    message.get("winning_outcome"),
                )
        else:
            assets = self.subscribed_asset_ids or list(self._markets_by_asset)
            if not assets:
                assets = self.repo.market_ws_asset_ids()
                if not assets:
                    # Direct process_message() callers (tests/smoke tools) do
                    # not necessarily have a live WS subscription. Fall back
                    # to the assets explicitly carried by this frame only.
                    #
                    # A real Market WS connection always has
                    # subscribed_asset_ids, so this does not broaden the
                    # production subscription scope.
                    assets = self._message_asset_ids(message)
                self._refresh_market_cache(assets)
            self.order_books.ensure_assets(assets)
            frame = self.order_books.apply(
                message,
                now_ms=now_ms,
                max_age_ms=self.stale_after_seconds * 1000,
                future_tolerance_ms=self.future_tolerance_ms,
                include_depth=self.include_depth_in_callback,
            )
            timing["message_hash"] = frame.message_hash
            timing["book_update_monotonic"] = time.perf_counter()
            timing["handler_to_book_update_ms"] = (
                timing["book_update_monotonic"] - started
            ) * 1000
            timing["rejected_reason"] = frame.rejected_reason
            timing["out_of_order"] = frame.out_of_order
            timing["book_top"] = [
                {
                    "asset_id": view.get("asset_id"),
                    "best_bid": view.get("best_bid"),
                    "best_ask": view.get("best_ask"),
                    "reason": view.get("readiness_reason"),
                }
                for view in frame.updates
            ]
            self.last_exchange_age_ms = frame.exchange_age_ms
            self.last_receive_latency_ms = frame.receive_latency_ms
            if frame.out_of_order:
                self.out_of_order_frames += 1
                self.rejected_frames += 1
                self.rejection_reasons["OUT_OF_ORDER_EXCHANGE_TIMESTAMP"] = (
                    self.rejection_reasons.get("OUT_OF_ORDER_EXCHANGE_TIMESTAMP", 0) + 1
                )
            if frame.rejected_reason:
                if not frame.out_of_order:
                    self.rejected_frames += 1
                if not (
                    frame.out_of_order
                    and frame.rejected_reason == "OUT_OF_ORDER_EXCHANGE_TIMESTAMP"
                ):
                    self.rejection_reasons[frame.rejected_reason] = (
                        self.rejection_reasons.get(frame.rejected_reason, 0) + 1
                    )
                self._queue_states({
                    "strategy_readiness": "NOT_READY",
                    "strategy_block_reason": frame.rejected_reason,
                })
            for view in frame.updates:
                asset_id = str(view["asset_id"])
                market = self._markets_by_asset.get(asset_id)
                if not market:
                    market = self.repo.market_for_asset(asset_id)
                    if market:
                        self._cache_market(market)
                if not market:
                    continue
                outcome = (
                    "YES" if str(market.get("yes_token_id")) == asset_id
                    else "NO" if str(market.get("no_token_id")) == asset_id
                    else None
                )
                candidate = {
                    **view,
                    "condition_id": market["condition_id"],
                    "event_id": market.get("event_id"),
                    "outcome": outcome,
                    "received_at": received_at,
                    "latency_ms": frame.receive_latency_ms,
                    "source": "POLYMARKET_MARKET_WS",
                    "raw_message": (
                        {"message": message, "asset_id": asset_id}
                        if self.persist_raw_payloads else {
                            "event_type": frame.event_type,
                            "message_hash": frame.message_hash,
                            "asset_id": asset_id,
                        }
                    ),
                    "id": -self.messages_received - len(callback_updates) - 1,
                }
                callback_updates.append(candidate)
                if self._should_persist_snapshot(candidate):
                    persistent_candidate = candidate
                    if not self.include_depth_in_callback:
                        book = self.order_books.books.get(asset_id)
                        if book is not None:
                            # Copy is intentionally performed while still on
                            # the event-loop-owned order book. The expensive
                            # sort + canonical level construction is deferred
                            # to the dedicated persistence thread.
                            persistent_candidate = {
                                **candidate,
                                "_persistence_bid_items": tuple(
                                    book.bids.items()
                                ),
                                "_persistence_ask_items": tuple(
                                    book.asks.items()
                                ),
                            }

                    self._enqueue_snapshot(
                        persistent_candidate
                    )
            integrity_reasons = {
                str(view.get("readiness_reason") or "") for view in frame.updates
            }
            for reason in (
                "DELTA_BEFORE_SNAPSHOT", "BEST_UPDATE_BEFORE_SNAPSHOT",
                "BEST_PRICE_MISMATCH", "MALFORMED_DELTA",
            ):
                if reason in integrity_reasons:
                    if reason == "BEST_PRICE_MISMATCH" and event_type == "best_bid_ask":
                        continue
                    self._integrity_resync_reason = reason
                    break

        if "book_update_monotonic" not in timing:
            timing["message_hash"] = hashlib.sha256(
                json.dumps(
                    message, sort_keys=True, separators=(",", ":"), default=str
                ).encode()
            ).hexdigest()
            timing["book_update_monotonic"] = time.perf_counter()
            timing["handler_to_book_update_ms"] = (
                timing["book_update_monotonic"] - started
            ) * 1000
        self.messages_received += 1
        self.status.status = "CONNECTED"
        self.status.last_message_at = received_at
        self.status.stale = False
        status_values = {"market_ws_status": self.status.status}
        monotonic_now = time.monotonic()
        if monotonic_now - self._last_message_state_monotonic >= 1.0:
            status_values["market_ws_last_message_at"] = self.status.last_message_at
            self._last_message_state_monotonic = monotonic_now
        self._queue_states(status_values)

        event_readiness: dict[str, dict[str, Any]] = {}
        for update in callback_updates:
            condition_id = str(update.get("condition_id") or "")
            if condition_id and condition_id not in event_readiness:
                event_readiness[condition_id] = self.event_freshness(
                    condition_id, now_ms=now_ms
                )
        if event_readiness:
            all_ready = all(item["ready"] for item in event_readiness.values())
            reason = "" if all_ready else next(
                item["reason"] for item in event_readiness.values() if not item["ready"]
            )
            self._queue_states({
                "strategy_readiness": "READY" if all_ready else "NOT_READY",
                "strategy_block_reason": reason,
            })
        if diagnostic_timing and self._latency_csv is not None:
            self._record_latency_csv(
                message, timing=timing, frame=frame, event_readiness=event_readiness
            )
        if (
            self.on_atomic_frame is not None and callback_updates
            and not (event_type != "market_resolved" and frame.rejected_reason)
        ):
            context = {
                "event_type": event_type,
                "message_hash": (
                    frame.message_hash if event_type != "market_resolved"
                    else str(callback_updates[0].get("message_hash") or "")
                ),
                "received_at": received_at,
                "updates": callback_updates,
                "event_readiness": event_readiness,
            }
            timing["strategy_scheduled_monotonic"] = time.perf_counter()
            timing["book_update_to_strategy_ms"] = (
                timing["strategy_scheduled_monotonic"]
                - timing["book_update_monotonic"]
            ) * 1000
            context["_latency_timing"] = timing
            result = self.on_atomic_frame(context)
            if asyncio.iscoroutine(result):
                try:
                    asyncio.get_running_loop().create_task(result)
                except RuntimeError:
                    asyncio.run(result)

        ended = time.perf_counter()
        elapsed_ms = (ended - started) * 1000
        timing["handler_end_monotonic"] = ended
        timing["total_processing_ms"] = elapsed_ms
        timing["handler_to_strategy_ms"] = (
            (timing.get("strategy_scheduled_monotonic") or ended) - started
        ) * 1000
        self._last_handler_end_monotonic = ended
        if diagnostic_timing:
            timing["asset_ids"] = self._message_asset_ids(message)
            self._latency_records.append(timing)
        self.last_message_processing_ms = elapsed_ms
        self.max_message_processing_ms = max(self.max_message_processing_ms, elapsed_ms)
        return (
            stored_critical if event_type == "market_resolved"
            else bool(callback_updates) and not frame.rejected_reason
        )

    @staticmethod
    def _message_asset_ids(message: dict[str, Any]) -> list[str]:
        if isinstance(message.get("price_changes"), list):
            return sorted({
                str(item.get("asset_id") or "") for item in message["price_changes"]
                if isinstance(item, dict) and item.get("asset_id")
            })
        asset = str(message.get("asset_id") or "")
        return [asset] if asset else []

    def _record_latency_csv(
        self, message: dict[str, Any], *, timing: dict[str, Any],
        frame: Any, event_readiness: dict[str, dict[str, Any]],
    ) -> None:
        diagnostic = self._latency_csv
        receive_wall_ns = timing.get("receive_wall_ns")
        receive_monotonic_ns = timing.get("receive_monotonic_ns")
        processing_wall_ns = timing.get("processing_wall_ns")
        processing_monotonic_ns = timing.get("processing_monotonic_ns")
        if diagnostic is None or None in (
            receive_wall_ns, receive_monotonic_ns,
            processing_wall_ns, processing_monotonic_ns,
        ):
            return
        raw_text, unit, normalized_utc, exchange_ns, timestamp_note = (
            normalize_exchange_timestamp(
                message.get("timestamp"), receive_wall_ns=int(receive_wall_ns)
            )
        )
        transport_ms = (
            (int(receive_wall_ns) - exchange_ns) / 1_000_000
            if exchange_ns is not None else None
        )
        queue_wait_ms = (
            int(processing_monotonic_ns) - int(receive_monotonic_ns)
        ) / 1_000_000
        total_age_ms = (
            (int(processing_wall_ns) - exchange_ns) / 1_000_000
            if exchange_ns is not None else None
        )
        event_type = str(
            message.get("event_type") or message.get("type") or ""
        ).lower()
        nested = (
            [item for item in message.get("price_changes") or []
             if isinstance(item, dict)]
            if event_type == "price_change" else [message]
        )
        frame_updates = list(getattr(frame, "updates", ()) or ())
        rejected_reason = str(getattr(frame, "rejected_reason", "") or "")
        duplicate = bool(getattr(frame, "duplicate", False))
        outer_index = int(timing.get("batch_index") or 0)
        outer_batch_size = int(timing.get("batch_size") or 1)
        for nested_index, item in enumerate(nested):
            token_id = str(item.get("asset_id") or message.get("asset_id") or "")
            market = self._markets_by_asset.get(token_id) or {}
            condition_id = str(
                market.get("condition_id") or message.get("market") or ""
            )
            readiness_data = event_readiness.get(condition_id) or {}
            block_reason = rejected_reason or str(readiness_data.get("reason") or "")
            if duplicate and not block_reason:
                block_reason = "DUPLICATE_FRAME_IGNORED"
            if duplicate:
                # Duplicate frames never reach readiness or strategy evaluation.
                # This label is observability only; it does not alter the decision.
                readiness = "NOT_EVALUATED"
            elif rejected_reason:
                readiness = "NOT_READY"
            elif readiness_data:
                readiness = "READY" if readiness_data.get("ready") else "NOT_READY"
            else:
                readiness = "NOT_EVALUATED"
            stale = block_reason == "STALE_EXCHANGE_TIMESTAMP"
            view = next((
                candidate for candidate in frame_updates
                if str(candidate.get("asset_id") or "") == token_id
            ), {})
            best_bid = item.get("best_bid", view.get("best_bid", ""))
            best_ask = item.get("best_ask", view.get("best_ask", ""))
            if event_type == "book" and not view:
                bid_prices = [
                    decimal_value(level.get("price"))
                    for level in message.get("bids") or [] if isinstance(level, dict)
                ]
                ask_prices = [
                    decimal_value(level.get("price"))
                    for level in message.get("asks") or [] if isinstance(level, dict)
                ]
                bid_prices = [price for price in bid_prices if price is not None]
                ask_prices = [price for price in ask_prices if price is not None]
                best_bid = canonical_decimal(max(bid_prices)) if bid_prices else ""
                best_ask = canonical_decimal(min(ask_prices)) if ask_prices else ""
            notes = [
                timestamp_note,
                "timestamp_source=top_level_market_channel",
                f"ws_internal_queue_depth={timing.get('ws_internal_queue_depth')}",
                f"tcp_recv_q_bytes={timing.get('tcp_recv_q_bytes')}",
            ]
            if len(nested) > 1:
                notes.extend((
                    f"price_change_item={nested_index + 1}/{len(nested)}",
                    "exchange_timestamp_shared_by_price_change_items=true",
                ))
            if timing.get("occurred_after_resubscribe"):
                notes.append("within_5s_after_subscribe_or_resubscribe=true")
            if event_type == "book":
                notes.append("snapshot=true")
            if duplicate:
                notes.append("duplicate_frame_ignored=true")
            diagnostic.submit({
                "connection_id": timing.get("connection_id", ""),
                "reconnect_generation": timing.get("connection_generation", ""),
                "websocket_connected": str(
                    self.status.status in {"SUBSCRIBED", "CONNECTED"}
                ).lower(),
                "message_type": event_type,
                "event_id": market.get("event_id", ""),
                "market_id": message.get("market") or condition_id,
                "token_id": token_id,
                "side": item.get("side", ""),
                "raw_exchange_timestamp": raw_text,
                "detected_timestamp_unit": unit,
                "normalized_exchange_timestamp_utc": normalized_utc,
                "receive_timestamp_utc": utc_iso_from_ns(int(receive_wall_ns)),
                "processing_timestamp_utc": utc_iso_from_ns(int(processing_wall_ns)),
                "transport_latency_ms": (
                    round(transport_ms, 3) if transport_ms is not None else ""
                ),
                "queue_wait_ms": round(queue_wait_ms, 3),
                "total_age_at_processing_ms": (
                    round(total_age_ms, 3) if total_age_ms is not None else ""
                ),
                "frame_size_bytes": timing.get("frame_size_bytes", ""),
                "batch_size": outer_batch_size,
                "item_index_in_batch": (
                    f"{outer_index}:{nested_index}" if len(nested) > 1
                    else outer_index
                ),
                "queue_depth": timing.get("ingress_queue_depth", ""),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "readiness": readiness,
                "stale_classification": "STALE" if stale else "NOT_STALE",
                "exact_block_reason": block_reason or "READY",
                "occurred_after_reconnect": str(bool(
                    timing.get("occurred_after_reconnect")
                )).lower(),
                "notes": ";".join(notes),
            }, stale=stale)

    @staticmethod
    def _ws_internal_queue_depth(ws: Any) -> int | None:
        try:
            return len(ws.recv_messages.frames)
        except (AttributeError, TypeError):
            return None

    @staticmethod
    def _tcp_recv_q_bytes(ws: Any) -> int | None:
        try:
            transport = getattr(ws, "transport", None)
            raw_socket = transport.get_extra_info("socket") if transport else None
            if raw_socket is None:
                return None
            pending = array.array("i", [0])
            fcntl.ioctl(raw_socket.fileno(), 0x541B, pending, True)  # Linux FIONREAD
            return int(pending[0])
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    def _connection_metadata(self, ws: Any, url: str) -> dict[str, Any]:
        protocol = getattr(ws, "protocol", None)
        extensions = [
            type(extension).__name__
            for extension in (getattr(protocol, "extensions", None) or [])
        ]
        return {
            "generation": self._connection_generation,
            "connected_at": now_iso(),
            "url": url,
            "max_queue": list(getattr(ws, "max_queue", (None, None))),
            "max_size": getattr(protocol, "max_size", None),
            "write_limit": list(getattr(ws, "write_limit", (None, None))),
            "compression_extensions": extensions,
            "ping_interval": getattr(ws, "ping_interval", None),
            "ping_timeout": getattr(ws, "ping_timeout", None),
            "local_address": str(getattr(ws, "local_address", None)),
            "remote_address": str(getattr(ws, "remote_address", None)),
            "proxy_environment": bool(
                os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("ALL_PROXY")
            ),
        }

    async def _event_loop_watchdog(self) -> None:
        interval = 0.01
        expected = time.perf_counter() + interval
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            current = time.perf_counter()
            lag_ms = max(0.0, (current - expected) * 1000)
            self._event_loop_lag_ms = lag_ms
            self._event_loop_lag_max_ms = max(self._event_loop_lag_max_ms, lag_ms)
            self._event_loop_lag_samples.append(lag_ms)
            expected = current + interval

    @staticmethod
    def _percentiles(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"p50": None, "p95": None, "p99": None, "max": None}
        ordered = sorted(values)
        def at(fraction: float) -> float:
            return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * fraction) - 1))]
        return {
            "p50": round(statistics.median(ordered), 4),
            "p95": round(at(0.95), 4),
            "p99": round(at(0.99), 4),
            "max": round(ordered[-1], 4),
        }

    def latency_diagnostics(self) -> dict[str, Any]:
        records = list(self._latency_records)
        metric_names = (
            "exchange_to_socket_receive_ms", "socket_receive_to_handler_ms",
            "parse_ms", "handler_to_book_update_ms",
            "book_update_to_strategy_ms", "strategy_queue_delay_ms",
            "total_processing_ms", "event_loop_lag_ms", "recv_wait_ms",
            "between_recv_gap_ms", "ws_internal_queue_depth", "tcp_recv_q_bytes",
            "ingress_queue_wait_ms", "ingress_queue_depth",
        )
        metrics = {
            name: self._percentiles([
                float(record[name]) for record in records
                if record.get(name) is not None
            ])
            for name in metric_names
        }
        by_type: dict[str, dict[str, Any]] = {}
        for event_type in sorted({str(record.get("event_type") or "") for record in records}):
            subset = [record for record in records if record.get("event_type") == event_type]
            by_type[event_type] = {
                "count": len(subset),
                "exchange_to_socket_receive_ms": self._percentiles([
                    float(record["exchange_to_socket_receive_ms"]) for record in subset
                    if record.get("exchange_to_socket_receive_ms") is not None
                ]),
            }
        return {
            "generated_at": now_iso(),
            "health": self.health(),
            "record_count": len(records),
            "connection": dict(self._connection_diagnostics),
            "metrics": metrics,
            "by_event_type": by_type,
            "event_loop_lag_max_lifetime_ms": round(self._event_loop_lag_max_ms, 4),
            "recent_records": records[-2_000:],
        }

    async def _diagnostics_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30)
            try:
                snapshot = await asyncio.to_thread(self.latency_diagnostics)
                await asyncio.to_thread(self._write_diagnostics, snapshot)
            except OSError:
                pass

    def _write_diagnostics(self, snapshot: dict[str, Any]) -> None:
        self._diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._diagnostics_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(snapshot, sort_keys=True, default=str))
        temporary.replace(self._diagnostics_path)

    def _cache_market(self, market: dict[str, Any]) -> None:
        condition_id = str(market.get("condition_id") or "")
        if condition_id:
            self._markets_by_condition[condition_id] = market
        for key in ("yes_token_id", "no_token_id"):
            token = str(market.get(key) or "")
            if token:
                self._markets_by_asset[token] = market

    def _refresh_market_cache(self, asset_ids: list[str]) -> None:
        self._markets_by_asset.clear()
        self._markets_by_condition.clear()
        for asset_id in asset_ids:
            market = self.repo.market_for_asset(asset_id)
            if market:
                self._cache_market(market)

    def market_for_condition(self, condition_id: str) -> dict[str, Any] | None:
        """Return market metadata from RAM; SQLite is fallback-only on cache miss."""
        key = str(condition_id)
        market = self._markets_by_condition.get(key)
        if market:
            return market
        market = self.repo.latest_market(key)
        if market:
            self._cache_market(market)
        return market

    def event_freshness(
        self, condition_id: str, *, now_ms: int | None = None
    ) -> dict[str, Any]:
        market = self.market_for_condition(str(condition_id))
        if not market:
            return {"ready": False, "reason": "UNKNOWN_EVENT"}
        asset_ids = [
            str(market.get("yes_token_id") or ""),
            str(market.get("no_token_id") or ""),
        ]
        checked_ms = self._clock_ms() if now_ms is None else int(now_ms)
        ready, reason = self.order_books.event_ready(
            asset_ids, now_ms=checked_ms,
            max_age_ms=self.stale_after_seconds * 1000,
            future_tolerance_ms=self.future_tolerance_ms,
        )
        if self.status.status != "CONNECTED":
            ready, reason = False, "MARKET_WS_NOT_CONNECTED"
        if not set(asset_ids).issubset(set(self.subscribed_asset_ids or asset_ids)):
            ready, reason = False, "EVENT_NOT_SUBSCRIBED"
        ages = {
            asset: (
                checked_ms - self.order_books.books[asset].last_exchange_timestamp_ms
                if asset in self.order_books.books
                and self.order_books.books[asset].last_exchange_timestamp_ms is not None
                else None
            )
            for asset in asset_ids
        }
        arrival_latencies = {
            asset: (
                self.order_books.books[asset].receive_latency_ms
                if asset in self.order_books.books else None
            )
            for asset in asset_ids
        }
        if ready and any(
            age is not None and age > self.stale_after_seconds * 1000
            for age in ages.values()
        ):
            self.idle_ready_checks_over_threshold += 1
        return {
            "ready": ready, "reason": reason, "asset_ids": asset_ids,
            "generation": self.order_books.generation,
            "idle_exchange_age_ms": ages,
            "message_arrival_latency_ms": arrival_latencies,
            "book_versions": {
                asset: {
                    "generation": self.order_books.books[asset].generation,
                    "update_number": self.order_books.books[asset].update_number,
                    "message_hash": self.order_books.books[asset].last_message_hash,
                }
                for asset in asset_ids if asset in self.order_books.books
            },
            "checked_at_ms": checked_ms,
        }

    def _ensure_persistence_writer(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

        if self._persistence_executor is None:
            self._persistence_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="market-db-writer",
            )

        if (
            self._persistence_task is None
            or self._persistence_task.done()
        ):
            self._persistence_task = loop.create_task(
                self._persistence_writer(),
                name="market-ws-persistence-writer",
            )

        return True

    @staticmethod
    def _materialize_persistence_snapshot(
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Build sorted full L2 depth outside the event loop."""
        result = dict(snapshot)

        bid_items = result.pop(
            "_persistence_bid_items",
            None,
        )
        ask_items = result.pop(
            "_persistence_ask_items",
            None,
        )

        if bid_items is not None:
            result["bids"] = [
                {
                    "price": canonical_decimal(price),
                    "size": canonical_decimal(size),
                }
                for price, size in sorted(
                    bid_items,
                    key=lambda item: item[0],
                    reverse=True,
                )
            ]

        if ask_items is not None:
            result["asks"] = [
                {
                    "price": canonical_decimal(price),
                    "size": canonical_decimal(size),
                }
                for price, size in sorted(
                    ask_items,
                    key=lambda item: item[0],
                )
            ]

        return result

    def _persistence_batch_sync(
        self,
        snapshots: list[dict[str, Any]],
        states: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Run one complete persistence batch on one dedicated thread."""
        if self._persistence_connection is None:
            self._persistence_connection = self.repo.connect()

        conn = self._persistence_connection

        materialized = [
            self._materialize_persistence_snapshot(snapshot)
            for snapshot in snapshots
        ]

        try:
            stored = (
                self.repo.store_market_snapshots_on_connection(
                    conn,
                    materialized,
                )
                if materialized
                else []
            )

            if states:
                self.repo.set_states_on_connection(
                    conn,
                    states,
                    "market_ws",
                )

            conn.commit()
            self.persistence_batches += 1
            return stored

        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

            self.persistence_failures += 1
            raise

    def _close_persistence_connection_sync(self) -> None:
        conn = self._persistence_connection
        self._persistence_connection = None

        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _enqueue_snapshot(self, snapshot: dict[str, Any]) -> None:
        if not self._ensure_persistence_writer():
            stored = self.repo.store_market_snapshot(snapshot)
            if stored is not None:
                self.snapshots_received += 1
                if self.on_snapshot is not None:
                    self.on_snapshot(stored)
            return
        key = str(snapshot["asset_id"])
        if key in self._pending_snapshots:
            self._pending_snapshots.pop(key)
            self.snapshots_coalesced += 1
        elif len(self._pending_snapshots) >= self.persistence_queue_capacity:
            self._pending_snapshots.popitem(last=False)
            self.snapshots_dropped += 1
        self._pending_snapshots[key] = snapshot
        self.max_persistence_queue_depth = max(
            self.max_persistence_queue_depth, len(self._pending_snapshots)
        )
        self._persistence_event.set()

    def _queue_states(self, values: dict[str, str]) -> None:
        changed = {
            key: str(value) for key, value in values.items()
            if self._last_queued_state_values.get(key) != str(value)
        }
        if not changed:
            return
        new_readiness = changed.get("strategy_readiness")
        if new_readiness and new_readiness != self._readiness_state:
            transition_at = time.monotonic()
            if self._readiness_state == "NOT_READY" and self._not_ready_started_monotonic is not None:
                duration = transition_at - self._not_ready_started_monotonic
                self.not_ready_total_seconds += duration
                self.not_ready_max_seconds = max(self.not_ready_max_seconds, duration)
                self._not_ready_started_monotonic = None
            if new_readiness == "NOT_READY":
                self.not_ready_transitions += 1
                self._not_ready_started_monotonic = transition_at
            self._readiness_state = new_readiness
        self._last_queued_state_values.update(changed)
        if not self._ensure_persistence_writer():
            self.repo.set_states(changed, "market_ws")
            return
        self._pending_states.update(changed)
        self._persistence_event.set()

    async def _persistence_writer(self) -> None:
        while (
            not self._stop.is_set()
            or self._pending_snapshots
            or self._pending_states
        ):
            if (
                not self._pending_snapshots
                and not self._pending_states
            ):
                self._persistence_event.clear()

                try:
                    await asyncio.wait_for(
                        self._persistence_event.wait(),
                        0.25,
                    )
                except asyncio.TimeoutError:
                    continue

            # Small batching window. Telemetry may wait a few milliseconds;
            # market decisions never wait for this worker.
            await asyncio.sleep(0.02)

            snapshots = list(
                self._pending_snapshots.values()
            )
            states = dict(self._pending_states)

            self._pending_snapshots.clear()
            self._pending_states.clear()

            if not snapshots and not states:
                continue

            executor = self._persistence_executor

            if executor is None:
                self.persistence_failures += 1
                self.persistence_last_error = (
                    "PERSISTENCE_EXECUTOR_UNAVAILABLE"
                )
                continue

            loop = asyncio.get_running_loop()

            try:
                stored = await loop.run_in_executor(
                    executor,
                    self._persistence_batch_sync,
                    snapshots,
                    states,
                )

            except Exception as exc:
                self.persistence_last_error = (
                    f"{type(exc).__name__}:{exc}"
                )[:500]

                self._logger.exception(
                    "Market persistence batch failed"
                )

                # Requeue latest telemetry. It is still lossy/coalescible,
                # but a transient DB failure must not silently kill the
                # writer task.
                for snapshot in snapshots:
                    key = str(
                        snapshot.get("asset_id") or ""
                    )
                    if key:
                        self._pending_snapshots[key] = snapshot

                self._pending_states.update(states)

                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        0.25,
                    )
                except asyncio.TimeoutError:
                    pass

                continue

            self.persistence_last_error = ""
            self.snapshots_received += len(stored)

            if self.on_snapshot is not None:
                for snapshot in stored:
                    await asyncio.to_thread(
                        self.on_snapshot,
                        snapshot,
                    )

    def _should_persist_snapshot(self, snapshot: dict[str, Any]) -> bool:
        asset_id = str(snapshot["asset_id"])
        now = time.monotonic()
        previous = self._last_snapshot_monotonic.get(asset_id)
        if previous is not None and now - previous < self.snapshot_min_interval_seconds:
            return False
        signature = "|".join(
            str(snapshot.get(key) if snapshot.get(key) is not None else "")
            for key in (
                "best_bid",
                "best_ask",
                "best_bid_size",
                "best_ask_size",
                "book_ready",
                "generation",
                "update_number",
            )
        )
        if signature == self._last_snapshot_signature.get(asset_id):
            return False
        self._last_snapshot_signature[asset_id] = signature
        self._last_snapshot_monotonic[asset_id] = now
        return True

    def _normalize_snapshots(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        event_type = str(message.get("event_type") or message.get("type") or "").lower()
        if event_type == "market_resolved":
            condition_id = str(message.get("condition_id") or message.get("market") or "")
            market = self.repo.latest_market(condition_id) if condition_id else None
            if not market:
                return []
            winning_asset = str(message.get("winning_asset_id") or "")
            timestamp = str(message.get("timestamp") or "") or None
            received_at = now_iso()
            results = []
            for index, (asset_id, outcome) in enumerate((
                (market.get("yes_token_id"), "YES"),
                (market.get("no_token_id"), "NO"),
            )):
                if not asset_id:
                    continue
                payout = 1.0 if str(asset_id) == winning_asset else 0.0
                identity = {"message": message, "asset_id": str(asset_id), "index": index}
                results.append({
                    "condition_id": condition_id,
                    "event_id": market.get("event_id"),
                    "asset_id": str(asset_id),
                    "outcome": outcome,
                    "event_type": event_type,
                    "best_bid": payout,
                    "best_ask": payout,
                    "market_timestamp": timestamp,
                    "received_at": received_at,
                    "latency_ms": self._latency_ms(timestamp),
                    "source": "POLYMARKET_MARKET_WS",
                    "message_hash": __import__("hashlib").sha256(
                        json.dumps(identity, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                    "raw_message": identity,
                })
            return results
        if event_type not in {"book", "best_bid_ask", "price_change"}:
            return []
        items = message.get("price_changes") if event_type == "price_change" else [message]
        if not isinstance(items, list):
            return []
        snapshots: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            asset_id = str(item.get("asset_id") or message.get("asset_id") or "")
            market = self.repo.market_for_asset(asset_id) if asset_id else None
            if not market:
                continue
            bids = message.get("bids") if event_type == "book" else []
            asks = message.get("asks") if event_type == "book" else []
            bids = bids if isinstance(bids, list) else []
            asks = asks if isinstance(asks, list) else []
            best_bid, best_bid_size = self._best_level(bids, highest=True)
            best_ask, best_ask_size = self._best_level(asks, highest=False)
            if event_type != "book":
                best_bid = self._number(item.get("best_bid"))
                best_ask = self._number(item.get("best_ask"))
            timestamp = str(message.get("timestamp") or item.get("timestamp") or "") or None
            received_at = now_iso()
            latency_ms = self._latency_ms(timestamp)
            outcome = (
                "YES" if str(market.get("yes_token_id")) == asset_id
                else "NO" if str(market.get("no_token_id")) == asset_id
                else None
            )
            raw_with_identity = {"message": message, "asset_id": asset_id, "index": index}
            snapshots.append({
                "condition_id": market["condition_id"],
                "event_id": market.get("event_id"),
                "asset_id": asset_id,
                "outcome": outcome,
                "event_type": event_type,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "best_bid_size": best_bid_size,
                "best_ask_size": best_ask_size,
                "bids": bids,
                "asks": asks,
                "market_timestamp": timestamp,
                "received_at": received_at,
                "latency_ms": latency_ms,
                "source": "POLYMARKET_MARKET_WS",
                "message_hash": __import__("hashlib").sha256(
                    json.dumps(raw_with_identity, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "raw_message": raw_with_identity,
            })
        return snapshots

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _best_level(cls, levels: list[Any], *, highest: bool) -> tuple[float | None, float | None]:
        parsed = [
            (cls._number(level.get("price")), cls._number(level.get("size")))
            for level in levels if isinstance(level, dict)
        ]
        valid = [(price, size) for price, size in parsed if price is not None and (size or 0) > 0]
        if not valid:
            return None, None
        chooser = max if highest else min
        return chooser(valid, key=lambda level: level[0])

    @staticmethod
    def _latency_ms(timestamp: str | None) -> int | None:
        if not timestamp:
            return None
        try:
            source_ms = int(float(timestamp))
            if source_ms < 10_000_000_000:
                source_ms *= 1000
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            return max(0, now_ms - source_ms)
        except (TypeError, ValueError):
            return None

    def mark_disconnect(self, error: str = "") -> None:
        self._logger.warning(
            "MARKET_WS_DISCONNECTED generation=%s reason=%s",
            self._connection_generation, error or "UNKNOWN",
        )
        self.status.status = "DISCONNECTED"
        self.status.reconnect_attempts += 1
        self.status.stale = True
        self.status.error = error or None
        self.order_books.mark_not_ready("WS_DISCONNECTED")
        # Serialize disconnect state through the same coalescing writer so an
        # in-flight READY update can never commit after DISCONNECTED.
        self._queue_states({
            "market_ws_status": "DISCONNECTED",
            "strategy_readiness": "NOT_READY",
            "strategy_block_reason": "WS_DISCONNECTED",
        })

    def health(self) -> dict[str, Any]:
        stale = True
        if self.status.last_message_at:
            dt = datetime.fromisoformat(self.status.last_message_at.replace("Z", "+00:00"))
            stale = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() > self.stale_after_seconds
        self.status.stale = stale
        return {
            **self.status.__dict__,
            "subscribed_asset_ids": list(self.subscribed_asset_ids),
            "messages_received": self.messages_received,
            "snapshots_received": self.snapshots_received,
            "last_ping_at": self.last_ping_at,
            "last_pong_at": self.last_pong_at,
            "subscription_status": (
                "SUBSCRIBED" if self.subscribed_asset_ids else "NOT_SUBSCRIBED"
            ),
            "books": {
                asset: {
                    "ready": book.ready,
                    "reason": book.reason,
                    "generation": book.generation,
                    "update_number": book.update_number,
                    "exchange_timestamp_ms": book.last_exchange_timestamp_ms,
                    "exchange_age_ms": (
                        self._clock_ms() - book.last_exchange_timestamp_ms
                        if book.last_exchange_timestamp_ms is not None else None
                    ),
                    "receive_latency_ms": book.receive_latency_ms,
                    "best_bid": canonical_decimal(book.best_bid) if book.best_bid is not None else None,
                    "best_ask": canonical_decimal(book.best_ask) if book.best_ask is not None else None,
                }
                for asset, book in self.order_books.books.items()
            },
            "raw_payload_persistence": self.persist_raw_payloads,
            "snapshot_min_interval_seconds": self.snapshot_min_interval_seconds,
            "freshness_threshold_ms": self.stale_after_seconds * 1000,
            "future_tolerance_ms": self.future_tolerance_ms,
            "exchange_age_ms": self.last_exchange_age_ms,
            "receive_latency_ms": self.last_receive_latency_ms,
            "message_processing_ms": self.last_message_processing_ms,
            "max_message_processing_ms": self.max_message_processing_ms,
            "persistence_queue_depth": len(self._pending_snapshots),
            "max_persistence_queue_depth": self.max_persistence_queue_depth,
            "persistence_batches": self.persistence_batches,
            "persistence_failures": self.persistence_failures,
            "persistence_last_error": self.persistence_last_error,
            "persistence_connection_open": (
                self._persistence_connection is not None
            ),
            "ingress_queue_capacity": self.ingress_queue_capacity,
            "ingress_queue_depth": (
                self._ingress_queue.qsize() if self._ingress_queue is not None else 0
            ),
            "max_ingress_queue_depth": self.max_ingress_queue_depth,
            "ingress_frames_enqueued": self.ingress_frames_enqueued,
            "ingress_frames_dequeued": self.ingress_frames_dequeued,
            "ingress_queue_saturations": self.ingress_queue_saturations,
            "ingress_resyncs": self.ingress_resyncs,
            "ingress_resync_reasons": dict(self.ingress_resync_reasons),
            "ingress_market_frames_discarded": self.ingress_market_frames_discarded,
            "ingress_critical_frames_preserved": self.ingress_critical_frames_preserved,
            "unsubscribed_market_frames_ignored": self.unsubscribed_market_frames_ignored,
            "unsubscribed_asset_counts": dict(
                sorted(
                    self.unsubscribed_asset_counts.items(),
                    key=lambda item: item[1], reverse=True,
                )[:32]
            ),
            "snapshots_coalesced": self.snapshots_coalesced,
            "snapshots_dropped": self.snapshots_dropped,
            "dynamic_subscriptions": self.dynamic_subscriptions,
            "dynamic_subscription_fallbacks": self.dynamic_subscription_fallbacks,
            "readiness_state": self._readiness_state,
            "not_ready_transitions": self.not_ready_transitions,
            "not_ready_total_seconds": self.not_ready_total_seconds + (
                time.monotonic() - self._not_ready_started_monotonic
                if self._readiness_state == "NOT_READY"
                and self._not_ready_started_monotonic is not None else 0.0
            ),
            "not_ready_max_seconds": max(
                self.not_ready_max_seconds,
                time.monotonic() - self._not_ready_started_monotonic
                if self._readiness_state == "NOT_READY"
                and self._not_ready_started_monotonic is not None else 0.0
            ),
            "rejected_frames": self.rejected_frames,
            "out_of_order_frames": self.out_of_order_frames,
            "rejection_reasons": dict(self.rejection_reasons),
            "idle_ready_checks_over_threshold": self.idle_ready_checks_over_threshold,
        }


class UserWebSocketManager:
    STATES = {"DISABLED", "CONNECTING", "AUTHENTICATING", "CONNECTED", "STALE", "RECONNECTING", "AUTH_FAILED", "ERROR", "STOPPED"}
    AUTH_KEYS = ("POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE")

    def __init__(self, repo: LiveRepository, stale_after_seconds: int = 25,
                 reconciliation: Callable[[], Awaitable[Any]] | None = None):
        self.repo, self.stale_after_seconds = repo, stale_after_seconds
        self.status = WebSocketStatus(channel="user", status="DISABLED")
        self.connected_at = self.last_ping_at = self.last_pong_at = None
        self.messages_received = self.order_events_received = self.trade_events_received = 0
        self.subscribed_condition_ids: list[str] = []
        self._task = None
        self._stop = asyncio.Event()
        self._ws = None
        self._lock = asyncio.Lock()
        self._reconciliation = reconciliation
        self._authenticated_signal = False
        self._silent_failures = 0
        self._logger = logging.getLogger("live.user_ws")

    def subscription_message(self, condition_ids, auth_payload=None):
        payload = {"type": "user", "markets": list(dict.fromkeys(condition_ids))}
        if auth_payload:
            payload["auth"] = auth_payload
        return payload

    def dynamic_subscription_message(self, condition_ids, operation="subscribe"):
        if operation not in {"subscribe", "unsubscribe"}:
            raise ValueError("invalid subscription operation")
        return {"operation": operation, "markets": list(dict.fromkeys(condition_ids))}

    def credentials(self):
        values = [os.getenv(name, "").strip() for name in self.AUTH_KEYS]
        return {"apiKey": values[0], "secret": values[1], "passphrase": values[2]} if all(values) else None

    async def start(self, url):
        async with self._lock:
            if self._task and not self._task.done():
                return
            self._stop.clear()
            self._task = asyncio.create_task(self.run(url), name="polymarket-user-ws")

    async def stop(self):
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()
        if self._task:
            try:
                await asyncio.wait_for(self._task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._set_state("STOPPED")

    async def run(self, url, connect=None):
        creds = self.credentials()
        if not creds:
            self._set_state("AUTH_FAILED", "User WebSocket credentials are missing")
            return
        try:
            import websockets
        except Exception:
            self._set_state("ERROR", "websockets package unavailable")
            return
        connector, attempt = connect or websockets.connect, 0
        while not self._stop.is_set():
            condition_ids = self.repo.user_ws_condition_ids()
            if not condition_ids:
                self._set_state("DISABLED", "No managed BTC 5m condition IDs available")
                await asyncio.sleep(2)
                continue
            self._set_state("CONNECTING" if attempt == 0 else "RECONNECTING")
            try:
                async with connector(url, ping_interval=None, close_timeout=5) as ws:
                    self._ws = ws
                    self._authenticated_signal = False
                    self._set_state("AUTHENTICATING")
                    await ws.send(json.dumps(self.subscription_message(condition_ids, creds)))
                    await ws.send("PING")
                    self.last_ping_at = now_iso()
                    self.subscribed_condition_ids, self.connected_at = condition_ids, now_iso()
                    self._set_state("CONNECTED")
                    attempt = 0
                    if self._reconciliation:
                        await self._reconciliation()
                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    subscriptions = asyncio.create_task(self._subscription_loop(ws))
                    try:
                        while not self._stop.is_set():
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_after_seconds)
                            except asyncio.TimeoutError:
                                self._set_state("STALE", "User WebSocket receive timeout")
                                raise ConnectionError("stale connection")
                            await self._receive(raw)
                    finally:
                        heartbeat.cancel()
                        subscriptions.cancel()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._ws = None
                error = self._safe_error(exc)
                if self._is_auth_error(error):
                    self._set_state("AUTH_FAILED", "User WebSocket authentication failed")
                    return
                attempt += 1
                if not self._authenticated_signal:
                    self._silent_failures += 1
                    if self._silent_failures >= 2:
                        self._set_state("AUTH_FAILED", "User WebSocket closed before authentication acknowledgement")
                        return
                else:
                    self._silent_failures = 0
                self.status.reconnect_attempts += 1
                self._set_state("RECONNECTING", error)
                try:
                    await asyncio.wait_for(self._stop.wait(), min(30.0, 2 ** min(attempt, 5)) + random.random())
                except asyncio.TimeoutError:
                    pass
        self._set_state("STOPPED")

    async def _heartbeat(self, ws):
        while not self._stop.is_set():
            await asyncio.sleep(10)
            await ws.send("PING")
            self.last_ping_at = now_iso()

    async def _subscription_loop(self, ws):
        while not self._stop.is_set():
            await asyncio.sleep(2)
            wanted = self.repo.user_ws_condition_ids()
            add = [x for x in wanted if x not in self.subscribed_condition_ids]
            remove = [x for x in self.subscribed_condition_ids if x not in wanted]
            if add:
                await ws.send(json.dumps(self.dynamic_subscription_message(add, "subscribe")))
            if remove:
                await ws.send(json.dumps(self.dynamic_subscription_message(remove, "unsubscribe")))
            self.subscribed_condition_ids = wanted

    async def _receive(self, raw):
        self.status.last_message_at, self.status.stale = now_iso(), False
        if raw == "PONG" or raw == b"PONG":
            self._authenticated_signal = True
            self._silent_failures = 0
            self.last_pong_at = now_iso()
            self._persist_state()
            return
        try:
            payload = json.loads(raw)
        except Exception:
            return
        for message in payload if isinstance(payload, list) else [payload]:
            if isinstance(message, dict):
                self._authenticated_signal = True
                self._silent_failures = 0
                if self._is_auth_error(json.dumps(message)):
                    raise PermissionError("authentication failed")
                self.process_message(message)

    def process_message(self, message):
        normalized = self.normalize(message)
        stored = self.repo.store_ws_event("user", normalized, "processed")
        self.messages_received += 1
        if stored and normalized.get("event_type") == "order":
            self.order_events_received += 1
        if stored and normalized.get("event_type") == "trade":
            self.trade_events_received += 1
        if stored and normalized.get("event_type") in {"order", "trade"} and self._reconciliation:
            try:
                asyncio.get_running_loop().create_task(self._reconciliation())
            except RuntimeError:
                pass
        self.status.status, self.status.last_message_at, self.status.stale = "CONNECTED", now_iso(), False
        self._persist_state()
        return stored

    def normalize(self, message):
        clean = self.sanitize(message)
        event_type = str(clean.get("event_type") or clean.get("type") or "").lower()
        status = str(clean.get("status") or clean.get("event") or clean.get("message_type") or "").upper()
        condition_id, asset_id = clean.get("market") or clean.get("condition_id"), clean.get("asset_id") or clean.get("token_id")
        original = self._number(clean.get("original_size") or clean.get("size") or clean.get("maker_amount"))
        matched = self._number(clean.get("matched_size") or clean.get("size_matched") or clean.get("taker_amount"))
        remaining = self._number(clean.get("remaining_size"))
        if remaining is None and original is not None and matched is not None:
            remaining = max(0.0, original - matched)
        order_id = clean.get("order_id") or (clean.get("id") if event_type == "order" else None)
        trade_id = clean.get("trade_id") or (clean.get("id") if event_type == "trade" else None)
        return {"event_type": event_type, "message_type": status, "message_status": status,
                "order_id": order_id, "trade_id": trade_id, "condition_id": condition_id, "asset_id": asset_id,
                "outcome": clean.get("outcome") or self.repo.outcome_for_asset(condition_id, asset_id),
                "side": str(clean.get("side") or "").upper() or None, "price": self._number(clean.get("price")),
                "original_size": original, "matched_size": matched, "remaining_size": remaining,
                "liquidity_role": clean.get("trader_side") or clean.get("liquidity_role"),
                "transaction_hash": clean.get("transaction_hash") or clean.get("transactionHash"),
                "event_timestamp": str(clean.get("timestamp") or clean.get("created_at") or clean.get("updated_at") or "") or None,
                "correlation": {k: clean.get(k) for k in ("owner", "maker_order_id", "taker_order_id") if clean.get(k)},
                "raw": clean}

    @classmethod
    def sanitize(cls, value):
        if isinstance(value, dict):
            return {key: ("[REDACTED]" if any(m in key.lower() for m in ("secret", "passphrase", "apikey", "api_key", "private_key", "auth")) else cls.sanitize(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [cls.sanitize(item) for item in value]
        return value

    @staticmethod
    def _number(value):
        try:
            return float(value) if value is not None and value != "" else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_auth_error(text):
        lowered = text.lower()
        return any(x in lowered for x in ("auth failed", "authentication failed", "invalid api", "unauthorized", "forbidden"))

    @staticmethod
    def _safe_error(exc):
        text = f"{type(exc).__name__}: {exc}"
        for name in UserWebSocketManager.AUTH_KEYS:
            value = os.getenv(name, "")
            if value:
                text = text.replace(value, "[REDACTED]")
        return text[:500]

    def _set_state(self, state, error=None):
        self.status.status = state if state in self.STATES else "ERROR"
        self.status.error = error
        self.status.stale = state in {"STALE", "ERROR", "AUTH_FAILED", "STOPPED"}
        self._persist_state()

    def _persist_state(self):
        self.repo.set_state("user_ws_status", self.status.status, "user_ws")
        self.repo.set_state("user_ws_health", json.dumps(self.health(), sort_keys=True), "user_ws")

    def health(self):
        return {"connected": self.status.status == "CONNECTED", "status": self.status.status,
                "connected_at": self.connected_at, "last_message_at": self.status.last_message_at,
                "last_ping_at": self.last_ping_at, "last_pong_at": self.last_pong_at,
                "last_error": self.status.error, "reconnect_count": self.status.reconnect_attempts,
                "reconnect_attempts": self.status.reconnect_attempts,
                "subscribed_condition_ids": list(self.subscribed_condition_ids),
                "messages_received": self.messages_received, "order_events_received": self.order_events_received,
                "trade_events_received": self.trade_events_received, "stale": self.status.stale}

"""BTC/USD reference price feed (Chainlink via Polymarket RTDS).

The feed runs as its own asyncio task. Per tick it only parses the message
and updates in-memory state; it never touches the database. Every read API
(``current``, ``open_price``, ``close_price``, ``price_at``) is a pure
in-memory lookup that is safe to call from the entry hot path.

Window boundaries follow the market slug ``btc-updown-5m-<start_epoch>``:
the window open price is the first tick whose payload timestamp is
``>= window_start`` and the close price is the first tick ``>= window_end``
(``window_start + 300``). One boundary map serves both: the close of one
window is the open of the next.

The same connection also carries Chainlink's 60-second TWAP stream (the
market's resolution source), kept in a separate state. Polymarket's
"price to beat" is the TWAP value at the window start (verified against
Gamma ``eventMetadata.priceToBeat``), so the gate and the deal snapshots
measure the spot price against ``reference_open`` (the TWAP open), never
against the spot open.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import random
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

WINDOW_SECONDS = 300
WINDOW_MS = WINDOW_SECONDS * 1000
EXPECTED_TICK_INTERVAL_MS = 1000
# A tick arriving more than 1.5 intervals after the previous one marks a gap.
GAP_THRESHOLD_MS = 1500
BUFFER_RETENTION_MS = 15 * 60 * 1000
BOUNDARY_RETENTION_MS = 24 * 60 * 60 * 1000
WINDOW_STATS_RETENTION = 288

RTDS_URL = "wss://ws-live-data.polymarket.com"
RTDS_TOPIC = "crypto_prices_chainlink"
RTDS_TWAP_TOPIC = "crypto_prices_twap_sixty"
# The spot snapshot arrives as topic "crypto_prices"; the TWAP snapshot keeps
# its own topic and carries ``window_s`` in the payload.
RTDS_SPOT_SNAPSHOT_TOPIC = "crypto_prices"
RTDS_SYMBOL = "btc/usd"
RTDS_PING_INTERVAL_SECONDS = 5.0
# full_accuracy_value is an integer scaled by 1e18 (Chainlink Data Streams).
FULL_ACCURACY_SCALE = Decimal(10) ** 18
FULL_ACCURACY_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class BtcTick:
    price: Decimal
    ts_ms: int
    received_ms: int


def window_start_from_event_id(event_id: str) -> int | None:
    """Window start epoch (seconds) from ``btc-updown-5m-<start>``."""
    try:
        return int(str(event_id).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None


def side_outcome_map(market: dict[str, Any]) -> dict[str, str] | None:
    """{"YES": "UP"|"DOWN", "NO": ...} from the market's Gamma outcome labels.

    Discovery stores the labels in ``raw_market_info``. Returns None when they
    are missing or are not exactly one Up and one Down; callers fail closed.
    """
    info = market.get("raw_market_info")
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except ValueError:
            return None
    if not isinstance(info, dict):
        return None
    yes = str(info.get("yes_outcome_label") or "").strip().upper()
    no = str(info.get("no_outcome_label") or "").strip().upper()
    if {yes, no} != {"UP", "DOWN"}:
        return None
    return {"YES": yes, "NO": no}


def rtds_subscribe_message() -> str:
    # RTDS compares the ``filters`` string literally. ``{"symbol": "btc/usd"}``
    # (with a space) is accepted silently and yields one snapshot followed by
    # no updates at all, so both levels must be serialized compactly.
    filters = json.dumps({"symbol": RTDS_SYMBOL}, separators=(",", ":"))
    return json.dumps(
        {
            "action": "subscribe",
            "subscriptions": [
                {"topic": RTDS_TOPIC, "type": "*", "filters": filters},
                {"topic": RTDS_TWAP_TOPIC, "type": "*", "filters": filters},
            ],
        },
        separators=(",", ":"),
    )


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _tick_price(payload: dict[str, Any]) -> tuple[Decimal | None, bool]:
    """Price from full_accuracy_value when consistent, else from value.

    Returns ``(price, precision_mismatch)``.
    """
    value = _decimal(payload.get("value"))
    full = payload.get("full_accuracy_value")
    if full in (None, ""):
        return value, False
    try:
        scaled = Decimal(str(full)) / FULL_ACCURACY_SCALE
    except (InvalidOperation, ValueError):
        return value, True
    if not scaled.is_finite() or scaled <= 0:
        return value, True
    if value is not None and abs(scaled - value) > FULL_ACCURACY_TOLERANCE:
        return value, True
    return scaled, False


def parse_rtds_message(raw: Any) -> dict[str, Any]:
    """Parse one RTDS frame.

    Returns ``{"kind": "update"|"snapshot"|"ignore", "stream": "spot"|"twap",
    "ticks": [(ts_ms, price)], "precision_mismatch": int}``. Floats are parsed
    as Decimal so the snapshot's float ``value`` keeps its printed digits.
    """
    result: dict[str, Any] = {
        "kind": "ignore", "stream": "spot", "ticks": [], "precision_mismatch": 0,
    }
    if not raw or not isinstance(raw, (str, bytes)):
        return result
    try:
        message = json.loads(raw, parse_float=Decimal)
    except ValueError:
        return result
    if not isinstance(message, dict):
        return result
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return result
    symbol = str(payload.get("symbol") or "").lower()
    if symbol and symbol != RTDS_SYMBOL:
        return result
    topic = message.get("topic")
    if topic == RTDS_TWAP_TOPIC or "window_s" in payload:
        stream = "twap"
    elif topic in (RTDS_TOPIC, RTDS_SPOT_SNAPSHOT_TOPIC):
        stream = "spot"
    else:
        return result
    result["stream"] = stream
    if isinstance(payload.get("data"), list):
        ticks = []
        mismatches = 0
        for item in payload["data"]:
            if not isinstance(item, dict):
                continue
            price, mismatch = _tick_price(item)
            ts = item.get("timestamp")
            if price is None or not isinstance(ts, int):
                continue
            mismatches += int(mismatch)
            ticks.append((ts, price))
        result.update(kind="snapshot", ticks=ticks, precision_mismatch=mismatches)
        return result
    if topic not in (RTDS_TOPIC, RTDS_TWAP_TOPIC):
        return result
    ts = payload.get("timestamp")
    price, mismatch = _tick_price(payload)
    if price is None or not isinstance(ts, int):
        return result
    result.update(kind="update", ticks=[(ts, price)], precision_mismatch=int(mismatch))
    return result


class BtcReferenceState:
    """In-memory tick store. No I/O; every method is O(log n) or O(1)."""

    def __init__(
        self,
        *,
        open_tick_max_offset_ms: int = 2000,
        clock_ms: Callable[[], int] | None = None,
    ):
        self.open_tick_max_offset_ms = int(open_tick_max_offset_ms)
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._ticks: list[BtcTick] = []
        self._keys: list[int] = []
        self._latest: BtcTick | None = None
        self._last_live_ts: int | None = None
        self._boundaries: dict[int, BtcTick] = {}
        self._window_stats: dict[int, dict[str, int]] = {}
        self.ticks_total = 0
        self.duplicates = 0
        self.out_of_order = 0
        self.backfilled = 0
        self.gaps = 0
        self.missing_ticks = 0
        self.precision_mismatches = 0

    # ------------------------------------------------------------- ingest
    def ingest(self, ts_ms: int, price: Decimal, *, received_ms: int | None = None,
               from_snapshot: bool = False) -> bool:
        received = int(received_ms if received_ms is not None else self._clock_ms())
        ts_ms = int(ts_ms)
        index = bisect.bisect_left(self._keys, ts_ms)
        if index < len(self._keys) and self._keys[index] == ts_ms:
            if not from_snapshot:
                self.duplicates += 1
            return False
        if self._keys and ts_ms < self._keys[0] and (
            self._keys[-1] - ts_ms > BUFFER_RETENTION_MS
        ):
            return False
        tick = BtcTick(price=price, ts_ms=ts_ms, received_ms=received)
        if from_snapshot:
            self.backfilled += 1
        elif self._last_live_ts is not None and ts_ms < self._last_live_ts:
            self.out_of_order += 1
        elif self._last_live_ts is not None:
            step = ts_ms - self._last_live_ts
            if step > GAP_THRESHOLD_MS:
                self.gaps += 1
                self.missing_ticks += max(1, round(step / EXPECTED_TICK_INTERVAL_MS) - 1)
                self._window_stat(ts_ms // 1000)["gaps"] += 1
        if not from_snapshot:
            self.ticks_total += 1
            if self._last_live_ts is None or ts_ms > self._last_live_ts:
                self._last_live_ts = ts_ms
        self._keys.insert(index, ts_ms)
        self._ticks.insert(index, tick)
        if self._latest is None or ts_ms > self._latest.ts_ms:
            self._latest = tick
        self._note_boundary(tick)
        self._trim()
        return True

    def note_stall(self, at_ms: int | None = None) -> None:
        moment = int(at_ms if at_ms is not None else self._clock_ms())
        self._window_stat(moment // 1000)["stalls"] += 1

    def _window_stat(self, epoch_seconds: int) -> dict[str, int]:
        start = epoch_seconds // WINDOW_SECONDS * WINDOW_SECONDS
        stats = self._window_stats.get(start)
        if stats is None:
            stats = self._window_stats[start] = {"gaps": 0, "stalls": 0}
            if len(self._window_stats) > WINDOW_STATS_RETENTION:
                del self._window_stats[min(self._window_stats)]
        return stats

    def _note_boundary(self, tick: BtcTick) -> None:
        boundary_ms = tick.ts_ms // WINDOW_MS * WINDOW_MS
        current = self._boundaries.get(boundary_ms)
        if current is None or tick.ts_ms < current.ts_ms:
            self._boundaries[boundary_ms] = tick

    def _trim(self) -> None:
        newest = self._keys[-1]
        cutoff = bisect.bisect_left(self._keys, newest - BUFFER_RETENTION_MS)
        if cutoff:
            del self._keys[:cutoff]
            del self._ticks[:cutoff]
        if len(self._boundaries) > WINDOW_STATS_RETENTION + 4:
            floor = newest - BOUNDARY_RETENTION_MS
            for key in [key for key in self._boundaries if key < floor]:
                del self._boundaries[key]

    # -------------------------------------------------------------- reads
    def current(self) -> BtcTick | None:
        return self._latest

    def boundary_status(self, boundary_epoch_seconds: int) -> tuple[BtcTick | None, str]:
        """First tick at/after a 5-minute boundary, with OK/NO_TICK/TICK_TOO_LATE."""
        boundary_ms = int(boundary_epoch_seconds) * 1000
        tick = self._boundaries.get(boundary_ms)
        if tick is None:
            return None, "NO_TICK"
        if tick.ts_ms - boundary_ms > self.open_tick_max_offset_ms:
            return None, "TICK_TOO_LATE"
        return tick, "OK"

    def open_price(self, window_start: int) -> BtcTick | None:
        return self.boundary_status(window_start)[0]

    def close_price(self, window_end: int) -> BtcTick | None:
        return self.boundary_status(window_end)[0]

    def price_at(self, ts_ms: int) -> BtcTick | None:
        """Newest buffered tick with payload timestamp <= ts_ms."""
        index = bisect.bisect_right(self._keys, int(ts_ms)) - 1
        if index < 0:
            return None
        return self._ticks[index]

    def window_stats(self, window_start: int) -> dict[str, int]:
        stats = self._window_stats.get(int(window_start)) or {}
        return {"gaps": int(stats.get("gaps", 0)), "stalls": int(stats.get("stalls", 0))}

    def boundary_windows(self) -> list[int]:
        return sorted(key // 1000 for key in self._boundaries)

    def buffer_size(self) -> int:
        return len(self._ticks)


class BtcReferenceProvider:
    """Interface for a BTC reference feed. Reads never perform I/O."""

    name = "base"

    def __init__(self, *, open_tick_max_offset_ms: int = 2000,
                 clock_ms: Callable[[], int] | None = None):
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.state = BtcReferenceState(
            open_tick_max_offset_ms=open_tick_max_offset_ms, clock_ms=self._clock_ms,
        )
        # Chainlink 60s TWAP (resolution source). Its window-start value is
        # the market's price to beat (see reference_open).
        self.twap_state = BtcReferenceState(
            open_tick_max_offset_ms=open_tick_max_offset_ms, clock_ms=self._clock_ms,
        )
        self.connected = False
        self.reconnects = 0
        self.stalls = 0
        self.last_error = ""
        self.running = False

    def now_ms(self) -> int:
        return int(self._clock_ms())

    async def run(self, stop: asyncio.Event) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def current(self) -> BtcTick | None:
        return self.state.current()

    def open_price(self, window_start: int) -> BtcTick | None:
        return self.state.open_price(window_start)

    def close_price(self, window_end: int) -> BtcTick | None:
        return self.state.close_price(window_end)

    def price_at(self, ts_ms: int) -> BtcTick | None:
        return self.state.price_at(ts_ms)

    def twap_current(self) -> BtcTick | None:
        return self.twap_state.current()

    def twap_price_at(self, ts_ms: int) -> BtcTick | None:
        return self.twap_state.price_at(ts_ms)

    def twap_boundary(self, boundary_epoch_seconds: int) -> BtcTick | None:
        return self.twap_state.boundary_status(boundary_epoch_seconds)[0]

    def reference_open(self, window_start: int) -> BtcTick | None:
        """The market's price to beat: the TWAP tick at the window start.

        The spot open can differ from it by tens of dollars (median ~$12,
        p90 ~$44 on live data), enough to flip the side. None when the TWAP
        boundary tick is missing; callers fail closed.
        """
        return self.twap_boundary(window_start)

    def health(self) -> dict[str, Any]:
        now = self.now_ms()
        latest = self.state.current()
        twap = self.twap_state.current()
        return {
            "provider": self.name,
            "running": self.running,
            "connected": self.connected,
            "last_tick_age_ms": (now - latest.received_ms) if latest else None,
            "last_payload_age_ms": (now - latest.ts_ms) if latest else None,
            "reconnects": self.reconnects,
            "stalls": self.stalls,
            "gaps": self.state.gaps,
            "missing_ticks": self.state.missing_ticks,
            "ticks_total": self.state.ticks_total,
            "duplicates": self.state.duplicates,
            "out_of_order": self.state.out_of_order,
            "backfilled": self.state.backfilled,
            "precision_mismatches": self.state.precision_mismatches,
            "buffer_size": self.state.buffer_size(),
            "twap_ticks_total": self.twap_state.ticks_total,
            "twap_last_payload_age_ms": (now - twap.ts_ms) if twap else None,
            "twap_gaps": self.twap_state.gaps,
            "last_error": self.last_error,
        }


class RtdsChainlinkProvider(BtcReferenceProvider):
    """Public Polymarket RTDS, topics crypto_prices_chainlink (spot) and
    crypto_prices_twap_sixty (60s TWAP), btc/usd. No credentials."""

    name = "rtds"

    def __init__(
        self,
        *,
        url: str = RTDS_URL,
        stall_seconds: float = 10.0,
        open_tick_max_offset_ms: int = 2000,
        ping_interval_seconds: float = RTDS_PING_INTERVAL_SECONDS,
        connect: Callable[..., Any] | None = None,
        clock_ms: Callable[[], int] | None = None,
        max_backoff_seconds: float = 30.0,
        logger: logging.Logger | None = None,
    ):
        super().__init__(open_tick_max_offset_ms=open_tick_max_offset_ms, clock_ms=clock_ms)
        self.url = url
        self.stall_seconds = float(stall_seconds)
        self.ping_interval_seconds = float(ping_interval_seconds)
        self.max_backoff_seconds = float(max_backoff_seconds)
        self._connect = connect
        self._logger = logger or logging.getLogger(__name__)

    def _open(self) -> Any:
        if self._connect is not None:
            return self._connect(self.url)
        import websockets

        return websockets.connect(
            self.url, open_timeout=10, close_timeout=2,
            ping_interval=None, max_size=2 ** 20,
        )

    def handle_raw(self, raw: Any) -> int:
        """Apply one frame to state. Returns the number of new live spot ticks.

        Only spot ticks count as progress for the stall watchdog: the gate
        depends on spot, and a silent spot stream must reconnect even while
        TWAP frames keep arriving.
        """
        parsed = parse_rtds_message(raw)
        if parsed["kind"] == "ignore":
            return 0
        received = self.now_ms()
        twap = parsed["stream"] == "twap"
        state = self.twap_state if twap else self.state
        state.precision_mismatches += parsed["precision_mismatch"]
        snapshot = parsed["kind"] == "snapshot"
        added = 0
        for ts_ms, price in parsed["ticks"]:
            if state.ingest(ts_ms, price, received_ms=received, from_snapshot=snapshot):
                added += 0 if snapshot or twap else 1
        return added

    async def _session(self, stop: asyncio.Event) -> bool:
        """One connection. Returns True when at least one live tick arrived."""
        got_tick = False
        async with self._open() as ws:
            self.connected = True
            await ws.send(rtds_subscribe_message())
            last_progress = time.monotonic()
            last_ping = last_progress
            while not stop.is_set():
                now = time.monotonic()
                if now - last_ping >= self.ping_interval_seconds:
                    await ws.send("PING")
                    last_ping = now
                if now - last_progress > self.stall_seconds:
                    self.stalls += 1
                    self.state.note_stall()
                    self.last_error = "STALL"
                    self._logger.warning(
                        "BTC_FEED_STALL provider=%s silent_s=%.1f", self.name, now - last_progress,
                    )
                    return got_tick
                timeout = max(
                    0.05,
                    min(
                        self.ping_interval_seconds - (now - last_ping),
                        self.stall_seconds - (now - last_progress),
                        1.0,
                    ),
                )
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout)
                except asyncio.TimeoutError:
                    continue
                if raw == "PONG":
                    continue
                if self.handle_raw(raw):
                    got_tick = True
                    last_progress = time.monotonic()
        return got_tick

    async def run(self, stop: asyncio.Event) -> None:
        self.running = True
        attempt = 0
        try:
            while not stop.is_set():
                try:
                    if await self._session(stop):
                        attempt = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                    self._logger.warning("BTC_FEED_ERROR provider=%s %s", self.name, self.last_error)
                finally:
                    self.connected = False
                if stop.is_set():
                    break
                delay = min(self.max_backoff_seconds, 2 ** min(attempt, 5)) * random.uniform(0.5, 1.0)
                attempt += 1
                self.reconnects += 1
                try:
                    await asyncio.wait_for(stop.wait(), delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.running = False
            self.connected = False


class PolyBoltProvider(BtcReferenceProvider):
    """Placeholder for wss://ws-live-v2.polymarket.com (price.crypto).

    Not implemented: the endpoint answered HTTP 503 during discovery, so its
    message format is unverified. It never produces ticks, which keeps an
    enforcing gate closed if it is selected by mistake.
    """

    name = "polybolt"

    async def run(self, stop: asyncio.Event) -> None:
        self.last_error = "NOT_IMPLEMENTED"
        self.running = True
        try:
            await stop.wait()
        finally:
            self.running = False


def build_btc_reference_provider(config: Any) -> BtcReferenceProvider:
    provider = str(getattr(config, "btc_feed_provider", "rtds") or "rtds").lower()
    offset = int(getattr(config, "btc_open_tick_max_offset_ms", 2000))
    if provider == "polybolt":
        return PolyBoltProvider(open_tick_max_offset_ms=offset)
    return RtdsChainlinkProvider(
        stall_seconds=float(getattr(config, "btc_feed_stall_seconds", 10)),
        open_tick_max_offset_ms=offset,
    )

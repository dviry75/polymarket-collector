"""BTC reference feed: RTDS parsing, boundary prices, gaps and the stall watchdog."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from live.btc_reference_feed import (
    BtcReferenceState,
    RtdsChainlinkProvider,
    build_btc_reference_provider,
    parse_rtds_message,
    rtds_subscribe_message,
    window_start_from_event_id,
)

FIXTURE = Path(__file__).parent / "fixtures" / "rtds_chainlink_sample.jsonl"
BOUNDARY = 1793295600  # 2026-10-29 17:40:00 UTC, observed in the fixture


def _fixture_frames():
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    return [row["data"] for row in sorted(rows, key=lambda row: row["recv_ms"])]


def test_subscribe_message_is_compact():
    message = rtds_subscribe_message()
    assert ": " not in message and ", " not in message
    subscription = json.loads(message)["subscriptions"][0]
    # The server compares this string literally; a space silences the stream.
    assert subscription["filters"] == '{"symbol":"btc/usd"}'
    assert subscription["topic"] == "crypto_prices_chainlink"
    twap = json.loads(message)["subscriptions"][1]
    assert twap["topic"] == "crypto_prices_twap_sixty"
    assert twap["filters"] == '{"symbol":"btc/usd"}'


def test_parse_real_update_uses_full_accuracy_value():
    frame = next(f for f in _fixture_frames() if '"type":"update"' in f)
    parsed = parse_rtds_message(frame)
    assert parsed["kind"] == "update"
    (ts_ms, price), = parsed["ticks"]
    payload = json.loads(frame)["payload"]
    assert ts_ms == payload["timestamp"]
    assert isinstance(price, Decimal)
    assert price == Decimal(payload["full_accuracy_value"]) / Decimal(10) ** 18
    assert abs(price - Decimal(str(payload["value"]))) < Decimal("0.000001")
    assert parsed["precision_mismatch"] == 0


def test_parse_real_snapshot_keeps_printed_digits():
    frame = next(f for f in _fixture_frames() if '"type":"subscribe"' in f)
    parsed = parse_rtds_message(frame)
    assert parsed["kind"] == "snapshot"
    assert len(parsed["ticks"]) == 59
    ts_ms, price = parsed["ticks"][0]
    assert ts_ms == 1793295431000
    assert price == Decimal("81031.384707022")


def test_parse_ignores_noise_and_mismatched_scale():
    assert parse_rtds_message("")["kind"] == "ignore"
    assert parse_rtds_message("PONG")["kind"] == "ignore"
    other = {"topic": "crypto_prices_chainlink", "type": "update",
             "payload": {"symbol": "eth/usd", "timestamp": 1, "value": 2000.5}}
    assert parse_rtds_message(json.dumps(other))["kind"] == "ignore"
    bad_scale = {"topic": "crypto_prices_chainlink", "type": "update",
                 "payload": {"symbol": "btc/usd", "timestamp": 5, "value": 80000.5,
                             "full_accuracy_value": "80000500000000000000"}}
    parsed = parse_rtds_message(json.dumps(bad_scale))
    assert parsed["ticks"] == [(5, Decimal("80000.5"))]
    assert parsed["precision_mismatch"] == 1


def test_real_fixture_boundary_tick_is_open_and_close():
    provider = RtdsChainlinkProvider(clock_ms=lambda: BOUNDARY * 1000 + 3000)
    for frame in _fixture_frames():
        provider.handle_raw(frame)
    tick = provider.open_price(BOUNDARY)
    assert tick is not None and tick.ts_ms == BOUNDARY * 1000
    # full_accuracy_value keeps the digit the float "value" (…94825) drops.
    assert tick.price == Decimal("80953.503173948255")
    # The window that ends at the boundary closes on the same tick.
    assert provider.close_price(BOUNDARY) == tick


def _state(offset_ms=2000):
    return BtcReferenceState(open_tick_max_offset_ms=offset_ms, clock_ms=lambda: 0)


def test_open_exact_late_and_missing():
    w = 1_800_000_000
    state = _state()
    state.ingest(w * 1000 - 1000, Decimal("100"))
    state.ingest(w * 1000, Decimal("101"))
    state.ingest(w * 1000 + 1000, Decimal("102"))
    assert state.open_price(w).price == Decimal("101")
    assert state.open_price(w + 300) is None
    assert state.boundary_status(w + 300)[1] == "NO_TICK"

    late = _state()
    late.ingest(w * 1000 - 1000, Decimal("100"))
    late.ingest(w * 1000 + 2500, Decimal("103"))
    assert late.open_price(w) is None
    assert late.boundary_status(w)[1] == "TICK_TOO_LATE"

    edge = _state()
    edge.ingest(w * 1000 + 2000, Decimal("104"))
    assert edge.open_price(w).price == Decimal("104")


def test_snapshot_backfills_open_after_reconnect():
    w = 1_800_000_000
    state = _state()
    state.ingest(w * 1000 - 1000, Decimal("100"))
    # Disconnected over the boundary; the first live tick is 4 s late.
    state.ingest(w * 1000 + 4000, Decimal("105"))
    assert state.open_price(w) is None
    for offset in (0, 1000, 2000, 3000):
        state.ingest(w * 1000 + offset, Decimal(100 + offset // 1000), from_snapshot=True)
    assert state.open_price(w).price == Decimal("100")
    assert state.backfilled == 4


def test_gap_duplicate_out_of_order_counters():
    state = _state()
    for ts in (1000, 2000, 3000, 6000, 6000, 5000, 7000):
        state.ingest(ts, Decimal("1"))
    assert state.gaps == 1
    assert state.missing_ticks == 2
    assert state.duplicates == 1
    assert state.out_of_order == 1
    assert state.ticks_total == 6


def test_price_at_and_retention():
    state = _state()
    for ts in range(0, 20 * 60 * 1000, 1000):
        state.ingest(ts, Decimal(ts))
    assert state.price_at(3 * 60 * 1000 + 500) is None  # trimmed (15 min buffer)
    assert state.price_at(5 * 60 * 1000 + 500).ts_ms == 5 * 60 * 1000
    hit = state.price_at(19 * 60 * 1000 + 500)
    assert hit.ts_ms == 19 * 60 * 1000
    assert state.current().ts_ms == 20 * 60 * 1000 - 1000


class _FakeSocket:
    def __init__(self, frames, *, hold=True):
        self.frames = list(frames)
        self.hold = hold
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, message):
        self.sent.append(message)

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(3600)


def test_watchdog_reconnects_after_silence():
    update = next(f for f in _fixture_frames() if '"type":"update"' in f)
    sockets = []

    def connect(_url):
        sock = _FakeSocket([update])
        sockets.append(sock)
        return sock

    provider = RtdsChainlinkProvider(
        connect=connect, stall_seconds=0.2, ping_interval_seconds=0.05,
        max_backoff_seconds=0.01,
    )

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(provider.run(stop))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(sockets) >= 2:
                break
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    assert len(sockets) >= 2
    assert provider.stalls >= 1
    assert provider.reconnects >= 1
    assert sockets[0].sent[0] == rtds_subscribe_message()
    assert "PING" in sockets[0].sent
    assert provider.state.ticks_total == 1  # same tick again is a duplicate
    assert provider.connected is False


def test_window_id_and_provider_selection():
    assert window_start_from_event_id("btc-updown-5m-1793295600") == 1793295600
    assert window_start_from_event_id("garbage") is None

    class Cfg:
        btc_feed_provider = "polybolt"
        btc_open_tick_max_offset_ms = 2000
        btc_feed_stall_seconds = 10

    assert build_btc_reference_provider(Cfg()).name == "polybolt"
    Cfg.btc_feed_provider = "rtds"
    assert build_btc_reference_provider(Cfg()).name == "rtds"

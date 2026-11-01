"""TWAP stream, DUST residual flag and the re-check measurement watch.

No network: real RTDS TWAP frames from a fixture, ticks fed with a fixed clock.
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live.strategy_runtime as strategy_runtime_module  # noqa: E402
from live.btc_reference_feed import (  # noqa: E402
    RtdsChainlinkProvider, parse_rtds_message,
)
from test_btc_reference_integration import (  # noqa: E402
    EVENT, OPEN, W, _exit_ticks, _iso, _open_position, harness_factory,  # noqa: F401
)

TWAP_FIXTURE = Path(__file__).parent / "fixtures" / "rtds_twap_sixty_sample.jsonl"


def _twap_frames():
    rows = [json.loads(line) for line in TWAP_FIXTURE.read_text().splitlines()]
    return [row["data"] for row in sorted(rows, key=lambda row: row["recv_ms"])]


# ------------------------------------------------------------------ feed
def test_parse_real_twap_snapshot_and_update():
    frames = _twap_frames()
    snapshot = parse_rtds_message(frames[0])
    assert snapshot["kind"] == "snapshot" and snapshot["stream"] == "twap"
    assert len(snapshot["ticks"]) == 59
    ts_ms, price = snapshot["ticks"][0]
    item = json.loads(frames[0])["payload"]["data"][0]
    assert ts_ms == item["timestamp"]
    assert price == Decimal(item["full_accuracy_value"]) / Decimal(10) ** 18
    update = parse_rtds_message(frames[1])
    assert update["kind"] == "update" and update["stream"] == "twap"
    payload = json.loads(frames[1])["payload"]
    assert payload["window_s"] == 60
    assert update["ticks"][0][0] == payload["timestamp"]


def test_twap_frames_fill_twap_state_only_and_do_not_feed_stall_watchdog():
    frames = _twap_frames()
    last_ts = json.loads(frames[-1])["payload"]["timestamp"]
    provider = RtdsChainlinkProvider(clock_ms=lambda: last_ts + 1500)
    progress = [provider.handle_raw(frame) for frame in frames]
    assert progress == [0] * len(frames)  # spot liveness is what the gate needs
    assert provider.current() is None and provider.state.buffer_size() == 0
    assert provider.twap_state.ticks_total == len(frames) - 1
    assert provider.twap_current().ts_ms == last_ts
    assert provider.twap_price_at(last_ts - 500).ts_ms == last_ts - 1000
    health = provider.health()
    assert health["twap_ticks_total"] == len(frames) - 1
    assert health["twap_last_payload_age_ms"] == 1500


# ----------------------------------------------------- deal TWAP + DUST
def test_entry_records_twap_next_to_spot(harness_factory):
    h = harness_factory(mode="observe")
    h.tick(0, OPEN)
    h.tick(199_000, OPEN + 25)
    h.provider.twap_state.ingest((W + 198) * 1000, Decimal("80011.5"))
    h.trigger("twap")
    h.flush()
    deal = h.deal()
    assert deal["btc_entry_price_text"] == "80025"
    assert deal["btc_entry_twap_text"] == "80011.5"


def test_stale_twap_is_not_recorded_at_entry(harness_factory):
    h = harness_factory(mode="observe")
    h.tick(0, OPEN)
    h.tick(199_000, OPEN + 25)
    h.provider.twap_state.ingest((W + 150) * 1000, Decimal("80011.5"))
    h.trigger("stale-twap")
    h.flush()
    assert h.deal()["btc_entry_twap_text"] is None


def test_dust_exit_keeps_its_type_and_flags_the_residual(harness_factory):
    h = harness_factory()
    _exit_ticks(h)
    h.provider.twap_state.ingest((W + 250) * 1000, Decimal("80020"))
    position = _open_position(h)
    intent = h.strategy.reserve_position_intent(
        position, action="EXIT", purpose="STOP_066", order_type="FAK",
        shares=Decimal("8"), price_limit=Decimal("0.01"), book_hash="dust",
    )
    h.strategy.add_fill(
        intent_id=intent["intent_id"], remote_trade_id="trade-dust",
        shares=Decimal("7.5"), price=Decimal("0.60"), fee=Decimal("0"),
        status="CONFIRMED", matched_at=_iso(W + 250),
    )
    h.strategy.apply_exit_fill(
        position_id=position["position_id"], intent_id=intent["intent_id"],
        sold_shares=Decimal("7.5"), average_price=Decimal("0.60"), fees=Decimal("0"),
        final_state="PARTIAL_FINAL", min_sellable=Decimal("1"), purpose="STOP_066",
        book_hash="dust",
    )
    assert h.deal()["state"] == "DUST"
    h.clock[0] = (W + 260) * 1000
    h.flush()
    deal = h.deal()
    assert deal["btc_exit_type"] == "STOP"
    assert deal["btc_exit_dust_residual"] == 1
    assert deal["btc_exit_price_text"] == "80040"
    assert deal["btc_exit_twap_text"] == "80020"


# ------------------------------------------------------ re-check watch
def _frame(h, tag, ask, asset="yes"):
    h.runtime.schedule_frame({
        "event_type": "price_change", "message_hash": tag,
        "updates": [{"condition_id": "condition", "asset_id": asset,
                     "outcome": "YES" if asset == "yes" else "NO", "best_ask": ask}],
        "event_readiness": {"condition": {"ready": True, "reason": "READY"}},
    })


def _shadow_row(h):
    rows = h.rows("SELECT * FROM live_btc_gate_evaluations WHERE source='SHADOW'")
    assert len(rows) == 1
    return rows[0]


def _paused_at_remaining(harness_factory, monkeypatch, remaining):
    h = harness_factory(mode="observe", paused=True)
    monkeypatch.setattr(strategy_runtime_module, "remaining_seconds",
                        lambda _event, _at: Decimal(str(remaining)))
    h.clock[0] = (W + 300 - remaining) * 1000
    h.tick(0, OPEN)
    return h


def test_watch_records_crossing_while_ask_stays_at_074(harness_factory, monkeypatch):
    h = _paused_at_remaining(harness_factory, monkeypatch, 100)
    h.tick(199_000, OPEN + 3)
    _frame(h, "edge", "0.74")
    assert h.runtime._btc.has_watches
    h.flush()
    row = _shadow_row(h)
    assert row["reason"] == "BTC_DELTA_BELOW_MIN" and row["watch_end_reason"] is None

    # BTC crosses +10 while the ask is still 0.74 (no book frame needed).
    h.tick(203_000, OPEN + 15)
    h.clock[0] = (W + 203) * 1000
    h.flush()
    assert h.runtime._btc.has_watches  # still at 0.74: watch continues

    h.clock[0] = (W + 205) * 1000
    _frame(h, "left", "0.75")
    assert not h.runtime._btc.has_watches
    h.flush()
    row = _shadow_row(h)
    assert row["delta_crossed_min_while_at_074"] == 1
    assert row["crossed_at"] == "2027-01-15T08:03:23Z"
    assert row["directional_delta_at_cross"] == "15"
    assert row["ask_left_074_at"] == "2027-01-15T08:03:25Z"
    assert row["watch_end_reason"] == "ASK_LEFT_074"
    assert h.submits == []  # measurement only


def test_watch_without_crossing_ends_at_final_minute(harness_factory, monkeypatch):
    h = _paused_at_remaining(harness_factory, monkeypatch, 100)
    h.tick(199_000, OPEN + 3)
    _frame(h, "edge", "0.74")
    h.tick(239_000, OPEN + 4)
    h.clock[0] = (W + 240) * 1000  # remaining 60: the entry cutoff
    h.flush()
    row = _shadow_row(h)
    assert row["delta_crossed_min_while_at_074"] == 0
    assert row["crossed_at"] is None and row["ask_left_074_at"] is None
    assert row["watch_end_reason"] == "FINAL_MINUTE"


def test_no_watch_starts_in_final_minute(harness_factory, monkeypatch):
    h = _paused_at_remaining(harness_factory, monkeypatch, 30)
    h.tick(269_000, OPEN + 3)
    _frame(h, "late", "0.74")
    assert not h.runtime._btc.has_watches
    h.flush()
    row = _shadow_row(h)
    assert row["trading_state"] == "FINAL_MINUTE"
    assert row["watch_end_reason"] is None


def test_later_crossing_episode_replaces_non_crossing_one(harness_factory, monkeypatch):
    h = _paused_at_remaining(harness_factory, monkeypatch, 100)
    h.tick(199_000, OPEN + 3)
    _frame(h, "edge-1", "0.74")
    _frame(h, "left-1", "0.75")
    h.flush()
    assert _shadow_row(h)["delta_crossed_min_while_at_074"] == 0

    h.clock[0] = (W + 210) * 1000
    h.tick(209_000, OPEN + 4)
    _frame(h, "edge-2", "0.74")  # second 0.74 episode, same blocking reason
    h.tick(211_000, OPEN + 30)
    h.clock[0] = (W + 211) * 1000
    _frame(h, "left-2", "0.73")
    h.flush()
    row = _shadow_row(h)
    assert row["delta_crossed_min_while_at_074"] == 1
    assert row["directional_delta_at_cross"] == "30"
    assert row["count"] == 2


def test_watch_ignores_other_tokens_and_survives_unflushed_rows(harness_factory, monkeypatch):
    h = _paused_at_remaining(harness_factory, monkeypatch, 100)
    h.tick(199_000, OPEN + 3)
    _frame(h, "edge", "0.74")
    _frame(h, "other", "0.30", asset="no")
    assert h.runtime._btc.has_watches
    _frame(h, "left", "0.75")  # ends before the evaluation row was ever written
    h.flush()
    assert _shadow_row(h)["watch_end_reason"] == "ASK_LEFT_074"

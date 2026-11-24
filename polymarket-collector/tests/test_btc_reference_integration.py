"""BTC reference gate wired into the entry path, plus deal BTC snapshots.

No network: the RTDS provider is fed ticks directly with a fixed clock.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile

import pytest

import live.strategy_runtime as strategy_runtime_module
from live.adapters.mock import MockTradingAdapter
from live.btc_reference_feed import RtdsChainlinkProvider
from live.btc_reference_recorder import BtcReferenceRecorder, parse_time_ms
from live.config import LiveConfig
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository
from live.strategy_runtime import LiveStrategyRuntime

W = 1_800_000_000  # a real 5-minute boundary (divisible by 300)
EVENT = f"btc-updown-5m-{W}"
OPEN = Decimal("80000")


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


class Harness:
    def __init__(self, *, mode="observe", paused=False, labels=("Up", "Down")):
        self.temp = tempfile.TemporaryDirectory()
        self.base = LiveRepository(Path(self.temp.name) / "live.sqlite3")
        self.base.migrate()
        self.strategy = StrategyRepository(self.base)
        self.strategy.migrate()
        self.base.upsert_market({
            "event_id": EVENT, "condition_id": "condition",
            "yes_token_id": "yes", "no_token_id": "no",
            "token_mapping_status": "verified", "accepting_orders": True,
            "min_order_size": 1, "min_tick_size": 0.01, "taker_base_fee": 0,
            "raw_market_info": {
                "scope_verified": True, "slug": EVENT,
                "yes_outcome_label": labels[0], "no_outcome_label": labels[1],
            },
        })
        if not paused:
            self.base.set_states({
                "kill_switch": "false", "canary_armed": "true",
                "canary_consumed": "false", "pause_entries": "false",
            }, "operator")
            self.strategy.set_pause_entries(False, "operator", "TEST_READY")
        self.clock = [(W + 200) * 1000]
        self.provider = RtdsChainlinkProvider(clock_ms=lambda: self.clock[0])
        self.config = LiveConfig(execution_mode="REAL_TRADING", btc_gate_mode=mode)
        self.runtime = LiveStrategyRuntime(
            self.config, self.base, self.strategy, MockTradingAdapter()
        )
        self.runtime.set_btc_reference_provider(self.provider)
        self.runtime.entry_schedule_status = lambda at=None: {
            "allowed": True, "reason": "ENTRY_SCHEDULE_ACTIVE",
            "timezone": "Asia/Jerusalem", "local_time": "test",
        }
        self.runtime.set_market_freshness_provider(lambda _condition: {
            "ready": True, "reason": "READY",
            "exchange_age_ms": {"yes": 100, "no": 100},
            "book_versions": {"yes": {"generation": 1, "update_number": 1}},
        })
        self.submits = []

        async def fake_submit(**kwargs):
            self.submits.append(kwargs)

        self.runtime._submit_entry = fake_submit

    def tick(self, offset_ms: int, price, *, price_to_beat=None) -> None:
        self.provider.state.ingest(W * 1000 + offset_ms, Decimal(str(price)))
        if offset_ms == 0:
            # The window-start TWAP tick is the market's price to beat; by
            # default it equals the spot open.
            self.provider.twap_state.ingest(
                W * 1000, Decimal(str(price if price_to_beat is None else price_to_beat)),
            )

    def trigger(self, tag: str) -> None:
        update = {
            "asset_id": "yes", "outcome": "YES", "best_ask": "0.74",
            "asks": [{"price": "0.74", "size": "10"}],
            "generation": 1, "update_number": 1,
        }
        asyncio.run(self.runtime._process_event(
            market=self.base.latest_market("condition"), updates=[update],
            event_ready=True, readiness_reason="READY",
            received_at=_iso(W + 200), frame_hash=tag,
        ))

    def flush(self) -> None:
        asyncio.run(self.runtime._btc.flush(force_scans=True))

    def rows(self, sql, *args):
        with self.base.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def deal(self):
        return self.rows("SELECT * FROM live_strategy_deals WHERE event_id=?", EVENT)[0]

    def close(self):
        self.temp.cleanup()


@pytest.fixture
def harness_factory():
    made = []

    def make(**kwargs):
        made.append(Harness(**kwargs))
        return made[-1]

    yield make
    for harness in made:
        harness.close()


def test_gate_pass_reserves_and_fills_deal_entry_columns(harness_factory):
    h = harness_factory(mode="enforce")
    h.tick(0, OPEN)
    h.tick(199_000, OPEN + 25)
    h.trigger("pass")
    assert len(h.submits) == 1
    h.flush()
    deal = h.deal()
    assert deal["btc_open_price_text"] == "80000"
    assert deal["btc_open_tick_ts_ms"] == W * 1000
    assert deal["btc_entry_price_text"] == "80025"
    assert deal["btc_entry_ts_ms"] == (W + 199) * 1000
    assert deal["btc_entry_age_ms"] == 1000
    assert deal["btc_entry_directional_delta_usd"] == "25"
    assert deal["btc_gate_reason"] == "BTC_GATE_PASS"
    assert deal["btc_gate_mode"] == "enforce"
    assert deal["btc_entry_missing_reason"] is None
    evaluation = h.rows("SELECT * FROM live_btc_gate_evaluations")[0]
    assert evaluation["source"] == "DECISION" and evaluation["allowed"] == 1
    assert evaluation["intent_id"] == deal["entry_intent_id"]


def test_gate_measures_against_price_to_beat_not_spot_open(harness_factory):
    # Trade 860 (btc-updown-5m-1793787900): spot open 78485.25, price to beat
    # (TWAP open) 78446.49, spot at entry 78458.96. Against the spot open Up
    # looked $26 behind; against the price to beat it was $12 ahead.
    h = harness_factory(mode="enforce")
    h.tick(0, "78485.246527368625", price_to_beat="78446.490507973333352448")
    h.tick(199_000, "78458.956163457505")
    h.trigger("price-to-beat")
    assert len(h.submits) == 1
    h.flush()
    deal = h.deal()
    assert deal["btc_open_price_text"] == "78446.490507973333352448"
    assert deal["btc_entry_directional_delta_usd"] == "12.465655484171647552"
    assert deal["btc_gate_reason"] == "BTC_GATE_PASS"


def test_missing_price_to_beat_fails_closed(harness_factory):
    h = harness_factory(mode="enforce")
    h.provider.state.ingest(W * 1000, OPEN)  # spot open only, no TWAP open
    h.tick(199_000, OPEN + 25)
    h.trigger("no-price-to-beat")
    assert h.submits == []
    h.flush()
    row = h.rows("SELECT reason FROM live_btc_gate_evaluations WHERE event_id=?", EVENT)[0]
    assert row["reason"] == "BTC_OPEN_PRICE_UNKNOWN"


def test_enforce_block_skips_without_locking_then_next_trigger_enters(harness_factory):
    h = harness_factory(mode="enforce")
    h.tick(0, OPEN)
    h.tick(199_000, OPEN + 5)
    h.trigger("blocked")
    assert h.submits == []
    assert h.rows("SELECT * FROM live_strategy_intents") == []
    assert h.rows("SELECT * FROM live_event_states WHERE event_id=?", EVENT) == []
    assert h.runtime._entry_state_from_ram(EVENT)["event_locked"] is False
    skipped = h.rows(
        "SELECT * FROM live_audit_timeline WHERE source='btc_reference_gate'"
    )
    assert len(skipped) == 1
    assert skipped[0]["category"] == "DECISION"
    assert skipped[0]["reason_code"] == "BTC_DELTA_BELOW_MIN"
    assert skipped[0]["result_status"] == "SKIPPED"

    # BTC keeps moving up; the next 0.74 trigger in the same event enters.
    h.tick(199_500, OPEN + 30)
    h.trigger("passes")
    assert len(h.submits) == 1
    assert len(h.rows("SELECT * FROM live_strategy_intents")) == 1
    h.flush()
    reasons = {
        row["reason"]: row
        for row in h.rows("SELECT * FROM live_btc_gate_evaluations WHERE source='DECISION'")
    }
    assert reasons["BTC_DELTA_BELOW_MIN"]["would_block"] == 1
    assert reasons["BTC_DELTA_BELOW_MIN"]["intent_id"] is None
    assert reasons["BTC_GATE_PASS"]["intent_id"] is not None
    assert h.runtime._btc.decision_blocks == 1


def test_enforce_fails_closed_without_feed(harness_factory):
    h = harness_factory(mode="enforce")
    h.trigger("no-feed")
    assert h.submits == []
    skipped = h.rows("SELECT reason_code FROM live_audit_timeline WHERE source='btc_reference_gate'")
    assert skipped == [{"reason_code": "BTC_FEED_UNAVAILABLE"}]


def test_unknown_side_mapping_fails_closed(harness_factory):
    h = harness_factory(mode="enforce", labels=("Yes", "No"))
    h.tick(0, OPEN)
    h.tick(199_000, OPEN + 50)
    h.trigger("unmapped")
    assert h.submits == []
    skipped = h.rows("SELECT reason_code FROM live_audit_timeline WHERE source='btc_reference_gate'")
    assert skipped == [{"reason_code": "BTC_SIDE_MAPPING_UNKNOWN"}]


def test_reversed_labels_flip_direction(harness_factory):
    # A market whose YES token is "Down": BTC falling is the YES direction.
    h = harness_factory(mode="enforce", labels=("Down", "Up"))
    h.tick(0, OPEN)
    h.tick(199_000, OPEN - 20)
    h.trigger("reversed")
    assert len(h.submits) == 1


def test_observe_mode_enters_and_records_would_block(harness_factory):
    h = harness_factory(mode="observe")
    h.tick(0, OPEN)
    h.tick(199_000, OPEN + 5)
    h.trigger("observe")
    assert len(h.submits) == 1
    h.flush()
    evaluation = h.rows("SELECT * FROM live_btc_gate_evaluations")[0]
    assert evaluation["reason"] == "BTC_DELTA_BELOW_MIN"
    assert evaluation["allowed"] == 0 and evaluation["would_block"] == 1
    assert evaluation["mode"] == "observe"
    assert evaluation["intent_id"] is not None
    deal = h.deal()
    assert deal["btc_gate_reason"] == "BTC_DELTA_BELOW_MIN"
    assert deal["btc_gate_mode"] == "observe"
    assert deal["btc_entry_directional_delta_usd"] == "5"
    assert h.rows("SELECT * FROM live_audit_timeline WHERE source='btc_reference_gate'") == []


def test_shadow_evaluation_while_paused(harness_factory, monkeypatch):
    h = harness_factory(mode="observe", paused=True)
    monkeypatch.setattr(strategy_runtime_module, "remaining_seconds",
                        lambda _event, _at: Decimal("100"))
    h.tick(0, OPEN)
    h.tick(199_000, OPEN + 12)
    frame = {
        "event_type": "price_change", "message_hash": "shadow",
        "updates": [{"condition_id": "condition", "asset_id": "no",
                     "outcome": "NO", "best_ask": "0.74"}],
        "event_readiness": {"condition": {"ready": True, "reason": "READY"}},
    }
    h.runtime.schedule_frame(frame)
    h.runtime.schedule_frame(frame)
    assert h.submits == []
    h.flush()
    rows = h.rows("SELECT * FROM live_btc_gate_evaluations")
    assert len(rows) == 1
    shadow = rows[0]
    assert shadow["source"] == "SHADOW" and shadow["count"] == 2
    assert shadow["side"] == "NO" and shadow["side_outcome"] == "DOWN"
    assert shadow["reason"] == "BTC_DELTA_BELOW_MIN"
    assert shadow["directional_delta_usd"] == "-12"
    assert shadow["first_directional_delta_usd"] == "-12"
    assert "PAUSED" in shadow["trading_state"]
    assert shadow["remaining_seconds"] == "100"
    assert h.rows("SELECT * FROM live_strategy_intents") == []


def test_shadow_skips_triggers_outside_entry_window(harness_factory, monkeypatch):
    h = harness_factory(mode="observe", paused=True)
    monkeypatch.setattr(strategy_runtime_module, "remaining_seconds",
                        lambda _event, _at: Decimal("200"))
    h.runtime.schedule_frame({
        "event_type": "price_change", "message_hash": "early",
        "updates": [{"condition_id": "condition", "asset_id": "yes",
                     "outcome": "YES", "best_ask": "0.74"}],
        "event_readiness": {"condition": {"ready": True, "reason": "READY"}},
    })
    h.flush()
    assert h.rows("SELECT * FROM live_btc_gate_evaluations") == []


# ----------------------------------------------------------------- exits
def _open_position(h):
    h.strategy.reserve_event_entry(
        event_id=EVENT, condition_id="condition", token_id="yes", side="YES",
        simultaneous=False, reason_code="ENTRY_PRICE_EXACT",
    )
    return h.strategy.open_position(
        event_id=EVENT, condition_id="condition", token_id="yes", outcome="YES",
        shares=Decimal("8"), average_price=Decimal("0.74"),
        cost_all_in=Decimal("6"), fees=Decimal("0"), sellable_shares=Decimal("8"),
        min_sellable=Decimal("1"),
    )


def _exit(h, position, *, purpose, action, matched_at):
    intent = h.strategy.reserve_position_intent(
        position, action=action, purpose=purpose,
        order_type="GTC" if action == "TP" else "FAK", shares=Decimal("8"),
        price_limit=Decimal("0.96") if action == "TP" else Decimal("0.01"),
        book_hash=f"test-{purpose}",
    )
    h.strategy.add_fill(
        intent_id=intent["intent_id"], remote_trade_id=f"trade-{purpose}",
        shares=Decimal("8"), price=Decimal("0.96"), fee=Decimal("0"),
        status="CONFIRMED", matched_at=matched_at,
    )
    h.strategy.apply_exit_fill(
        position_id=position["position_id"], intent_id=intent["intent_id"],
        sold_shares=Decimal("8"), average_price=Decimal("0.96"), fees=Decimal("0"),
        final_state="FILLED", min_sellable=Decimal("1"), purpose=purpose,
        book_hash="test",
    )


def _exit_ticks(h):
    h.tick(0, OPEN)
    for second, price in ((249, 80030), (250, 80040), (251, 80050)):
        h.tick(second * 1000, price)


@pytest.mark.parametrize("purpose,action,expected", [
    ("TAKE_PROFIT", "TP", "TP"),
    ("STOP_066", "EXIT", "STOP"),
    ("EMERGENCY_060", "EXIT", "EMERGENCY"),
])
def test_exit_fill_records_btc_at_fill_time(harness_factory, purpose, action, expected):
    h = harness_factory()
    _exit_ticks(h)
    position = _open_position(h)
    # STOP fill timestamp as epoch seconds, TP as ISO: both formats occur.
    matched = str(W + 250) if action == "EXIT" else _iso(W + 250)
    _exit(h, position, purpose=purpose, action=action, matched_at=matched)
    h.clock[0] = (W + 252) * 1000
    h.flush()
    assert h.deal()["btc_exit_type"] is None  # tick for the fill may still be in flight
    h.clock[0] = (W + 260) * 1000
    h.flush()
    deal = h.deal()
    assert deal["state"] == "CLOSED"
    assert deal["btc_exit_type"] == expected
    assert deal["btc_exit_price_text"] == "80040"
    assert deal["btc_exit_ts_ms"] == (W + 250) * 1000
    assert deal["btc_exit_age_ms"] == 0
    assert deal["btc_exit_directional_delta_usd"] == "40"
    assert deal["btc_exit_missing_reason"] is None


def test_resolution_records_window_close(harness_factory):
    h = harness_factory()
    # Price to beat (TWAP open) is $5 above the spot open.
    h.tick(0, OPEN, price_to_beat=OPEN + 5)
    h.tick(300_000, OPEN + 100)
    # Spot closes up, but the 60s TWAP (the resolution source) closes below
    # the price to beat: the derived outcome follows TWAP vs TWAP.
    h.provider.twap_state.ingest((W + 300) * 1000, Decimal(OPEN - 1))
    position = _open_position(h)
    h.strategy.mark_position_resolved(position["position_id"], winner=True, redeem_pending=True)
    h.clock[0] = (W + 301) * 1000
    h.flush()
    assert h.deal()["btc_exit_type"] is None
    h.clock[0] = (W + 310) * 1000
    h.flush()
    deal = h.deal()
    assert deal["btc_exit_type"] == "RESOLUTION"
    assert deal["btc_exit_price_text"] == "80100"
    assert deal["btc_exit_ts_ms"] == (W + 300) * 1000
    assert deal["btc_exit_age_ms"] == 0
    # Measured against the price to beat (80005), not the spot open.
    assert deal["btc_exit_directional_delta_usd"] == "95"
    assert deal["btc_exit_twap_text"] == "79999"
    assert deal["btc_exit_dust_residual"] == 0

    reference = h.rows("SELECT * FROM live_btc_event_reference WHERE event_id=?", EVENT)[0]
    assert reference["open_price_text"] == "80000"
    assert reference["close_price_text"] == "80100"
    assert reference["open_twap_text"] == "80005"
    assert reference["close_twap_text"] == "79999"
    assert reference["close_twap_ts_ms"] == (W + 300) * 1000
    assert reference["derived_outcome"] == "DOWN"
    assert reference["derived_outcome_spot"] == "UP"
    assert reference["derived_outcome_twap_open"] == "DOWN"
    assert reference["actual_outcome"] is None
    h.base.mark_market_resolved_from_rest("condition", "no", "Down")
    h.flush()
    reference = h.rows("SELECT * FROM live_btc_event_reference WHERE event_id=?", EVENT)[0]
    assert reference["actual_outcome"] == "DOWN"
    assert reference["outcome_match"] == 1


def test_exit_without_feed_records_null_and_reason(harness_factory):
    h = harness_factory()
    position = _open_position(h)
    _exit(h, position, purpose="STOP_066", action="EXIT", matched_at=_iso(W + 250))
    h.clock[0] = (W + 260) * 1000
    h.flush()
    deal = h.deal()
    assert deal["state"] == "CLOSED"
    assert deal["btc_exit_type"] == "STOP"
    assert deal["btc_exit_price_text"] is None
    assert deal["btc_exit_missing_reason"] == "BTC_TICK_NOT_IN_BUFFER"


def test_entry_without_feed_records_missing_reason(harness_factory):
    h = harness_factory(mode="observe")
    h.trigger("observe-no-feed")
    assert len(h.submits) == 1
    h.flush()
    deal = h.deal()
    assert deal["btc_entry_price_text"] is None
    assert deal["btc_entry_missing_reason"] == "BTC_FEED_UNAVAILABLE"
    assert deal["btc_gate_reason"] == "BTC_FEED_UNAVAILABLE"


def test_ticks_never_write_to_the_database():
    calls = []

    class CountingRepo:
        def __getattr__(self, name):
            def record(*args, **kwargs):
                calls.append(name)
                return [] if name == "deals_pending_btc_exit" else 0
            return record

    clock = [(W + 290) * 1000]
    provider = RtdsChainlinkProvider(clock_ms=lambda: clock[0])
    recorder = BtcReferenceRecorder(LiveConfig(), CountingRepo(), None, provider=provider)
    for second in range(290):
        provider.state.ingest((W + second) * 1000, OPEN + second)
    assert calls == []
    asyncio.run(recorder.flush())
    assert calls.count("upsert_btc_event_references") == 1
    assert "upsert_btc_gate_evaluations" not in calls
    calls.clear()
    asyncio.run(recorder.flush())  # nothing changed: nothing written
    assert calls == []


def test_parse_time_ms_formats():
    assert parse_time_ms("1800000250") == 1_800_000_250_000
    assert parse_time_ms("1800000250123") == 1_800_000_250_123
    assert parse_time_ms("2027-01-15T08:00:00Z") == parse_time_ms("2027-01-15T08:00:00+00:00")
    assert parse_time_ms("") is None and parse_time_ms("nope") is None


def test_dashboard_trade_history_exposes_deal_btc_fields():
    from live.dashboard_read_model import DashboardReadModel, resolve_window
    from test_dashboard_v1 import NOW, build_db, seed_verified_lifecycle

    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp)
        seed_verified_lifecycle(repo)
        with repo.connect() as conn:
            conn.execute(
                "UPDATE live_strategy_deals SET btc_open_price_text='80000',"
                "btc_entry_price_text='80025.5',btc_entry_directional_delta_usd='25.5',"
                "btc_gate_reason='BTC_GATE_PASS',btc_gate_mode='observe',"
                "btc_exit_type='TP',btc_exit_price_text='80060'"
            )
            conn.commit()
        model = DashboardReadModel(LiveRepository(repo.db_path, query_only=True))
        item = model.trade_history(resolve_window("today", now=NOW))["items"][0]
        assert item["btc"]["btc_entry_price"] == 80025.5
        assert item["btc"]["btc_entry_directional_delta_usd"] == 25.5
        assert item["btc"]["btc_gate_reason"] == "BTC_GATE_PASS"
        assert item["btc"]["btc_exit_type"] == "TP"
        assert item["btc"]["btc_exit_missing_reason"] is None


def test_dashboard_external_deal_table_carries_deal_btc_fields():
    import sqlite3
    from live.dashboard_read_model import DashboardReadModel, resolve_window
    from scripts.migrate_external_fills import DDL
    from test_dashboard_v1 import NOW, build_db, seed_verified_lifecycle

    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp)
        seed_verified_lifecycle(repo)
        with sqlite3.connect(repo.db_path) as conn:
            conn.executescript(DDL)
            for trade_id, slug in (("t1", "btc-updown-5m-1786529400"), ("t2", "btc-updown-5m-other")):
                conn.execute(
                    "INSERT INTO external_fills(trade_id,condition_id,event_slug,side,price,size,"
                    "notional_usd,fee_usd,matched_at,raw_json,ingested_at)"
                    " VALUES(?,?,?,'BUY',0.74,10,7.4,0,'2026-08-12T10:00:03+00:00','{}',"
                    "'2026-08-12T10:00:04+00:00')",
                    (trade_id, f"condition-{trade_id}", slug),
                )
            conn.execute(
                "UPDATE live_strategy_deals SET btc_open_price_text='80000',"
                "btc_entry_price_text='80025.5',btc_entry_directional_delta_usd='25.5',"
                "btc_gate_reason='BTC_GATE_PASS',btc_gate_mode='observe',"
                "btc_exit_type='STOP',btc_exit_price_text='79990',"
                "btc_exit_directional_delta_usd='-10'"
            )
        model = DashboardReadModel(LiveRepository(repo.db_path, query_only=True))
        items = {
            item["event_slug"]: item
            for item in model.external_deal_history(resolve_window("today", now=NOW))["items"]
        }
        btc = items["btc-updown-5m-1786529400"]["btc"]
        assert btc["btc_open_price"] == 80000
        assert btc["btc_entry_directional_delta_usd"] == 25.5
        assert btc["btc_exit_type"] == "STOP"
        assert btc["btc_exit_price"] == 79990
        assert "btc" not in items["btc-updown-5m-other"]  # no local deal for that event


def test_gate_error_blocks_in_enforce_only(harness_factory):
    def boom(**_kwargs):
        raise RuntimeError("evaluation bug")

    enforce = harness_factory(mode="enforce")
    enforce.runtime._btc.evaluate = boom
    enforce.trigger("enforce-error")
    assert enforce.submits == []
    skipped = enforce.rows(
        "SELECT reason_code FROM live_audit_timeline WHERE source='btc_reference_gate'"
    )
    assert skipped == [{"reason_code": "BTC_GATE_ERROR"}]

    observe = harness_factory(mode="observe")
    observe.runtime._btc.evaluate = boom
    observe.trigger("observe-error")
    assert len(observe.submits) == 1


def test_deal_entry_bookkeeping_error_still_submits(harness_factory):
    h = harness_factory(mode="observe")
    h.tick(0, OPEN)
    h.tick(199_000, OPEN + 25)

    def boom(*_args, **_kwargs):
        raise RuntimeError("bookkeeping bug")

    h.runtime._btc.note_deal_entry = boom
    h.trigger("bookkeeping-error")
    assert len(h.submits) == 1


# ---------------------------------------------------------------------------
# Weekday 14:00-23:00 Asia/Jerusalem: entries open with a $30 BTC minimum
# (live/entry_schedule.py) instead of the window closing entries.

def _strict_schedule(h):
    h.runtime.entry_schedule_status = lambda at=None: {
        "allowed": True, "reason": "ENTRY_SCHEDULE_BTC_STRICT",
        "btc_strict_window": True,
        "timezone": "Asia/Jerusalem", "local_time": "test",
    }


def test_strict_window_requires_30_then_enters(harness_factory):
    h = harness_factory(mode="enforce")
    _strict_schedule(h)
    h.tick(0, OPEN)
    h.tick(199_000, OPEN + 29)  # passes the $10 gate, not the $30 one
    h.trigger("strict-below")
    assert h.submits == []
    skipped = h.rows(
        "SELECT reason_code FROM live_audit_timeline WHERE source='btc_reference_gate'"
    )
    assert skipped == [{"reason_code": "BTC_DELTA_BELOW_MIN"}]
    h.tick(199_500, OPEN + 30)
    h.trigger("strict-pass")
    assert len(h.submits) == 1
    h.flush()
    rows = h.rows(
        "SELECT reason, trading_state FROM live_btc_gate_evaluations WHERE source='DECISION'"
    )
    assert {row["reason"] for row in rows} == {"BTC_DELTA_BELOW_MIN", "BTC_GATE_PASS"}
    assert all(row["trading_state"].endswith("|BTC_STRICT_WINDOW") for row in rows)


def test_strict_window_down_side_needs_30_down(harness_factory):
    h = harness_factory(mode="enforce", labels=("Down", "Up"))
    _strict_schedule(h)
    h.tick(0, OPEN)
    h.tick(199_000, OPEN - 20)  # enough outside the window, not inside it
    h.trigger("strict-down-below")
    assert h.submits == []
    h.tick(199_500, OPEN - 31)
    h.trigger("strict-down-pass")
    assert len(h.submits) == 1


def test_outside_window_keeps_10_minimum(harness_factory):
    h = harness_factory(mode="enforce")
    h.tick(0, OPEN)
    h.tick(199_000, OPEN + 10)
    h.trigger("normal-10")
    assert len(h.submits) == 1


@pytest.mark.parametrize("mode", ["observe", "off"])
def test_strict_window_stays_closed_without_enforcing_gate(harness_factory, mode):
    h = harness_factory(mode=mode)
    _strict_schedule(h)
    h.tick(0, OPEN)
    h.tick(199_000, OPEN + 100)
    h.trigger("not-enforced")
    assert h.submits == []
    schedule = h.runtime._entry_schedule()
    assert schedule["allowed"] is False
    assert schedule["reason"] == "ENTRY_SCHEDULE_INACTIVE"
    assert schedule["inactive_cause"] == "BTC_GATE_NOT_ENFORCED"


def test_evaluate_derives_window_from_gate_clock(harness_factory):
    h = harness_factory(mode="enforce")
    market = h.base.latest_market("condition")
    btc = h.runtime._btc
    # Mon 2026-11-23 15:00 Asia/Jerusalem (13:00 UTC): inside the window.
    h.clock[0] = int(datetime(2026, 11, 23, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)
    inside = btc.evaluate(market=market, side="YES", token_id="yes")
    assert inside["strict_window"] is True and inside["min_delta_usd"] == Decimal("30")
    # Sun 2026-11-22 15:00 Asia/Jerusalem: Sunday is outside the window.
    h.clock[0] = int(datetime(2026, 11, 22, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)
    outside = btc.evaluate(market=market, side="YES", token_id="yes")
    assert outside["strict_window"] is False and outside["min_delta_usd"] == Decimal("10")
    # The entry path's own schedule verdict wins over the clock.
    forced = btc.evaluate(market=market, side="YES", token_id="yes", strict_window=True)
    assert forced["min_delta_usd"] == Decimal("30")
    assert btc.health()["window_min_delta_usd"] == "30"


def test_window_min_delta_config_validation():
    ok = LiveConfig(btc_gate_min_delta_usd=Decimal("10"),
                    btc_gate_window_min_delta_usd=Decimal("30"))
    assert not [e for e in ok.validation_errors() if "WINDOW_MIN" in e]
    looser = LiveConfig(btc_gate_min_delta_usd=Decimal("10"),
                        btc_gate_window_min_delta_usd=Decimal("5"))
    assert any("LIVE_BTC_GATE_WINDOW_MIN_DELTA_USD" in e for e in looser.validation_errors())
    capped = LiveConfig(btc_gate_min_delta_usd=Decimal("10"),
                        btc_gate_window_min_delta_usd=Decimal("30"),
                        btc_gate_max_delta_usd=Decimal("20"))
    assert any("LIVE_BTC_GATE_MAX_DELTA_USD" in e for e in capped.validation_errors())
    assert LiveConfig().btc_gate_window_min_delta_usd == Decimal("30")


def test_strict_window_days_match_previous_blackout():
    from zoneinfo import ZoneInfo
    from live.entry_schedule import in_strict_btc_window
    il = ZoneInfo("Asia/Jerusalem")
    # Week of 2026-11-22: Sun 22 ... Sat 28.
    assert not in_strict_btc_window(datetime(2026, 11, 22, 16, 0, tzinfo=il))  # Sun
    for day in (23, 24, 25, 26, 27):  # Mon-Fri
        assert in_strict_btc_window(datetime(2026, 11, day, 16, 0, tzinfo=il))
    assert not in_strict_btc_window(datetime(2026, 11, 28, 16, 0, tzinfo=il))  # Sat

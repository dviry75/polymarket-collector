"""P0 LIVE hardening — one test per proven failure mode.

Incident: btc-updown-5m-1788250200. Signal YES ask == 0.74 at 08:13:32;
CLOB filled 10.555554 @ 0.36 at 08:13:36 (BUY limit 0.76 allows any lower
price). Position was not managed for ~57s; STOP never latched; fee recorded
as 0 / VERIFIED in a fee-enabled crypto taker market.
"""

import asyncio
import time
from decimal import Decimal
from pathlib import Path
import tempfile

from live.config import LiveConfig
from live.fee_accounting import resolve_trade_fee
from live.repository import LiveRepository, now_iso
from live.strategy_repository import StrategyRepository
from live.strategy_runtime import LiveStrategyRuntime
from live.adapters.mock import MockTradingAdapter

from test_latched_market_exit import RecordingSellAdapter, _book, _case, _ok_reconcile


# --------------------------------------------------------------------------
# entry-side harness
# --------------------------------------------------------------------------

class _EntryAdapter(MockTradingAdapter):
    def __init__(self, response):
        super().__init__(scenario="delayed")
        self.response = response
        self.create_calls: list[dict] = []
        self.token_balance: dict[str, str] = {}

    async def create_order(self, order):
        self.create_calls.append(dict(order))
        return dict(self.response)

    async def get_token_balance(self, token_id):
        if token_id in self.token_balance:
            return {"status": "ok", "balance_text": self.token_balance[token_id]}
        return {"status": "ok", "balance_text": "0"}

    async def get_order_book(self, token_id):
        return {"asset_id": token_id, "bids": [], "asks": []}


def _entry_case(name, *, response, exit_bid=None, reconciliation=None):
    temp = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temp.name) / "live.sqlite3")
    base.migrate()
    repo = StrategyRepository(base)
    repo.migrate()
    event_id = f"btc-updown-5m-{name}"
    condition_id = f"cond-{name}"
    token_id = f"tok-{name}"
    base.upsert_market({
        "event_id": event_id,
        "condition_id": condition_id,
        "yes_token_id": token_id,
        "no_token_id": f"no-{name}",
        "token_mapping_status": "verified",
        "accepting_orders": True,
        "min_order_size": "5",
        "min_tick_size": "0.01",
        "taker_base_fee": "0.07",
        "fee_details": '{"fee_type":"crypto_fees_v2","fees_enabled":true,'
                       '"rate":"0.07","taker_only":true}',
    })
    base.set_state("kill_switch", "false", "operator")
    repo.set_pause_entries(False, "operator", "TEST_READY")
    base.set_state("pause_entries", "false", "operator")
    reservation = repo.reserve_event_entry(
        event_id=event_id, condition_id=condition_id, token_id=token_id,
        side="YES", simultaneous=False, reason_code="ENTRY_PRICE_EXACT",
    )
    config = LiveConfig(
        live_module_enabled=True, execution_mode="READ_ONLY",
        stop_loss_retry_delay_ms=0,
    )
    adapter = _EntryAdapter(response)
    runtime = LiveStrategyRuntime(
        config, base, repo, adapter, reconciliation=reconciliation,
    )
    market = base.latest_market(condition_id)
    runtime.set_market_provider(lambda cid: market)
    runtime.set_market_freshness_provider(
        lambda cid: {"ready": True, "reason": "READY", "book_versions": {}}
    )
    runtime.entry_schedule_status = lambda at=None: {
        "allowed": True, "reason": "ENTRY_SCHEDULE_ACTIVE",
        "timezone": "Asia/Jerusalem", "local_time": "test",
    }
    book = None
    if exit_bid is not None:
        book = {
            "asset_id": token_id, "best_bid": exit_bid, "best_ask": "0.74",
            "book_ready": True, "generation": 5, "exchange_age_ms": 10,
            "bids": [{"price": exit_bid, "size": "50"}],
            "asks": [{"price": "0.74", "size": "50"}],
        }
    runtime.set_exit_book_provider(lambda tid: dict(book) if book else None)
    return temp, base, repo, runtime, adapter, market, str(reservation["entry_intent_id"]), token_id, event_id


def _latched_update(token_id, *, ask="0.74", generation=5, age_ms=0):
    now_ms = int(time.time() * 1000)
    return {
        "asset_id": token_id,
        "condition_id": None,
        "best_ask": ask,
        "best_bid": "0.73",
        "asks": [{"price": ask, "size": "50"}],
        "generation": generation,
        "update_number": 1,
        "exchange_timestamp_ms": now_ms - age_ms,
        "message_hash": "sig-hash",
        "exchange_age_ms": age_ms,
        "_critical_trigger_latched": True,
        "_critical_entry_latched": True,
        "_critical_latched_at_ms": now_ms - age_ms,
    }


def _submit(runtime, market, side, intent_id, update):
    asyncio.run(runtime._submit_entry(
        market=market, update=update, side=side,
        intent_id=intent_id, fee_rate=Decimal("0.07"),
    ))


_MATCHED_074 = {
    "success": True, "status": "matched", "polymarket_order_id": "rem-1",
    "making_amount": "3.70", "taking_amount": "5.0",
}
_MATCHED_036 = {
    "success": True, "status": "matched", "polymarket_order_id": "rem-1",
    "making_amount": "3.80", "taking_amount": "10.555554",
}


# --------------------------------------------------------------------------
# A / B / C — signal TTL + pre-submission revalidation
# --------------------------------------------------------------------------

def test_A_stale_signal_ttl_blocks_submission():
    temp, base, repo, runtime, adapter, market, intent_id, tok, event_id = _entry_case(
        "A", response=_MATCHED_074, exit_bid="0.73",
    )
    try:
        update = _latched_update(tok, age_ms=5000)  # older than 1500ms TTL
        _submit(runtime, market, "YES", intent_id, update)
        assert adapter.create_calls == []
        intent = repo.intent(intent_id)
        assert intent["state"] in {"ZERO_FILL", "REJECTED"}
        assert intent["reason_code"] == "ENTRY_SIGNAL_EXPIRED"
        audit = repo.entry_audit(intent_id)
        assert audit["entry_validity"] == "ABORTED_SIGNAL_EXPIRED"
    finally:
        temp.cleanup()


def test_B_pre_submit_revalidation_price_moved_aborts():
    temp, base, repo, runtime, adapter, market, intent_id, tok, event_id = _entry_case(
        "B", response=_MATCHED_074,
    )
    try:
        # current book now shows 0.60, not 0.74
        runtime.set_exit_book_provider(lambda tid: {
            "asset_id": tok, "best_ask": "0.60", "best_bid": "0.58",
            "book_ready": True, "generation": 6, "exchange_age_ms": 20,
        })
        _submit(runtime, market, "YES", intent_id, _latched_update(tok))
        assert adapter.create_calls == []
        intent = repo.intent(intent_id)
        assert intent["state"] in {"ZERO_FILL", "REJECTED"}
        assert intent["reason_code"] == "ENTRY_REVALIDATION_PRICE_CHANGED"
        assert base.get_state("pause_entries") != "true"  # clean abort, no pause
    finally:
        temp.cleanup()


def test_B2_revalidation_book_not_ready_fails_closed():
    temp, base, repo, runtime, adapter, market, intent_id, tok, event_id = _entry_case(
        "B2", response=_MATCHED_074,
    )
    try:
        runtime.set_exit_book_provider(lambda tid: None)
        _submit(runtime, market, "YES", intent_id, _latched_update(tok))
        assert adapter.create_calls == []
        assert repo.intent(intent_id)["reason_code"] == (
            "ENTRY_REVALIDATION_BOOK_NOT_READY"
        )
        assert base.get_state("pause_entries") == "true"  # fail-closed
    finally:
        temp.cleanup()


def test_C_valid_revalidation_allows_submission():
    temp, base, repo, runtime, adapter, market, intent_id, tok, event_id = _entry_case(
        "C", response=_MATCHED_074, exit_bid="0.73",
    )
    try:
        runtime.set_exit_book_provider(lambda tid: {
            "asset_id": tok, "best_ask": "0.74", "best_bid": "0.73",
            "book_ready": True, "generation": 5, "exchange_age_ms": 10,
            "bids": [{"price": "0.73", "size": "10"}],
        })
        _submit(runtime, market, "YES", intent_id, _latched_update(tok))
        assert len(adapter.create_calls) == 1
        position = repo.position_for_token(tok)
        assert position is not None
        assert position["state"] in {"OPEN", "TP_OPEN", "EXITING"}
        assert position["entry_policy_status"] == "VALID"
    finally:
        temp.cleanup()


# --------------------------------------------------------------------------
# D / E — adverse fill after a valid submission
# --------------------------------------------------------------------------

def test_D_adverse_fill_publishes_and_latches_invalid_entry():
    temp, base, repo, runtime, adapter, market, intent_id, tok, event_id = _entry_case(
        "D", response=_MATCHED_036, exit_bid="0.35",
    )
    try:
        runtime.set_exit_book_provider(lambda tid: {
            "asset_id": tok, "best_ask": "0.74", "best_bid": "0.35",
            "book_ready": True, "generation": 5, "exchange_age_ms": 10,
            "bids": [{"price": "0.35", "size": "50"}],
        })
        _submit(runtime, market, "YES", intent_id, _latched_update(tok))
        position = repo.position_for_token(tok)
        assert position is not None                      # E: exists immediately
        assert position["entry_policy_status"] == "OUTSIDE_POLICY"
        assert int(position["stop_stage"]) >= 1
        assert position["exit_obligation_reason"] == "EMERGENCY_INVALID_ENTRY"
    finally:
        temp.cleanup()


def test_E_position_in_hot_state_immediately_after_fill():
    temp, base, repo, runtime, adapter, market, intent_id, tok, event_id = _entry_case(
        "E", response=_MATCHED_074, exit_bid="0.73",
    )
    try:
        _submit(runtime, market, "YES", intent_id, _latched_update(tok))
        ram = runtime._positions_from_ram(tok)
        assert len(ram) == 1
        assert ram[0]["state"] in {"OPEN", "TP_OPEN", "EXITING"}
    finally:
        temp.cleanup()


# --------------------------------------------------------------------------
# F — reconciliation recovery hot publish despite unrelated gaps
# --------------------------------------------------------------------------

def test_F_reconciliation_recovery_publishes_hot_state_despite_gaps():
    async def _gappy_reconcile(_reason):
        return {"status": "gaps", "gaps": [{"type": "unrelated"}]}

    temp, base, repo, runtime, adapter, market, intent_id, tok, event_id = _entry_case(
        "F", response={"success": True, "status": "matched",
                       "polymarket_order_id": "rem-1"},
        exit_bid="0.73", reconciliation=_gappy_reconcile,
    )
    try:
        adapter.token_balance[tok] = "5.0"       # remote truth: 5 shares held
        _submit(runtime, market, "YES", intent_id, _latched_update(tok))
        # even though reconcile returned gaps, the position is in hot state
        assert repo.position_for_token(tok) is not None
        assert len(runtime._positions_from_ram(tok)) == 1
    finally:
        temp.cleanup()


# --------------------------------------------------------------------------
# G / H — state-based STOP on recovery / restart
# --------------------------------------------------------------------------

def test_G_stop_latches_on_recovery_when_already_breached():
    temp, base, repo, runtime, position = _case(
        "G-recovery", paper=False, adapter=RecordingSellAdapter(),
        reconciliation=_ok_reconcile,
    )
    try:
        # price already below 0.66 and the position only now enters hot state
        runtime.set_exit_book_provider(lambda tid: _book(
            position["token_id"], "0.55", [("0.55", "5")], generation=3,
        ))
        asyncio.run(runtime._refresh_hot_state_once())
        asyncio.run(runtime._evaluate_new_position_exit_state(
            repo.position_for_token(position["token_id"])
        ))
        assert repo.position_for_token(position["token_id"])["stop_stage"] == 1
    finally:
        temp.cleanup()


def test_H_restart_below_sl_latches_without_new_crossing_frame():
    temp, base, repo, runtime, position = _case(
        "H-restart", paper=False, adapter=RecordingSellAdapter(),
        reconciliation=_ok_reconcile,
    )
    try:
        runtime.set_exit_book_provider(lambda tid: _book(
            position["token_id"], "0.60", [("0.60", "5")], generation=2,
        ))
        asyncio.run(runtime.start_heartbeat())
        asyncio.run(runtime.stop())
        assert repo.position_for_token(position["token_id"])["stop_stage"] == 1
    finally:
        temp.cleanup()


# --------------------------------------------------------------------------
# I / J — market-equivalent aggressive liquidation
# --------------------------------------------------------------------------

def test_I_market_sl_first_sell_is_aggressive_fak_at_floor():
    adapter = RecordingSellAdapter()
    temp, base, repo, runtime, position = _case(
        "I-market-sl", paper=False, adapter=adapter, reconciliation=_ok_reconcile,
    )
    try:
        asyncio.run(runtime._manage_position(
            market={}, update=_book(position["token_id"], "0.66", [("0.66", "5")]),
            event_ready=True, frame_hash="i",
        ))
        assert len(adapter.create_calls) == 1
        call = adapter.create_calls[0]
        assert call["order_type"] == "FAK"
        assert call["min_price"] == "0.01"
    finally:
        temp.cleanup()


def test_J_invalid_entry_liquidates_without_waiting_for_stop_threshold():
    adapter = RecordingSellAdapter()
    temp, base, repo, runtime, position = _case(
        "J-invalid", paper=False, adapter=adapter, reconciliation=_ok_reconcile,
    )
    try:
        repo.latch_invalid_entry_exit(position["position_id"])
        asyncio.run(runtime._refresh_hot_state_once())
        # bid well ABOVE the 0.66 stop threshold — a STOP would not fire here
        asyncio.run(runtime._manage_position(
            market={}, update=_book(position["token_id"], "0.80", [("0.80", "5")]),
            event_ready=True, frame_hash="j",
        ))
        assert len(adapter.create_calls) == 1
        assert adapter.create_calls[0]["purpose"] == "EMERGENCY_INVALID_ENTRY"
        assert adapter.create_calls[0]["min_price"] == "0.01"
    finally:
        temp.cleanup()


# --------------------------------------------------------------------------
# K / L / M / N — exit supervisor priority, concurrency, storm, SLA
# --------------------------------------------------------------------------

def _supervisor_case(n_dust, *, closed_dust=True):
    temp = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temp.name) / "live.sqlite3")
    base.migrate()
    repo = StrategyRepository(base)
    repo.migrate()
    for i in range(n_dust):
        ev = f"btc-updown-5m-dust{i}"
        base.upsert_market({
            "event_id": ev, "condition_id": f"c{i}", "yes_token_id": f"d{i}",
            "no_token_id": f"n{i}", "token_mapping_status": "verified",
            "accepting_orders": True, "min_order_size": "5", "min_tick_size": "0.01",
        })
        repo.reserve_event_entry(
            event_id=ev, condition_id=f"c{i}", token_id=f"d{i}", side="YES",
            simultaneous=False, reason_code="ENTRY_PRICE_EXACT",
        )
        repo.open_position(
            event_id=ev, condition_id=f"c{i}", token_id=f"d{i}", outcome="YES",
            shares=Decimal("2"), average_price=Decimal("0.5"),
            cost_all_in=Decimal("1"), fees=Decimal("0"), min_sellable=Decimal("5"),
        )
        with base.connect() as conn:
            conn.execute(
                "UPDATE live_strategy_positions SET state='DUST',"
                "remaining_shares_text='2',sellable_shares_text='2',"
                + ("closed_at=? " if closed_dust else "closed_at=NULL ")
                + "WHERE token_id=?",
                ((now_iso(), f"d{i}") if closed_dust else (f"d{i}",)),
            )
            conn.commit()
    base.upsert_market({
        "event_id": "btc-updown-5m-active", "condition_id": "c-active",
        "yes_token_id": "active", "no_token_id": "n-active",
        "token_mapping_status": "verified", "accepting_orders": True,
        "min_order_size": "5", "min_tick_size": "0.01",
    })
    repo.reserve_event_entry(
        event_id="btc-updown-5m-active", condition_id="c-active",
        token_id="active", side="YES", simultaneous=False,
        reason_code="ENTRY_PRICE_EXACT",
    )
    repo.open_position(
        event_id="btc-updown-5m-active", condition_id="c-active",
        token_id="active", outcome="YES", shares=Decimal("5"),
        average_price=Decimal("0.74"), cost_all_in=Decimal("3.7"),
        fees=Decimal("0"), min_sellable=Decimal("5"),
    )
    config = LiveConfig(
        live_module_enabled=True, execution_mode="READ_ONLY",
        stop_loss_retry_delay_ms=0,
        exit_supervisor_max_concurrent_book_fetches=3,
    )
    order = []
    concurrency = {"max": 0, "cur": 0}

    class _SlowAdapter(MockTradingAdapter):
        async def get_order_book(self, token_id):
            concurrency["cur"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["cur"])
            order.append(token_id)
            await asyncio.sleep(0.05)
            concurrency["cur"] -= 1
            return {"asset_id": token_id, "bids": [{"price": "0.70", "size": "10"}],
                    "asks": []}

    runtime = LiveStrategyRuntime(config, base, repo, _SlowAdapter(),
                                  reconciliation=_ok_reconcile)
    runtime.set_exit_book_provider(lambda tid: None)  # force REST fallback
    asyncio.run(runtime._refresh_hot_state_once())
    return temp, runtime, order, concurrency


def test_G_fifty_terminal_dust_positions_never_precede_one_open():
    temp, runtime, order, _c = _supervisor_case(50, closed_dust=True)
    try:
        asyncio.run(runtime._drive_latched_exits_once())
        assert "active" in order
        # closed DUST must not have generated REST calls at all (tier 4)
        assert set(order) == {"active"}
    finally:
        temp.cleanup()


def test_L_bounded_concurrency_never_exceeds_configured_limit():
    temp, runtime, _order, concurrency = _supervisor_case(8, closed_dust=False)
    try:
        asyncio.run(runtime._drive_latched_exits_once())
        assert concurrency["max"] <= 3
    finally:
        temp.cleanup()


def test_M_fifty_dust_positions_do_not_cause_a_request_storm():
    temp, runtime, order, concurrency = _supervisor_case(50, closed_dust=True)
    try:
        asyncio.run(runtime._drive_latched_exits_once())
        assert set(order) == {"active"}   # only the active OPEN, never 50 DUST
        assert concurrency["max"] <= 3
    finally:
        temp.cleanup()


def test_N_first_eval_sla_is_measured():
    temp, runtime, _order, _c = _supervisor_case(3, closed_dust=True)
    try:
        asyncio.run(runtime._drive_latched_exits_once())
        health = runtime.health()
        assert "exit_supervisor_max_observed_concurrency" in health
        assert health["exit_supervisor_max_concurrent_book_fetches"] == 3
    finally:
        temp.cleanup()


# --------------------------------------------------------------------------
# R / S — fee accounting
# --------------------------------------------------------------------------

def test_R_fee_enabled_taker_cannot_be_zero_verified():
    market = {
        "fee_details": '{"fees_enabled":true,"rate":"0.07"}',
        "taker_base_fee": "0.07",
    }
    trade = {"price": "0.36", "size": "10.555554", "fee_rate_bps": "0",
             "liquidity_role": "TAKER"}
    fee, status, source = resolve_trade_fee(trade, market)
    assert status != "VERIFIED"
    assert status == "COMPUTED"
    assert fee > Decimal("0.16") and fee < Decimal("0.18")


def test_S_fee_disabled_market_zero_is_verified():
    market = {"fee_details": '{"fees_enabled":false}'}
    trade = {"price": "0.5", "size": "5", "fee_rate_bps": "0",
             "liquidity_role": "TAKER"}
    fee, status, source = resolve_trade_fee(trade, market)
    assert (fee, status) == (Decimal("0"), "VERIFIED")


def test_R2_missing_evidence_is_unknown_not_verified():
    trade = {"price": "0.5", "size": "5", "fee_rate_bps": None}
    fee, status, _ = resolve_trade_fee(trade, {})
    assert status == "UNKNOWN"

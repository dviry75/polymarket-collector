"""End-to-end: entry-liquidity capture rides alongside a real BUY without
changing the entry outcome."""

import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from test_p0_live_hardening import (  # noqa: E402
    _entry_case, _latched_update, _submit, _MATCHED_074,
)

DEEP_BID_BOOK = {
    "asset_id": "tok", "best_ask": "0.74", "best_bid": "0.73",
    "book_ready": True, "generation": 5, "exchange_age_ms": 10,
    "bids": [{"price": "0.73", "size": "40"}, {"price": "0.66", "size": "20"}],
    "asks": [{"price": "0.74", "size": "40"}],
}
THIN_BID_BOOK = {
    "asset_id": "tok", "best_ask": "0.74", "best_bid": "0.67",
    "book_ready": True, "generation": 5, "exchange_age_ms": 10,
    "bids": [
        {"price": "0.67", "size": "0.5"},
        {"price": "0.55", "size": "1"},
        {"price": "0.47", "size": "3"},
    ],
    "asks": [{"price": "0.74", "size": "40"}],
}


def _run(name, book, *, capture=True):
    temp, base, repo, runtime, adapter, market, intent_id, tok, event_id = (
        _entry_case(name, response=_MATCHED_074, exit_bid="0.73")
    )
    runtime._entry_liquidity_enabled = capture
    runtime.set_exit_book_provider(lambda tid: {**book, "asset_id": tok})
    _submit(runtime, market, "YES", intent_id, _latched_update(tok))
    runtime._entry_liquidity.drain_pending()
    return temp, base, repo, runtime, adapter, intent_id, tok


def test_capture_rides_along_without_changing_the_fill():
    temp, base, repo, runtime, adapter, intent_id, tok = _run("liqA", DEEP_BID_BOOK)
    try:
        # entry outcome unchanged
        assert len(adapter.create_calls) == 1
        position = repo.position_for_token(tok)
        assert position is not None
        assert position["state"] in {"OPEN", "TP_OPEN", "EXITING"}

        row = repo.entry_audit(intent_id)
        assert row["entry_liquidity_phase_primary"] in {"SUBMIT", "REVALIDATION"}
        assert row["full_ladder_captured_synchronously"] == 1
        # both bid levels (0.73, 0.66) clear every bucket floor
        assert row["depth_at_066_text"] == "60"   # 40 @0.73 + 20 @0.66
        assert row["depth_at_046_text"] == "60"
        assert Decimal(row["sim_exit_shares_text"]) == Decimal("5")  # actual fill
        assert row["sim_exit_vwap_text"] == "0.73"
        assert row["sim_exit_method"] == "ORDER_BOOK_VWAP"
        assert row["entry_liquidity_deep_capture"] == 0
        assert repo.entry_book_snapshots(intent_id) == []
    finally:
        temp.cleanup()


def test_thin_book_flags_deep_capture_and_writes_snapshots():
    temp, base, repo, runtime, adapter, intent_id, tok = _run("liqB", THIN_BID_BOOK)
    try:
        assert repo.position_for_token(tok) is not None  # entry still worked
        row = repo.entry_audit(intent_id)
        assert row["entry_liquidity_deep_capture"] == 1
        assert row["sim_exit_method"] == "ORDER_BOOK_VWAP_PARTIAL"
        assert row["sim_exit_worst_price_text"] == "0.47"
        snaps = {s["phase"] for s in repo.entry_book_snapshots(intent_id)}
        assert "SUBMIT" in snaps
    finally:
        temp.cleanup()


def test_disabled_collector_leaves_no_liquidity_columns():
    temp, base, repo, runtime, adapter, intent_id, tok = _run(
        "liqC", DEEP_BID_BOOK, capture=False,
    )
    try:
        assert repo.position_for_token(tok) is not None
        row = repo.entry_audit(intent_id)
        # the entry path still writes the base audit row, but no liquidity data
        assert row["depth_at_066_text"] is None
        assert row["sim_exit_vwap_text"] is None
        assert row["entry_liquidity_evidence_quality"] == "PENDING"
        assert repo.entry_book_snapshots(intent_id) == []
        assert runtime._entry_liquidity.stats()["enqueued"] == 0
    finally:
        temp.cleanup()


def test_capture_failure_never_breaks_the_entry():
    temp, base, repo, runtime, adapter, market, intent_id, tok, event_id = (
        _entry_case("liqD", response=_MATCHED_074, exit_bid="0.73")
    )
    try:
        def boom(*a, **k):
            raise RuntimeError("collector down")

        runtime._entry_liquidity.note_signal = boom
        runtime._entry_liquidity.note_submit = boom
        runtime.set_exit_book_provider(lambda tid: {**DEEP_BID_BOOK, "asset_id": tok})
        _submit(runtime, market, "YES", intent_id, _latched_update(tok))
        assert len(adapter.create_calls) == 1
        assert repo.position_for_token(tok) is not None
        assert "ENTRY_LIQUIDITY_NOTE" in runtime.last_error
    finally:
        temp.cleanup()

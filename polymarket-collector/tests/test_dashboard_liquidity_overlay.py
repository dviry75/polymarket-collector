"""Dashboard overlay: entry-liquidity + stop-parity columns merged into the
external-deal table per condition_id, with graceful pre-deploy degradation."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.dashboard_read_model import DashboardReadModel
from live.repository import LiveRepository, now_iso
from live.strategy_repository import StrategyRepository


def _model(tmp):
    base = LiveRepository(Path(tmp) / "d.sqlite3")
    base.migrate()
    repo = StrategyRepository(base)
    repo.migrate()
    model = DashboardReadModel(LiveRepository(base.db_path, query_only=True))
    return base, repo, model


def test_entry_liquidity_enrichment_surfaces_measured_numbers():
    with tempfile.TemporaryDirectory() as tmp:
        base, repo, model = _model(tmp)
        repo.record_entry_audit(
            "intent-1", event_id="btc-updown-5m-1", condition_id="cond-1",
            token_id="tok-1", side="YES",
            submit_best_bid_text="0.71", submit_best_ask_text="0.74",
            submit_spread_text="0.03", submit_book_age_ms=40,
            depth_at_066_text="55", depth_at_060_text="55",
            depth_at_055_text="55", depth_at_046_text="55",
            sim_exit_shares_text="5", sim_exit_vwap_text="0.705",
            sim_exit_method="ORDER_BOOK_VWAP",
            sim_exit_fillable_shares_text="5",
            sim_exit_worst_price_text="0.70", sim_exit_slippage_text="0.005",
            sim_exit_buffers_json=json.dumps({
                "3": {"fillable_shares": "15", "vwap": "0.70"},
                "5": {"fillable_shares": "20", "vwap": "0.69"},
            }),
            entry_liquidity_phase_primary="SUBMIT",
            entry_liquidity_evidence_quality="OK",
            entry_liquidity_deep_capture=0,
            full_ladder_captured_synchronously=1,
        )
        out = model._entry_liquidity_enrichment(["cond-1", "cond-x"])
        row = out["cond-1"]
        assert row["entry_best_bid"] == 0.71
        assert row["entry_spread"] == 0.03
        assert row["entry_depth_066"] == 55.0
        assert row["entry_sim_exit_vwap"] == 0.705
        assert row["entry_sim_exit_worst_price"] == 0.70
        assert row["entry_sim_exit_fillable_3x"] == 15.0
        assert row["entry_sim_exit_fillable_5x"] == 20.0
        assert row["entry_sim_exit_vwap_5x"] == 0.69
        assert row["entry_liquidity_synchronous"] is True
        assert row["entry_liquidity_deep_capture"] is False
        assert "cond-x" not in out


def test_exit_parity_columns_are_included_when_present():
    with tempfile.TemporaryDirectory() as tmp:
        base, repo, model = _model(tmp)
        ts = now_iso()
        with repo.base.connect() as conn:
            conn.execute(
                "INSERT INTO live_strategy_exit_audit"
                "(exit_audit_id,position_id,episode_seq,event_id,condition_id,"
                " created_at,updated_at,submit_best_ask_text,"
                " submit_depth_at_060_text,expected_vwap_at_submit_text,"
                " actual_fill_vwap_text,full_ladder_captured_synchronously)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("xa-1", "pos-1", 1, "btc-updown-5m-1", "cond-1", ts, ts,
                 "0.68", "12", "0.61", "0.58", 1),
            )
            conn.commit()
        out = model._exit_audit_enrichment(["cond-1"])["cond-1"]
        assert out["stop_submit_ask"] == 0.68
        assert out["stop_submit_depth_060"] == 12.0
        assert out["stop_expected_vwap"] == 0.61
        assert out["stop_actual_vwap"] == 0.58
        assert out["stop_ladder_synchronous"] is True


def test_missing_table_and_columns_degrade_to_empty():
    with tempfile.TemporaryDirectory() as tmp:
        base = LiveRepository(Path(tmp) / "bare.sqlite3")
        base.migrate()  # no StrategyRepository.migrate -> no audit tables
        model = DashboardReadModel(LiveRepository(base.db_path, query_only=True))
        assert model._entry_liquidity_enrichment(["cond-1"]) == {}
        assert model._exit_audit_enrichment(["cond-1"]) == {}

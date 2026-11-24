"""BTC reference columns in the trade-history Excel export (temp DB, no network)."""
from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository
from tools.export_trade_history_simple import (
    BTC_COLUMNS,
    BTC_DEAL_COLUMNS,
    COLUMNS,
    attach_btc,
    load_btc_deals,
    write_workbook,
)

TP_EVENT = "btc-updown-5m-1761998400"
RES_EVENT = "btc-updown-5m-1761998700"
OLD_EVENT = "btc-updown-5m-1761900000"


def fill(action: str, slug: str) -> dict:
    row = {column: None for column in COLUMNS}
    row.update({"Action": action, "Event Slug": slug, "Outcome": "Up"})
    return row


class TradeExportBtcTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "live.sqlite3"
        base = LiveRepository(self.db)
        base.migrate()
        self.strategy = StrategyRepository(base)
        self.strategy.migrate()
        base.upsert_market({
            "event_id": TP_EVENT, "condition_id": "c-tp", "yes_token_id": "y1",
            "no_token_id": "n1", "token_mapping_status": "verified",
            "raw_market_info": {"yes_outcome_label": "Up", "no_outcome_label": "Down"},
        })
        with sqlite3.connect(self.db) as conn:
            for event, side, opened in (
                (TP_EVENT, "YES", "2026-11-01T10:04:10+00:00"),
                (RES_EVENT, "NO", "2026-11-01T10:09:05+00:00"),
                (OLD_EVENT, "YES", "2026-10-31T09:00:00+00:00"),
            ):
                conn.execute(
                    "INSERT INTO live_strategy_deals(deal_id,event_id,state,outcome,"
                    "entry_intent_id,realized_pnl_text,opened_at,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"d-{event}", event, "CLOSED", side, f"i-{event}", "0.25",
                     opened, opened, opened),
                )
        self.strategy.record_deal_btc_entry(event_id=TP_EVENT, entry_intent_id=f"i-{TP_EVENT}", values={
            "btc_open_price_text": "80000.00", "btc_entry_price_text": "80012.50",
            "btc_entry_ts_ms": 1, "btc_entry_age_ms": 300,
            "btc_entry_directional_delta_usd": "12.50",
            "btc_gate_reason": "PASS", "btc_gate_mode": "observe",
        })
        self.strategy.record_deal_btc_exit(event_id=TP_EVENT, values={
            "btc_exit_price_text": "80030.00", "btc_exit_ts_ms": 2, "btc_exit_age_ms": 150,
            "btc_exit_directional_delta_usd": "30.00", "btc_exit_type": "TP",
        })
        self.strategy.record_deal_btc_entry(event_id=RES_EVENT, entry_intent_id=f"i-{RES_EVENT}", values={
            "btc_open_price_text": "80100.00", "btc_entry_price_text": "80080.00",
            "btc_entry_directional_delta_usd": "20.00",
            "btc_gate_reason": "PASS", "btc_gate_mode": "enforce",
        })
        self.strategy.record_deal_btc_exit(event_id=RES_EVENT, values={
            "btc_exit_price_text": "80050.00", "btc_exit_directional_delta_usd": "50.00",
            "btc_exit_type": "RESOLUTION",
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_load_returns_only_deals_with_btc_data(self) -> None:
        deals, status = load_btc_deals(str(self.db))
        self.assertEqual(status, "OK (2 deals)")
        self.assertEqual(set(deals), {TP_EVENT, RES_EVENT})

    def test_buy_gets_entry_and_sell_gets_exit(self) -> None:
        deals, _ = load_btc_deals(str(self.db))
        rows = [fill("קניה", TP_EVENT), fill("מכירה", TP_EVENT), fill("קניה", OLD_EVENT)]
        self.assertEqual(attach_btc(rows, deals), 2)
        buy, sell, old = rows
        self.assertEqual(buy["BTC Open USD"], 80000.0)
        self.assertEqual(buy["BTC At Trade USD"], 80012.5)
        self.assertEqual(buy["BTC Move For Side USD"], 12.5)
        self.assertEqual(buy["BTC Gate / Exit"], "PASS (observe)")
        self.assertEqual(sell["BTC At Trade USD"], 80030.0)
        self.assertEqual(sell["BTC Move For Side USD"], 30.0)
        self.assertEqual(sell["BTC Gate / Exit"], "TP")
        self.assertTrue(all(old[column] is None for column in BTC_COLUMNS))

    def test_resolution_close_is_not_shown_as_a_sell_price(self) -> None:
        deals, _ = load_btc_deals(str(self.db))
        rows = [fill("מכירה", RES_EVENT)]
        attach_btc(rows, deals)
        self.assertEqual(rows[0]["BTC Gate / Exit"], "RESOLUTION")
        self.assertIsNone(rows[0]["BTC At Trade USD"])

    def test_db_problems_leave_columns_empty(self) -> None:
        deals, status = load_btc_deals(str(Path(self.temp.name) / "missing.sqlite3"))
        self.assertEqual(deals, {})
        self.assertTrue(status.startswith("BTC_DB_NOT_FOUND"))
        bare = Path(self.temp.name) / "bare.sqlite3"
        LiveRepository(bare).migrate()
        StrategyRepository(LiveRepository(bare)).migrate()
        with sqlite3.connect(bare) as conn:
            conn.execute("ALTER TABLE live_strategy_deals DROP COLUMN btc_exit_type")
        self.assertEqual(load_btc_deals(str(bare)), ({}, "BTC_COLUMNS_NOT_MIGRATED"))

    def test_db_is_opened_read_only(self) -> None:
        before = self.db.read_bytes()
        os.chmod(self.db, stat.S_IRUSR)
        try:
            deals, status = load_btc_deals(str(self.db))
        finally:
            os.chmod(self.db, stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(len(deals), 2, status)
        self.assertEqual(self.db.read_bytes(), before)

    def test_workbook_has_btc_columns_and_deals_sheet(self) -> None:
        deals, _ = load_btc_deals(str(self.db))
        rows = [fill("קניה", TP_EVENT), fill("מכירה", TP_EVENT)]
        attach_btc(rows, deals)
        path = Path(self.temp.name) / "out.xlsx"
        write_workbook(rows, path, deals)
        book = load_workbook(path)
        self.assertEqual(book.sheetnames, ["Trade History", "BTC Deals"])
        history = list(book["Trade History"].values)
        self.assertEqual(list(history[0]), COLUMNS + BTC_COLUMNS)
        self.assertEqual(history[1][len(COLUMNS) + 1], 80012.5)
        self.assertEqual(history[2][len(COLUMNS) + 1], 80030.0)
        deal_rows = list(book["BTC Deals"].values)
        self.assertEqual(list(deal_rows[0]), BTC_DEAL_COLUMNS)
        by_slug = {row[1]: dict(zip(BTC_DEAL_COLUMNS, row)) for row in deal_rows[1:]}
        self.assertEqual(by_slug[TP_EVENT]["Side"], "Up")
        self.assertEqual(by_slug[TP_EVENT]["Opened Jerusalem"], "01/11/2026 12:04:10")
        self.assertEqual(by_slug[RES_EVENT]["Exit Type"], "RESOLUTION")
        self.assertEqual(by_slug[RES_EVENT]["BTC Exit USD"], 80050.0)

    def test_workbook_without_db_keeps_single_sheet(self) -> None:
        rows = [fill("קניה", TP_EVENT)]
        attach_btc(rows, {})
        path = Path(self.temp.name) / "plain.xlsx"
        write_workbook(rows, path, {})
        book = load_workbook(path)
        self.assertEqual(book.sheetnames, ["Trade History"])
        self.assertEqual(len(next(book["Trade History"].values)), len(COLUMNS) + len(BTC_COLUMNS))


if __name__ == "__main__":
    unittest.main()

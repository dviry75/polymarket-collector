"""The "עסקאות" sheet of the e-mailed export mirrors the dashboard deal table."""

from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile

from openpyxl import load_workbook

from live.deal_table_export import (
    COLUMNS, GREEN, RED, SHEET_TITLE, TABLE_COLUMNS, deal_status, entry_liquidity,
    load_deal_items, market_result, table_row,
)
from live.repository import LiveRepository
from tools.export_trade_history_simple import write_workbook

# The dashboard's <th> list in app/page.tsx (polymarket-dashboard repo).
DASHBOARD_HEADERS = (
    "#", "התחלה", "סיום", "שוק", "טוקן", "פעולות", "קנייה $", "מחיר קנייה",
    "מכירה $", "מחיר מכירה", "עמלות", "תזרים", "נזילות כניסה", "BTC בכניסה",
    "BTC ביציאה", "תוצאת שוק", "סטטוס",
)


def test_table_columns_are_the_dashboard_columns_in_order():
    assert TABLE_COLUMNS == DASHBOARD_HEADERS
    assert COLUMNS[: len(TABLE_COLUMNS)] == DASHBOARD_HEADERS


def test_status_mirrors_dashboard_labels():
    assert deal_status({}) == ("—", "", "")
    assert deal_status({"exit_root_cause": "HEALTHY"}) == ("תקין", "positive", "HEALTHY")
    assert deal_status({"exit_root_cause": "MARKET", "exit_technical_behavior": "SETTLEMENT_WAIT"}) \
        == ("המתנה לסליקה", "positive", "SETTLEMENT_WAIT")
    assert deal_status({"exit_root_cause": "MARKET", "exit_analysis_outcome": "NO_EXECUTION"}) \
        == ("לא בוצע", "negative", "NO_EXECUTION")
    assert deal_status({"exit_root_cause": "DETECTION_DELAY",
                        "exit_evidence_quality": "RECONSTRUCTED_X"}) \
        == ("איחור זיהוי ~", "negative", "DETECTION_DELAY")


def test_entry_liquidity_mirrors_dashboard_cell():
    assert entry_liquidity({}) == ("—", "")
    assert entry_liquidity({
        "entry_sim_exit_worst_price": 0.62, "entry_sim_exit_shares": 10,
        "entry_sim_exit_fillable_1x": 10, "entry_liquidity_synchronous": True,
    }) == ("0.62 · 10/10", "")
    assert entry_liquidity({
        "entry_sim_exit_vwap": 0.7, "entry_sim_exit_shares": 10,
        "entry_sim_exit_fillable_1x": 4, "entry_liquidity_synchronous": False,
    }) == ("0.7 · 4/10 ~", "negative")


def test_market_result_green_when_ours_red_when_other_side():
    assert market_result({"outcomes": "Up", "market_outcome": "Up"}) == ("UP", "positive")
    assert market_result({"outcomes": "Up", "market_outcome": "Down"}) == ("DOWN", "negative")
    assert market_result({"outcomes": "Up", "market_outcome": None}) == ("—", "")
    assert market_result({"outcomes": "Up,Down", "market_outcome": "Down"}) == ("DOWN", "")


def test_btc_cells_are_real_move_and_directional_extras():
    row = table_row({
        "seq": 7, "outcomes": "Down", "cash_flow_usd": -1.5,
        "btc": {"btc_open_price": 80000, "btc_entry_price": 79970,
                "btc_entry_directional_delta_usd": 30, "btc_exit_type": "RESOLUTION",
                "btc_exit_price": 80010, "btc_exit_directional_delta_usd": -10,
                "btc_gate_reason": "BTC_GATE_PASS", "btc_gate_mode": "enforce"},
    })
    assert row["BTC בכניסה"] == (-30, "negative")      # BTC went down
    assert row["BTC לטובת העסקה בכניסה"][0] == 30     # in favour of Down
    assert row["BTC ביציאה"] == (10, "positive")
    assert row["סוג יציאה BTC"][0] == "הכרעה"
    assert row["שער BTC"][0] == "BTC_GATE_PASS (enforce)"
    assert row["תזרים"] == (-1.5, "negative")


def test_workbook_first_sheet_is_the_deal_table_from_the_read_model():
    from scripts.migrate_external_fills import DDL
    from test_dashboard_v1 import build_db, seed_verified_lifecycle

    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp)
        seed_verified_lifecycle(repo)
        repo.upsert_market({
            "event_id": "evt-a", "condition_id": "cond-a", "yes_token_id": "y", "no_token_id": "n",
            "market_resolved": True, "winning_outcome": "Down",
        })
        with sqlite3.connect(repo.db_path) as conn:
            conn.executescript(DDL)
            for trade_id, side, price, notional, at in (
                ("t1", "BUY", 0.74, 7.4, "2026-11-24T18:23:47+00:00"),
                ("t2", "SELL", 0.40, 4.0, "2026-11-24T18:24:30+00:00"),
            ):
                conn.execute(
                    "INSERT INTO external_fills(trade_id,condition_id,event_slug,market_title,side,"
                    "outcome,price,size,notional_usd,fee_usd,matched_at,raw_json,ingested_at)"
                    " VALUES(?,'cond-a','btc-updown-5m-1795544400','Bitcoin Up or Down',?,'Up',?,10,?,"
                    "0.05,?,'{}',?)",
                    (trade_id, side, price, notional, at, at),
                )
        items, status = load_deal_items(repo.db_path)
        assert status.startswith("OK (1 deals")
        path = Path(tmp) / "export.xlsx"
        write_workbook([], path, None, items)

        workbook = load_workbook(path)
        assert workbook.sheetnames[0] == SHEET_TITLE
        assert workbook.active.title == SHEET_TITLE
        sheet = workbook[SHEET_TITLE]
        assert sheet.sheet_view.rightToLeft
        assert tuple(cell.value for cell in sheet[1]) == COLUMNS
        values = {column: cell for column, cell in zip(COLUMNS, sheet[2])}
        assert values["שוק"].value == "Bitcoin Up or Down"
        assert values["טוקן"].value == "Up"
        assert values["פעולות"].value == 2
        assert values["קנייה $"].value == 7.4
        assert values["מחיר קנייה"].value == 0.74
        assert values["מחיר מכירה"].value == 0.4
        assert abs(values["תזרים"].value - (4.0 - 7.4 - 0.1)) < 1e-9
        assert values["תזרים"].font.color.rgb.endswith(RED)
        assert values["התחלה"].value == datetime(2026, 11, 24, 20, 23, 47)  # Jerusalem
        assert values["תוצאת שוק"].value == "DOWN"
        assert values["תוצאת שוק"].font.color.rgb.endswith(RED)  # bought Up
        assert values["Event Slug"].value == "btc-updown-5m-1795544400"


def test_deal_table_failure_does_not_break_the_export():
    items, status = load_deal_items("/nonexistent/dir/db.sqlite3")
    assert items == [] and status.startswith("DEAL_TABLE_UNAVAILABLE")
    assert GREEN != RED

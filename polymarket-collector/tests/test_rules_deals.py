import sqlite3
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import load_workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import app


def valid_rule(**overrides):
    payload = {
        "name": "demo rule",
        "entry_price": "0.77",
        "stop_loss_price": "0.60",
        "take_profit_price": "0.90",
        "max_yes_entries_per_event": 1,
        "max_no_entries_per_event": 1,
        "status": "active",
    }
    payload.update(overrides)
    return payload


def orderbook(event_id, *, yes_ask=None, yes_bid=None, no_ask=None, no_bid=None, sampled_at=None):
    return {
        "sampled_at": sampled_at or "2026-07-17T09:00:01+00:00",
        "sampled_at_local": "17/07/2026 12:00:01",
        "event_slug": event_id,
        "condition_id": f"condition-{event_id}",
        "up_token_id": "yes-token",
        "down_token_id": "no-token",
        "up_best_ask": yes_ask,
        "up_best_bid": yes_bid,
        "down_best_ask": no_ask,
        "down_best_bid": no_bid,
        "up_last_trade_price": None,
        "down_last_trade_price": None,
        "up_spread": None,
        "down_spread": None,
        "up_midpoint": None,
        "down_midpoint": None,
        "raw_up_timestamp": None,
        "raw_down_timestamp": None,
        "up_volume_shares_10s": 0.0,
        "down_volume_shares_10s": 0.0,
        "up_volume_usdc_10s": 0.0,
        "down_volume_usdc_10s": 0.0,
        "trades_count_10s": 0,
        "trades_window_start": None,
        "trades_window_start_local": None,
        "trades_window_end": None,
        "trades_window_end_local": None,
        "trades_error": None,
        "status": "success",
        "error": None,
    }


class RuleDealTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = app.DB_PATH
        self.original_export_dir = app.EXPORT_DIR
        self.original_active_market = app.active_market
        self.temp_dir = tempfile.TemporaryDirectory()
        temp_path = Path(self.temp_dir.name)
        app.DB_PATH = temp_path / "test.sqlite3"
        app.EXPORT_DIR = temp_path / "output"
        app.active_market = None
        app.init_db()

    def tearDown(self):
        app.DB_PATH = self.original_db_path
        app.EXPORT_DIR = self.original_export_dir
        app.active_market = self.original_active_market
        self.temp_dir.cleanup()

    def fetch_deals(self):
        with app.get_conn() as conn:
            return conn.execute("SELECT * FROM deals ORDER BY id").fetchall()

    def test_rule_validation_and_deactivation_api(self):
        client = TestClient(app.app)

        response = client.post("/rules", json=valid_rule())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "active")

        invalid_cases = [
            valid_rule(name=""),
            valid_rule(entry_price="0.5"),
            valid_rule(stop_loss_price="0.80"),
            valid_rule(take_profit_price="0.70"),
            valid_rule(max_yes_entries_per_event=-1),
            valid_rule(status="paused"),
        ]
        for payload in invalid_cases:
            self.assertEqual(client.post("/rules", json=payload).status_code, 400)

        inactive = client.post("/rules", json=valid_rule(name="inactive", status="inactive"))
        self.assertEqual(inactive.status_code, 200)
        inactive_id = inactive.json()["id"]

        first = client.post(f"/rules/{inactive_id}/deactivate")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["message"], "Rule is already inactive")
        self.assertEqual(client.post("/rules/9999/deactivate").status_code, 404)
        self.assertIn(client.put(f"/rules/{inactive_id}", json={"name": "changed"}).status_code, {404, 405})

    def test_active_rule_starts_only_on_next_event_and_survives_restart(self):
        app.active_market = {"event_slug": "event-a", "condition_id": "condition-a"}
        rule = app.create_rule(valid_rule())
        self.assertEqual(rule["eligible_after_event_id"], "event-a")

        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.77, yes_bid=0.76, no_ask=0.2, no_bid=0.19))
        self.assertEqual(len(self.fetch_deals()), 0)

        app.active_market = None
        app.insert_orderbook_log(orderbook("event-b", yes_ask=0.77, yes_bid=0.76, no_ask=0.2, no_bid=0.19))
        deals = self.fetch_deals()
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0]["side"], "yes")

    def test_entry_rules_exact_prices_open_deal_limits_and_duplicates(self):
        app.create_rule(valid_rule(max_yes_entries_per_event=1, max_no_entries_per_event=1))

        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.76, yes_bid=0.75, no_ask=0.2, no_bid=0.19))
        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.78, yes_bid=0.75, no_ask=0.2, no_bid=0.19))
        self.assertEqual(len(self.fetch_deals()), 0)

        first_sample = orderbook("event-a", yes_ask="0.770000", yes_bid=0.76, no_ask=0.2, no_bid=0.19)
        log_id = app.insert_orderbook_log(first_sample)
        with app.get_conn() as conn:
            app.process_demo_trading_for_orderbook(conn, first_sample, log_id)
            conn.commit()
        self.assertEqual(len(self.fetch_deals()), 1)

        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.77, yes_bid=0.76, no_ask=0.2, no_bid=0.19))
        self.assertEqual(len(self.fetch_deals()), 1)

    def test_no_entry_for_inactive_open_deal_and_both_sides_match(self):
        app.create_rule(valid_rule(name="inactive", status="inactive"))
        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.77, yes_bid=0.76, no_ask=0.2, no_bid=0.19))
        self.assertEqual(len(self.fetch_deals()), 0)

        app.create_rule(valid_rule())
        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.77, yes_bid=0.76, no_ask=0.77, no_bid=0.76))
        self.assertEqual(len(self.fetch_deals()), 0)

    def test_two_rules_can_open_on_same_sample_and_side_quotas_reset_per_event(self):
        app.create_rule(valid_rule(name="rule 1", max_yes_entries_per_event=1, max_no_entries_per_event=1))
        app.create_rule(valid_rule(name="rule 2", max_yes_entries_per_event=1, max_no_entries_per_event=1))

        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.77, yes_bid=0.76, no_ask=0.2, no_bid=0.19))
        self.assertEqual(len(self.fetch_deals()), 2)

        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.2, yes_bid=0.91, no_ask=0.2, no_bid=0.19))
        app.insert_orderbook_log(orderbook("event-a", no_ask=0.77, no_bid=0.76, yes_ask=0.2, yes_bid=0.19))
        self.assertEqual(len(self.fetch_deals()), 4)

        app.insert_orderbook_log(orderbook("event-a", no_ask=0.2, no_bid=0.91, yes_ask=0.2, yes_bid=0.19))
        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.77, yes_bid=0.76, no_ask=0.2, no_bid=0.19))
        self.assertEqual(len(self.fetch_deals()), 4)

        app.insert_orderbook_log(orderbook("event-b", yes_ask=0.77, yes_bid=0.76, no_ask=0.2, no_bid=0.19))
        self.assertEqual(len(self.fetch_deals()), 6)

    def test_take_profit_and_stop_loss_use_same_side_bid_and_target_exit_price(self):
        app.create_rule(valid_rule())
        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.77, yes_bid=0.76, no_ask=0.2, no_bid=0.99))
        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.2, yes_bid=0.93, no_ask=0.2, no_bid=0.1))
        deal = self.fetch_deals()[0]
        self.assertEqual(deal["result"], "win")
        self.assertEqual(deal["exit_reason"], "take_profit")
        self.assertEqual(deal["exit_price"], 0.9)
        self.assertIsNotNone(deal["exit_orderbook_log_id"])
        self.assertAlmostEqual(deal["price_change_points"], 13.0)

        app.create_rule(valid_rule(name="no side", entry_price="0.74", stop_loss_price="0.65", take_profit_price="0.95"))
        app.insert_orderbook_log(orderbook("event-b", no_ask=0.74, no_bid=0.73, yes_ask=0.2, yes_bid=0.99))
        app.insert_orderbook_log(orderbook("event-b", no_ask=0.2, no_bid=0.57, yes_ask=0.2, yes_bid=0.99))
        no_deal = self.fetch_deals()[-1]
        self.assertEqual(no_deal["side"], "no")
        self.assertEqual(no_deal["result"], "loss")
        self.assertEqual(no_deal["exit_reason"], "stop_loss")
        self.assertEqual(no_deal["exit_price"], 0.65)
        self.assertAlmostEqual(no_deal["price_change_points"], 9.0)

    def test_stop_loss_wins_if_both_thresholds_are_seen(self):
        with app.get_conn() as conn:
            now = app.now_iso()
            cursor = conn.execute("""
                INSERT INTO rules (
                    name, created_at, updated_at, entry_price, stop_loss_price,
                    take_profit_price, max_yes_entries_per_event,
                    max_no_entries_per_event, status, eligible_after_event_id
                ) VALUES ('legacy-invalid', ?, ?, 0.77, 0.80, 0.70, 1, 1, 'active', NULL)
            """, (now, now))
            rule_id = cursor.lastrowid
            conn.execute("""
                INSERT INTO deals (
                    rule_id, rule_name, event_id, side, result, entry_at, entry_price,
                    entry_orderbook_log_id, created_at, updated_at
                ) VALUES (?, ?, 'event-a', 'yes', 'open', ?, 0.77, 1, ?, ?)
            """, (rule_id, "legacy-invalid", now, now, now))
            conn.commit()

        app.insert_orderbook_log(orderbook("event-a", yes_bid=0.75, yes_ask=0.2, no_bid=0.1, no_ask=0.2))
        deal = self.fetch_deals()[0]
        self.assertEqual(deal["result"], "loss")
        self.assertEqual(deal["exit_reason"], "stop_loss")
        self.assertEqual(deal["exit_price"], 0.8)

    def test_event_resolution_closes_open_deals_once(self):
        app.create_rule(valid_rule())
        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.77, yes_bid=0.76, no_ask=0.2, no_bid=0.19))
        market_row = {
            "polymarket_event_id": "poly-event",
            "polymarket_market_id": "poly-market",
            "condition_id": "condition-a",
            "event_slug": "event-a",
            "market_slug": "event-a",
            "title": "title",
            "question": "question",
            "event_url": "url",
            "start_time": "2026-07-17T09:00:00Z",
            "start_time_local": "17/07/2026 12:00:00",
            "end_time": "2026-07-17T09:05:00Z",
            "end_time_local": "17/07/2026 12:05:00",
            "yes_token_id": "yes-token",
            "no_token_id": "no-token",
            "outcomes": '["Up", "Down"]',
            "outcome_prices": '["1", "0"]',
            "active": 0,
            "closed": 1,
            "enable_order_book": 1,
            "accepting_orders": 0,
            "created_at_poly": "2026-07-17T08:59:00Z",
            "created_at_poly_local": "17/07/2026 11:59:00",
            "status": "closed",
            "notes": "",
            "raw_json": "{}",
        }
        app.upsert_event(market_row)
        app.upsert_event(market_row)
        deal = self.fetch_deals()[0]
        self.assertEqual(deal["result"], "win")
        self.assertEqual(deal["exit_reason"], "event_resolution")
        self.assertEqual(deal["exit_price"], 1)
        self.assertEqual(deal["market_result"], "yes")
        self.assertIsNone(deal["exit_orderbook_log_id"])

    def test_deactivate_rule_keeps_open_deal_closing_but_blocks_new_entries(self):
        rule = app.create_rule(valid_rule(max_yes_entries_per_event=2))
        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.77, yes_bid=0.76, no_ask=0.2, no_bid=0.19))
        app.deactivate_rule(rule["id"])
        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.2, yes_bid=0.93, no_ask=0.2, no_bid=0.19))
        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.77, yes_bid=0.76, no_ask=0.2, no_bid=0.19))
        deals = self.fetch_deals()
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0]["result"], "win")

    def test_deal_metric_examples(self):
        points, percent = app.calculate_deal_metrics(0.77, 0.95)
        self.assertEqual(points, 18.0)
        self.assertAlmostEqual(percent, ((0.95 - 0.77) / 0.77) * 100)

        points, percent = app.calculate_deal_metrics(0.74, 0.65)
        self.assertEqual(points, 9.0)
        self.assertAlmostEqual(percent, ((0.65 - 0.74) / 0.74) * 100)

        points, percent = app.calculate_deal_metrics(0.77, 1)
        self.assertEqual(points, 23.0)
        self.assertAlmostEqual(percent, ((1 - 0.77) / 0.77) * 100)

        points, percent = app.calculate_deal_metrics(0.77, 0)
        self.assertEqual(points, 77.0)
        self.assertAlmostEqual(percent, ((0 - 0.77) / 0.77) * 100)

        pnl, roi, shares = app.calculate_deal_pnl_usd(0.77, 0.90, 1)
        self.assertAlmostEqual(shares, 1 / 0.77)
        self.assertAlmostEqual(pnl, (1 / 0.77) * 0.90 - 1)
        self.assertAlmostEqual(roi, ((0.90 - 0.77) / 0.77) * 100)

        pnl, roi, shares = app.calculate_deal_pnl_usd(0.74, 0.65, 1)
        self.assertAlmostEqual(shares, 1 / 0.74)
        self.assertAlmostEqual(pnl, (1 / 0.74) * 0.65 - 1)
        self.assertAlmostEqual(roi, ((0.65 - 0.74) / 0.74) * 100)

    def test_excel_and_dashboard_include_rules_and_deals(self):
        active = app.create_rule(valid_rule(name="active"))
        app.create_rule(valid_rule(name="inactive", status="inactive"))
        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.77, yes_bid=0.76, no_ask=0.2, no_bid=0.19))
        app.insert_orderbook_log(orderbook("event-a", yes_ask=0.2, yes_bid=0.93, no_ask=0.2, no_bid=0.19))
        app.deactivate_rule(active["id"])

        overview = app.load_dashboard_overview(1)
        self.assertEqual(overview["closed_deals"], 1)
        self.assertEqual(overview["open_deals"], 0)
        self.assertEqual(overview["wins"], 1)
        self.assertAlmostEqual(overview["net_pnl_usd"], (1 / 0.77) * 0.90 - 1)

        rules_performance = app.load_rules_performance(1)
        self.assertEqual(len(rules_performance), 2)
        self.assertEqual(rules_performance[0]["rule_name"], "active")
        self.assertEqual(rules_performance[0]["closed_deals"], 1)
        self.assertEqual(rules_performance[0]["wins"], 1)
        self.assertAlmostEqual(rules_performance[0]["net_pnl_usd"], (1 / 0.77) * 0.90 - 1)
        self.assertEqual(rules_performance[1]["rule_name"], "inactive")
        self.assertEqual(rules_performance[1]["closed_deals"], 0)

        risk = app.load_risk_snapshot(1)
        self.assertEqual(risk["closed_deals"], 1)
        self.assertAlmostEqual(risk["ending_equity_usd"], (1 / 0.77) * 0.90 - 1)
        self.assertAlmostEqual(risk["max_drawdown_usd"], 0)
        self.assertEqual(risk["best_deal"]["deal_id"], 1)
        self.assertEqual(risk["worst_deal"]["deal_id"], 1)
        self.assertEqual(risk["exit_reasons"][0]["exit_reason"], "take_profit")

        conditions = app.load_market_conditions(1)
        self.assertEqual(conditions["by_side"][0]["label"], "YES")
        self.assertEqual(conditions["by_side"][0]["closed_deals"], 1)
        self.assertAlmostEqual(conditions["by_side"][0]["net_pnl_usd"], (1 / 0.77) * 0.90 - 1)
        self.assertEqual(conditions["by_entry_price"][0]["label"], "0.70-0.79")
        self.assertEqual(conditions["by_entry_price"][0]["closed_deals"], 1)

        export_path, row_counts = app.write_xlsx_export()
        self.assertEqual(row_counts["rules"], 2)
        self.assertEqual(row_counts["deals"], 1)
        workbook = load_workbook(export_path, read_only=True)
        try:
            self.assertEqual(workbook.sheetnames, ["events", "orderbook_log", "btc_volume_log", "rules", "deals"])
        finally:
            workbook.close()

        client = TestClient(app.app)
        dashboard = client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        html = dashboard.text
        self.assertIn("<h2>Executive Overview</h2>", html)
        self.assertIn("<h2>Rules Performance</h2>", html)
        self.assertIn("<h2>Risk Snapshot</h2>", html)
        self.assertIn("<h2>Market Conditions</h2>", html)
        self.assertIn("Performance by Side", html)
        self.assertIn("0.70-0.79", html)
        self.assertIn("Exit Reasons", html)
        self.assertIn("Net P&amp;L", html)
        self.assertIn("Avg ROI", html)
        self.assertIn("$0.17", html)
        self.assertIn("<h2>Rules</h2>", html)
        self.assertIn("<h2>Deals</h2>", html)
        self.assertIn("Create Rule", html)
        self.assertNotIn('http-equiv="refresh"', html)
        self.assertIn("refreshDashboardContent", html)

        dashboard_content = client.get("/dashboard-content")
        self.assertEqual(dashboard_content.status_code, 200)
        self.assertIn("<h2>Executive Overview</h2>", dashboard_content.text)
        self.assertIn("<h2>Rules Performance</h2>", dashboard_content.text)
        self.assertIn("<h2>Risk Snapshot</h2>", dashboard_content.text)
        self.assertIn("<h2>Market Conditions</h2>", dashboard_content.text)
        self.assertIn("<h2>Rules</h2>", dashboard_content.text)
        self.assertIn("<h2>Deals</h2>", dashboard_content.text)

        app.export_state.update({"status": "ready", "filename": export_path.name, "path": str(export_path)})
        download = client.get("/download.xlsx")
        self.assertEqual(download.status_code, 200)
        downloaded = load_workbook(BytesIO(download.content), read_only=True)
        try:
            self.assertEqual(downloaded.sheetnames, ["events", "orderbook_log", "btc_volume_log", "rules", "deals"])
        finally:
            downloaded.close()


if __name__ == "__main__":
    unittest.main()

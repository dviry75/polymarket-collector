import hashlib
import inspect
import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import live_app
from live.config import LiveConfig
from live.market_websocket import MarketWebSocketManager
from live.paper_trading import PaperTradingEngine
from live.repository import LiveRepository
from live.router import configure


class PaperTradingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "live.sqlite3"
        self.repo = LiveRepository(self.db_path)
        self.repo.migrate()
        self.repo.upsert_market({
            "event_id": "btc-updown-5m-next",
            "condition_id": "condition-next",
            "yes_token_id": "yes-token",
            "no_token_id": "no-token",
            "token_mapping_status": "verified",
            "accepting_orders": True,
            "market_resolved": False,
            "source": "gamma_public_read_only",
        })
        self.engine = PaperTradingEngine(
            self.repo, enabled=True, max_market_age_seconds=10_000
        )
        self.manager = MarketWebSocketManager(
            self.repo, stale_after_seconds=10_000, on_snapshot=self.engine.process_snapshot
        )

    def tearDown(self):
        self.temp.cleanup()

    def create_rule(self, **overrides):
        payload = {
            "name": "paper 0.74",
            "entry_price": 0.74,
            "stop_loss_price": 0.65,
            "take_profit_price": 0.97,
            "requested_amount_usd": 1,
            "status": "active",
            "execution_mode": "PAPER_TRADING",
        }
        payload.update(overrides)
        return self.repo.create_rule(payload)

    @staticmethod
    def book(asset_id, bid, ask, timestamp):
        return {
            "event_type": "book",
            "asset_id": asset_id,
            "market": "condition-next",
            "bids": [{"price": str(bid), "size": "20"}],
            "asks": [{"price": str(ask), "size": "20"}],
            "timestamp": str(timestamp),
        }

    def test_market_ws_snapshot_opens_and_closes_paper_deal_without_orders(self):
        self.create_rule()
        self.assertTrue(self.manager.process_message(self.book("no-token", 0.25, 0.26, 1)))
        self.assertTrue(self.manager.process_message(self.book("yes-token", 0.73, 0.74, 2)))

        deals = self.repo.list_table("live_deals", 10)
        self.assertEqual(len(deals), 1)
        opened = deals[0]
        self.assertEqual(opened["execution_mode"], "PAPER_TRADING")
        self.assertEqual(opened["status"], "open")
        self.assertEqual(opened["price_source"], "POLYMARKET_MARKET_WS")
        self.assertAlmostEqual(opened["filled_size"], 1 / 0.74)
        self.assertEqual(opened["paper_fill_status"], "full")
        self.assertGreater(opened["fees_usd"], 0)
        self.assertEqual(opened["fee_source"], "SIMULATED_CRYPTO_DEFAULT")

        self.assertTrue(self.manager.process_message(self.book("yes-token", 0.65, 0.66, 3)))
        closed = self.repo.list_table("live_deals", 10)[0]
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["exit_reason"], "stop_loss")
        self.assertAlmostEqual(closed["average_exit_fill_price"], 0.65)
        self.assertLess(closed["net_pnl_usd"], 0)
        self.assertLess(closed["net_pnl_usd"], closed["gross_pnl_usd"])

        self.assertEqual(self.repo.list_table("live_orders", 10), [])
        self.assertEqual(self.repo.list_table("live_order_fills", 10), [])
        self.assertGreaterEqual(len(self.repo.list_table("live_rule_evaluations", 10)), 2)
        self.assertEqual(self.engine.health()["write_dependencies"], [])

    def test_exact_entry_does_not_open_on_crossing_and_deduplicates_exact_updates(self):
        self.create_rule()
        self.manager.process_message(self.book("no-token", 0.25, 0.26, 4))
        self.manager.process_message(self.book("yes-token", 0.72, 0.73, 5))
        self.manager.process_message(self.book("yes-token", 0.74, 0.75, 6))
        self.assertEqual(self.repo.open_paper_deals(), [])

        self.manager.process_message(self.book("yes-token", 0.73, 0.74, 7))
        self.manager.process_message(self.book("yes-token", 0.73, 0.74, 8))
        self.manager.process_message(self.book("yes-token", 0.73, 0.74, 9))
        deals = self.repo.open_paper_deals()
        self.assertEqual(len(deals), 1)
        self.assertEqual(Decimal(str(deals[0]["average_entry_fill_price"])), Decimal("0.74"))

    def test_take_profit_executes_at_real_best_bid_and_pnl_uses_execution_price(self):
        self.create_rule(take_profit_price=0.95)
        self.manager.process_message(self.book("no-token", 0.25, 0.26, 40))
        self.manager.process_message(self.book("yes-token", 0.73, 0.74, 41))
        self.manager.process_message(self.book("yes-token", 0.94, 0.96, 42))

        deal = self.repo.open_paper_deals()[0]
        snapshot = self.repo.latest_market_snapshot("yes-token")
        execution_price, fill_method = self.engine._executable_exit_price(deal, snapshot)
        self.repo.close_paper_deal(
            deal,
            snapshot,
            reason="take_profit",
            trigger_price=Decimal("0.95"),
            exit_price=execution_price,
            fill_method=fill_method,
        )

        deal = self.repo.list_table("live_deals", 1)[0]
        self.assertEqual(deal["exit_reason"], "take_profit")
        self.assertEqual(Decimal(str(deal["requested_exit_price"])), Decimal("0.95"))
        self.assertEqual(Decimal(str(deal["average_exit_fill_price"])), Decimal("0.94"))
        expected_gross = Decimal(str(deal["filled_size"])) * Decimal("0.94") - Decimal("1")
        self.assertAlmostEqual(deal["gross_pnl_usd"], float(expected_gross))

    def test_stop_loss_executes_at_real_best_bid(self):
        self.create_rule(stop_loss_price=0.65)
        self.manager.process_message(self.book("no-token", 0.25, 0.26, 50))
        self.manager.process_message(self.book("yes-token", 0.73, 0.74, 51))
        self.manager.process_message(self.book("yes-token", 0.63, 0.64, 52))

        deal = self.repo.list_table("live_deals", 1)[0]
        self.assertEqual(deal["exit_reason"], "stop_loss")
        self.assertEqual(Decimal(str(deal["requested_exit_price"])), Decimal("0.65"))
        self.assertEqual(Decimal(str(deal["average_exit_fill_price"])), Decimal("0.63"))

    def test_exit_uses_order_book_vwap_when_depth_is_sufficient(self):
        self.create_rule(take_profit_price=0.93, requested_amount_usd=2)
        self.manager.process_message(self.book("no-token", 0.25, 0.26, 60))
        self.manager.process_message(self.book("yes-token", 0.73, 0.74, 61))
        exit_book = self.book("yes-token", 0.94, 0.96, 62)
        exit_book["bids"] = [
            {"price": "0.94", "size": "1"},
            {"price": "0.93", "size": "10"},
        ]
        self.manager.process_message(exit_book)

        deal = self.repo.list_table("live_deals", 1)[0]
        size = Decimal("2") / Decimal("0.74")
        expected = (Decimal("0.94") + (size - Decimal("1")) * Decimal("0.93")) / size
        self.assertAlmostEqual(deal["average_exit_fill_price"], float(expected))
        audit = next(
            row for row in self.repo.list_table("live_audit_log", 10)
            if row["action"] == "paper_deal_closed"
        )
        details = json.loads(audit["details_json"])
        self.assertEqual(details["fill_method"], "ORDER_BOOK_VWAP")

    def test_no_valid_bid_does_not_invent_exit_fill(self):
        self.create_rule()
        self.manager.process_message(self.book("no-token", 0.25, 0.26, 70))
        self.manager.process_message(self.book("yes-token", 0.73, 0.74, 71))
        snapshot = self.repo.store_market_snapshot({
            "condition_id": "condition-next",
            "event_id": "btc-updown-5m-next",
            "asset_id": "yes-token",
            "outcome": "YES",
            "event_type": "price_change",
            "best_bid": None,
            "best_ask": 0.66,
            "bids": [],
            "asks": [],
            "received_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
        })
        self.engine.process_snapshot(snapshot)

        deal = self.repo.list_table("live_deals", 1)[0]
        self.assertEqual(deal["status"], "open")
        self.assertIsNone(deal["average_exit_fill_price"])
        audit = self.repo.list_table("live_audit_log", 1)[0]
        self.assertEqual(audit["reason"], "NO_VALID_FRESH_BID")

    def test_both_sides_match_is_fail_closed(self):
        rule = self.create_rule(status="inactive")
        self.manager.process_message(self.book("no-token", 0.73, 0.74, 10))
        self.manager.process_message(self.book("yes-token", 0.72, 0.75, 11))
        self.repo.update_rule_status(rule["id"], "active")

        self.manager.process_message(self.book("yes-token", 0.73, 0.74, 12))
        self.assertEqual(self.repo.open_paper_deals(), [])
        evaluation = self.repo.list_table("live_rule_evaluations", 1)[0]
        self.assertEqual(evaluation["reason"], "BOTH_SIDES_MATCH")

    def test_market_resolution_closes_at_official_payout(self):
        self.create_rule()
        self.manager.process_message(self.book("no-token", 0.25, 0.26, 20))
        self.manager.process_message(self.book("yes-token", 0.73, 0.74, 21))
        self.manager.process_message({
            "event_type": "market_resolved",
            "market": "condition-next",
            "winning_asset_id": "yes-token",
            "winning_outcome": "Up",
            "timestamp": "22",
        })
        deal = self.repo.list_table("live_deals", 1)[0]
        self.assertEqual(deal["status"], "closed")
        self.assertEqual(deal["exit_reason"], "event_resolution")
        self.assertEqual(deal["average_exit_fill_price"], 1)
        market = self.repo.latest_market("condition-next")
        self.assertEqual(market["market_resolved"], 1)
        self.assertEqual(market["yes_token_id"], "yes-token")
        self.assertEqual(market["no_token_id"], "no-token")

    def test_continuous_market_worker_connects_subscribes_and_consumes(self):
        sent = []
        manager = None

        class FakeSocket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def send(self, payload):
                sent.append(payload)

            async def recv(self):
                return json.dumps(PaperTradingTests.book("yes-token", 0.70, 0.71, 30))

            async def close(self):
                return None

        def on_snapshot(_snapshot):
            manager._stop.set()

        manager = MarketWebSocketManager(
            self.repo, stale_after_seconds=10_000, on_snapshot=on_snapshot
        )

        async def scenario():
            await manager.run("wss://fixture", connect=lambda *_args, **_kwargs: FakeSocket())

        asyncio.run(scenario())
        self.assertTrue(sent, manager.health())
        subscription = json.loads(sent[0])
        self.assertEqual(subscription["type"], "market")
        self.assertEqual(set(subscription["assets_ids"]), {"yes-token", "no-token"})
        self.assertTrue(subscription["custom_feature_enabled"])
        self.assertEqual(manager.health()["status"], "STOPPED")
        self.assertEqual(manager.snapshots_received, 1)

    def test_paper_config_rejects_real_trading_mix(self):
        mixed = LiveConfig(
            live_module_enabled=True,
            execution_mode="PAPER_TRADING",
            paper_trading_enabled=True,
            live_trading_enabled=True,
        )
        self.assertTrue(any("cannot be combined" in error for error in mixed.validation_errors()))
        with self.assertRaises(ValueError):
            configure(self.db_path, mixed)

    def test_paper_engine_has_no_trading_write_dependencies(self):
        source = inspect.getsource(PaperTradingEngine)
        for forbidden in (
            "OrderManager", "TradingAdapter", "RealPolymarketTradingAdapter",
            "submit_order", "create_order", "cancel_order",
        ):
            self.assertNotIn(forbidden, source)

    def test_separate_paper_sections_and_apis(self):
        config = LiveConfig(
            live_module_enabled=True,
            execution_mode="PAPER_TRADING",
            paper_trading_enabled=True,
            market_ws_enabled=False,
            live_adapter="mock",
            operator_token="test-token",
            login_username="Admin@system.com",
            login_password_hash="sha256:" + hashlib.sha256(b"pw").hexdigest(),
            session_secret="test-session-secret",
            live_db_path=str(self.db_path),
            backup_dir=str(Path(self.temp.name) / "backups"),
            max_market_data_age_seconds=10_000,
        )
        configure(self.db_path, config)
        client = TestClient(live_app.app, base_url="https://testserver")
        try:
            login = client.post(
                "/live/login", json={"username": "Admin@system.com", "password": "pw"}
            )
            self.assertEqual(login.status_code, 200)
            html = client.get("/live?view=paper-overview").text
            self.assertIn("PAPER TRADING ONLY", html)
            self.assertIn("Paper Rules", html)
            self.assertIn("Paper Deals", html)
            self.assertEqual(client.get("/live/paper/rules").status_code, 200)
            self.assertEqual(client.get("/live/paper/deals").status_code, 200)
            self.assertEqual(client.get("/live/paper/evaluations").status_code, 200)
            self.assertEqual(client.get("/live/paper/health").json()["status"], "RUNNING")
            headers = {
                "X-Live-Operator-Token": "test-token",
                "X-Live-CSRF-Token": login.headers["X-Live-CSRF-Token"],
            }
            created = client.post("/live/paper/rules", headers=headers, json={
                "name": "API Paper Rule",
                "entry_price": 0.74,
                "stop_loss_price": 0.65,
                "take_profit_price": 0.97,
                "requested_amount_usd": 1,
                "status": "inactive",
            })
            self.assertEqual(created.status_code, 200)
            self.assertEqual(created.json()["execution_mode"], "PAPER_TRADING")
            self.assertEqual(created.json()["eligible_after_event_id"], "btc-updown-5m-next")
            activated = client.post(
                f"/live/paper/rules/{created.json()['id']}/status",
                headers={**headers, "X-Live-Reauth-Password": "pw"},
                json={"status": "active"},
            )
            self.assertEqual(activated.status_code, 200)
            self.assertEqual(activated.json()["status"], "active")
        finally:
            client.close()

        conn = sqlite3.connect(self.db_path)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        finally:
            conn.close()
        self.assertIn("live_market_snapshots", tables)
        self.assertIn("live_rule_evaluations", tables)
        self.assertNotIn("events", tables)
        self.assertNotIn("deals", tables)


if __name__ == "__main__":
    unittest.main()

import asyncio
import hashlib
import os
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
from live.config import LiveConfig, redact_mapping
from live.public_client import MockPublicClobClient
from live.repository import LiveRepository
from live.router import configure, refresh_public_market_metadata


class LiveSystemTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = app.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        app.DB_PATH = self.db_path
        app.init_db()
        self.config = LiveConfig(
            live_module_enabled=True,
            live_adapter="mock",
            operator_token="test-token",
            login_password_hash="sha256:" + hashlib.sha256(b"pw").hexdigest(),
            session_secret="test-session-secret",
            live_kill_switch_default=True,
            max_market_data_age_seconds=10_000,
            max_reconciliation_age_seconds=10_000,
        )
        configure(self.db_path, self.config)

    def tearDown(self):
        app.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def client(self):
        return TestClient(app.app)

    def login(self, client):
        response = client.post("/live/login", json={"username": "operator", "password": "pw"})
        self.assertEqual(response.status_code, 200)
        return response

    def test_config_defaults_are_fail_closed(self):
        defaults = LiveConfig()
        self.assertEqual(defaults.trading_mode, "DEMO")
        self.assertFalse(defaults.live_module_enabled)
        self.assertFalse(defaults.live_trading_enabled)
        self.assertFalse(defaults.live_order_submission_enabled)
        self.assertEqual(defaults.live_adapter, "mock")
        self.assertTrue(defaults.live_kill_switch_default)
        self.assertFalse(defaults.real_submission_armed())

        armed = LiveConfig(
            trading_mode="LIVE",
            live_module_enabled=True,
            live_trading_enabled=True,
            live_order_submission_enabled=True,
            live_adapter="polymarket",
        )
        self.assertTrue(armed.real_submission_armed())

        redacted = redact_mapping({"POLYMARKET_API_SECRET": "abcdef", "safe": "value"})
        self.assertEqual(redacted["POLYMARKET_API_SECRET"], "ab****ef")
        self.assertEqual(redacted["safe"], "value")

    def test_live_migration_is_idempotent_and_preserves_demo_tables(self):
        repo = LiveRepository(self.db_path)
        repo.migrate()
        repo.migrate()
        conn = sqlite3.connect(self.db_path)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertIn("events", tables)
        self.assertIn("rules", tables)
        self.assertIn("deals", tables)
        self.assertIn("live_orders", tables)
        self.assertIn("live_order_fills", tables)
        self.assertIn("live_reconciliation_runs", tables)
        self.assertIn("live_system_state", tables)

    def test_live_routes_health_and_auth_blocks_writes_without_token(self):
        client = self.client()
        unauthenticated = client.get("/live/health")
        self.assertEqual(unauthenticated.status_code, 401)
        self.login(client)
        response = client.get("/live/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["kill_switch_active"])
        self.assertEqual(payload["adapter"], "mock")
        self.assertFalse(payload["config"]["real_submission_armed"])

        blocked = client.post("/live/kill-switch/deactivate")
        self.assertEqual(blocked.status_code, 403)

        ok = client.post("/live/kill-switch/deactivate", headers={"X-Live-Operator-Token": "test-token"}, follow_redirects=False)
        self.assertEqual(ok.status_code, 303)
        self.assertFalse(client.get("/live/health").json()["kill_switch_active"])

    def test_mock_order_lifecycle_and_fills(self):
        client = self.client()
        self.login(client)
        headers = {"X-Live-Operator-Token": "test-token"}
        client.post("/live/kill-switch/deactivate", headers=headers, follow_redirects=False)
        result = client.post(
            "/live/orders/mock",
            headers=headers,
            json={
                "idempotency_key": "entry-1",
                "condition_id": "condition-1",
                "token_id": "yes-token",
                "requested_price": 0.50,
                "requested_amount_usd": 1,
            },
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["order"]["status"], "filled")
        self.assertEqual(data["order"]["polymarket_order_id"], "mock-entry-1")
        self.assertEqual(len(client.get("/live/fills").json()), 1)

        duplicate = client.post(
            "/live/orders/mock",
            headers=headers,
            json={
                "idempotency_key": "entry-1",
                "condition_id": "condition-1",
                "token_id": "yes-token",
                "requested_price": 0.50,
                "requested_amount_usd": 1,
            },
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json()["risk"]["reason_code"], "DUPLICATE_IDEMPOTENCY")

    def test_risk_blocks_kill_switch_and_partial_fill_policy(self):
        client = self.client()
        self.login(client)
        headers = {"X-Live-Operator-Token": "test-token"}
        blocked = client.post(
            "/live/orders/mock",
            headers=headers,
            json={
                "idempotency_key": "blocked-1",
                "condition_id": "condition-1",
                "token_id": "yes-token",
                "requested_price": 0.50,
                "requested_amount_usd": 1,
            },
        )
        self.assertEqual(blocked.status_code, 200)
        self.assertEqual(blocked.json()["order"]["status"], "blocked")
        self.assertEqual(blocked.json()["risk"]["reason_code"], "KILL_SWITCH_ACTIVE")

        client.post("/live/kill-switch/deactivate", headers=headers, follow_redirects=False)
        fak = client.post(
            "/live/orders/mock",
            headers=headers,
            json={
                "idempotency_key": "fak-1",
                "condition_id": "condition-1",
                "token_id": "yes-token",
                "order_type": "FAK",
                "requested_price": 0.50,
                "requested_amount_usd": 1,
                "mock_scenario": "partial",
            },
        )
        self.assertEqual(fak.status_code, 200)
        self.assertEqual(fak.json()["order"]["status"], "blocked")
        self.assertEqual(fak.json()["risk"]["reason_code"], "PARTIAL_FILLS_DISABLED")

    def test_public_metadata_and_websocket_resolution_fixture(self):
        metadata = asyncio.run(refresh_public_market_metadata("condition-1", use_mock=True))
        self.assertEqual(metadata["token_mapping_status"], "matched")
        client = self.client()
        self.login(client)
        headers = {"X-Live-Operator-Token": "test-token"}
        message = {
            "event_type": "market_resolved",
            "condition_id": "condition-1",
            "winning_asset_id": "yes-token",
            "winning_outcome": "Yes",
        }
        first = client.post("/live/market-ws/fixture", headers=headers, json=message)
        second = client.post("/live/market-ws/fixture", headers=headers, json=message)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["stored"])
        self.assertFalse(second.json()["stored"])
        market = client.get("/live/markets").json()[0]
        self.assertEqual(market["market_resolved"], 1)
        self.assertEqual(market["winning_asset_id"], "yes-token")

    def test_user_websocket_fixture_reconciliation_and_export(self):
        client = self.client()
        self.login(client)
        headers = {"X-Live-Operator-Token": "test-token"}
        user_msg = {"type": "trade", "condition_id": "condition-1", "trade_id": "trade-1", "status": "MATCHED"}
        self.assertTrue(client.post("/live/user-ws/fixture", headers=headers, json=user_msg).json()["stored"])
        recon = client.post("/live/reconciliation/run", headers=headers, follow_redirects=False)
        self.assertEqual(recon.status_code, 303)
        self.assertGreaterEqual(len(client.get("/live/reconciliation").json()), 1)

        generated = client.post("/live/export/generate", headers=headers, follow_redirects=False)
        self.assertEqual(generated.status_code, 303)
        # BackgroundTasks run before TestClient returns.
        download = client.get("/live/export/download")
        self.assertEqual(download.status_code, 200)
        workbook = load_workbook(BytesIO(download.content), read_only=True)
        try:
            self.assertIn("live_orders", workbook.sheetnames)
            self.assertIn("live_audit_log", workbook.sheetnames)
            self.assertIn("live_dry_runs", workbook.sheetnames)
        finally:
            workbook.close()

    def test_account_identity_secret_readiness_and_dry_run(self):
        client = self.client()
        self.login(client)
        headers = {"X-Live-Operator-Token": "test-token"}
        configure(self.db_path, LiveConfig(
            live_module_enabled=True,
            live_adapter="mock",
            operator_token="test-token",
            login_password_hash="sha256:" + hashlib.sha256(b"pw").hexdigest(),
            session_secret="test-session-secret",
            profile_address="0xcE075637152167517e1492FcF5ff2D131686ee38",
            live_kill_switch_default=True,
            max_market_data_age_seconds=10_000,
            max_reconciliation_age_seconds=10_000,
        ))
        self.login(client)
        identity = client.post("/live/account/public-refresh?use_mock=true", headers=headers)
        self.assertEqual(identity.status_code, 200)
        self.assertEqual(identity.json()["account_identity_status"], "UNVERIFIED")
        self.assertNotIn("raw_public_payload", identity.json())

        dry = client.post("/live/dry-run", headers=headers, json={
            "idempotency_key": "dry-1",
            "condition_id": "condition-1",
            "token_id": "yes-token",
            "requested_price": 0.50,
            "requested_amount_usd": 1,
            "purpose": "entry",
        })
        self.assertEqual(dry.status_code, 200)
        self.assertEqual(dry.json()["final_decision"], "BLOCKED")
        self.assertEqual(client.get("/live/secrets/readiness").status_code, 200)

    def test_demo_routes_still_work_with_live_module_enabled(self):
        client = self.client()
        response = client.post("/rules", json={
            "name": "demo rule",
            "entry_price": "0.77",
            "stop_loss_price": "0.60",
            "take_profit_price": "0.90",
            "max_yes_entries_per_event": 1,
            "max_no_entries_per_event": 1,
            "status": "active",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/rules").status_code, 200)
        self.assertEqual(client.get("/deals").status_code, 200)
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertIn("Polymarket BTC Collector", client.get("/").text)

    def test_live_product_ui_screens_render(self):
        client = self.client()
        self.login(client)
        views = ["overview", "operations", "risk", "logs", "market", "account", "dry-run", "reconciliation", "orders", "deployment"]
        for view in views:
            response = client.get(f"/live?view={view}")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Polymarket LIVE", response.text)
            self.assertIn("Control Center", response.text)
            self.assertIn('name="viewport"', response.text)
        self.assertIn("Dry Run Studio", client.get("/live?view=dry-run").text)
        self.assertIn("Deployment Checklist", client.get("/live?view=deployment").text)
        self.assertIn("attr(data-label)", client.get("/live?view=risk").text)


if __name__ == "__main__":
    unittest.main()

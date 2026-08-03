from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from live.config import LiveConfig
from live.market_websocket import MarketWebSocketManager, UserWebSocketManager
from live.paper_trading import PaperTradingEngine
from live.repository import LiveRepository
from live.retention import LiveRetentionManager


class LiveStoragePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "live.sqlite3"
        self.repo = LiveRepository(
            self.db_path,
            snapshot_min_interval_ms=1000,
            snapshot_save_only_on_change=True,
            snapshot_raw_payload_enabled=False,
        )
        self.repo.migrate()
        self.repo.upsert_market({
            "event_id": "btc-updown-5m-1",
            "condition_id": "condition-1",
            "yes_token_id": "yes-token",
            "no_token_id": "no-token",
            "token_mapping_status": "verified",
            "accepting_orders": True,
            "market_resolved": False,
            "source": "test",
        })

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def book(asset: str, bid: str, ask: str, *, size: str = "10") -> dict:
        return {
            "event_type": "book",
            "asset_id": asset,
            "market": "condition-1",
            "bids": [{"price": bid, "size": size}],
            "asks": [{"price": ask, "size": size}],
            "timestamp": "1",
        }

    def test_every_message_reaches_engine_but_persistence_is_per_token_throttled(self):
        processed: list[dict] = []
        manager = MarketWebSocketManager(
            self.repo, on_snapshot=processed.append, raw_events_enabled=False
        )
        with patch("live.repository.time.monotonic", side_effect=[0.0, 0.1, 0.2, 0.3]):
            manager.process_message(self.book("yes-token", "0.4", "0.5"))
            manager.process_message(self.book("yes-token", "0.4", "0.5"))
            manager.process_message(self.book("yes-token", "0.41", "0.51"))
            manager.process_message(self.book("no-token", "0.49", "0.59"))
        self.assertEqual(len(processed), 4)
        self.assertEqual(manager.messages_received, 4)
        self.assertEqual(manager.snapshots_persisted, 2)
        self.assertEqual(len(self.repo.list_table("live_market_snapshots", 10)), 2)
        self.assertEqual(self.repo.list_table("live_websocket_events", 10), [])
        self.assertTrue(processed[0]["_persisted"])
        self.assertFalse(processed[1]["_persisted"])
        self.assertFalse(processed[2]["_persisted"])
        self.assertTrue(processed[3]["_persisted"])

    def test_change_after_interval_persists_and_raw_snapshot_payload_is_omitted(self):
        first = {"condition_id": "condition-1", "event_id": "e", "asset_id": "yes-token",
                 "event_type": "book", "best_bid": .4, "best_ask": .5,
                 "bids": [{"price": ".4", "size": "10"}],
                 "asks": [{"price": ".5", "size": "10"}]}
        changed = {**first, "best_bid": .41, "bids": [{"price": ".41", "size": "11"}]}
        with patch("live.repository.time.monotonic", side_effect=[0.0, 0.2, 1.2]):
            self.assertIsNotNone(self.repo.store_market_snapshot(first))
            self.assertIsNone(self.repo.store_market_snapshot(changed))
            stored = self.repo.store_market_snapshot(changed)
        self.assertIsNotNone(stored)
        self.assertIsNone(stored["raw_message"])

    def test_concurrent_identical_snapshot_creates_one_row(self):
        snapshot = {"condition_id": "condition-1", "event_id": "e", "asset_id": "yes-token",
                    "event_type": "book", "best_bid": .4, "best_ask": .5,
                    "bids": [{"price": ".4", "size": "10"}],
                    "asks": [{"price": ".5", "size": "10"}]}
        results: list[object] = []
        threads = [threading.Thread(target=lambda: results.append(self.repo.store_market_snapshot(snapshot))) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.repo.list_table("live_market_snapshots", 10)), 1)

    def test_transient_entry_signal_forces_business_snapshot_and_opens_deal(self):
        self.repo.create_rule({
            "name": "paper", "entry_price": .74, "stop_loss_price": .65,
            "take_profit_price": .97, "requested_amount_usd": 1,
            "status": "active", "execution_mode": "PAPER_TRADING",
        })
        engine = PaperTradingEngine(
            self.repo, enabled=True, max_market_age_seconds=10_000,
            taker_fee_rate=Decimal("0.07"),
        )
        manager = MarketWebSocketManager(
            self.repo, on_snapshot=engine.process_snapshot, raw_events_enabled=False
        )
        with patch("live.repository.time.monotonic", side_effect=[0.0, 0.1, 0.2, 0.3]):
            manager.process_message(self.book("no-token", ".25", ".26"))
            manager.process_message(self.book("yes-token", ".72", ".73"))
            manager.process_message(self.book("yes-token", ".73", ".74"))
        deals = self.repo.open_paper_deals()
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0]["entry_reason"], "ENTRY_PRICE_MATCHED")
        self.assertGreaterEqual(manager.snapshots_received, manager.snapshots_persisted)

    def test_heartbeat_and_routine_state_do_not_create_history_spam(self):
        manager = MarketWebSocketManager(self.repo, raw_events_enabled=True)
        self.assertFalse(manager.process_message({"event_type": "heartbeat"}))
        self.repo.set_state("market_ws_status", "CONNECTED", "market_ws")
        self.repo.set_state("market_ws_status", "CONNECTED", "market_ws")
        self.repo.set_state("market_ws_last_message_at", "one", "market_ws")
        self.repo.set_state("market_ws_last_message_at", "two", "market_ws")
        audit = self.repo.list_table("live_audit_log", 10)
        technical = [row for row in audit if row["category"] == "TECHNICAL"]
        self.assertEqual(len(technical), 1)
        self.assertEqual(self.repo.list_table("live_websocket_events", 10), [])


    def test_market_health_timestamp_is_persisted_at_most_once_per_second(self):
        manager = MarketWebSocketManager(self.repo, raw_events_enabled=False)
        with patch("live.market_websocket.monotonic", side_effect=[1.0, 1.5, 2.1]), patch(
            "live.market_websocket.now_iso", side_effect=["one", "two", "three"]
        ):
            manager.process_message({"event_type": "heartbeat"})
            manager.process_message({"event_type": "heartbeat"})
            manager.process_message({"event_type": "heartbeat"})
        self.assertEqual(self.repo.get_state("market_ws_last_message_at"), "three")

    def test_sqlite_lock_while_marking_disconnect_does_not_kill_reconnect_path(self):
        class LockedRepository:
            def set_state(self, *_args, **_kwargs):
                raise sqlite3.OperationalError("database is locked")

        manager = MarketWebSocketManager(LockedRepository())
        manager.mark_disconnect("temporary lock")
        self.assertEqual(manager.status.status, "DISCONNECTED")
        self.assertEqual(manager.status.reconnect_attempts, 1)
        user_manager = UserWebSocketManager(LockedRepository())
        user_manager._persist_state()


class LiveRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "live.sqlite3"
        self.repo = LiveRepository(self.db_path)
        self.repo.migrate()
        self.config = LiveConfig(
            live_db_path=str(self.db_path), ws_event_retention_hours=1,
            snapshot_retention_days=1, technical_audit_retention_days=1,
            retention_batch_size=1,
        )
        self.manager = LiveRetentionManager(self.repo, self.config)

    def tearDown(self):
        self.temp.cleanup()

    def test_retention_is_batched_idempotent_and_conservative(self):
        old = "2020-01-01T00:00:00+00:00"
        with self.repo.connect() as conn:
            for suffix in ("a", "b"):
                conn.execute(
                    "INSERT INTO live_websocket_events(channel,message_hash,received_at,status) VALUES('market',?,?, 'processed')",
                    (suffix, old),
                )
            conn.execute(
                "INSERT INTO live_market_snapshots(condition_id,asset_id,event_type,received_at,source,message_hash) VALUES('c','a','book',?,'test','old-snapshot')",
                (old,),
            )
            conn.commit()
        self.repo.audit("market_ws", "status_change", "ok", category="TECHNICAL")
        self.repo.audit("operator", "admin_action", "ok", category="ADMIN")
        self.repo.audit("legacy", "unknown_action", "ok")
        with self.repo.connect() as conn:
            conn.execute("UPDATE live_audit_log SET occurred_at = ?", (old,))
            conn.commit()
        now = datetime(2026, 8, 3, tzinfo=timezone.utc)
        preview = self.manager.preview(now)
        self.assertEqual(preview["websocket_events"], 2)
        self.assertEqual(preview["market_snapshots"], 1)
        self.assertEqual(preview["technical_audit"], 1)
        result = self.manager.run(now)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.deleted_websocket_events, 2)
        self.assertEqual(result.deleted_market_snapshots, 1)
        self.assertEqual(result.deleted_technical_audit, 1)
        remaining = self.repo.list_table("live_audit_log", 10)
        self.assertEqual({row["category"] for row in remaining}, {"ADMIN", "UNCLASSIFIED"})
        second = self.manager.run(now)
        self.assertEqual(second.deleted_websocket_events, 0)
        self.assertEqual(second.deleted_market_snapshots, 0)
        self.assertEqual(second.deleted_technical_audit, 0)

    def test_status_persistence_failure_does_not_leak_retention_lock(self):
        with patch.object(
            self.repo, "set_state", side_effect=sqlite3.OperationalError("database is locked")
        ):
            first = self.manager.run(datetime(2026, 8, 3, tzinfo=timezone.utc))
            second = self.manager.run(datetime(2026, 8, 3, tzinfo=timezone.utc))
        self.assertEqual(first.status, "ok")
        self.assertEqual(second.status, "ok")

    def test_concurrent_retention_run_is_skipped(self):
        self.manager._lock.acquire()
        try:
            result = self.manager.run(datetime(2026, 8, 3, tzinfo=timezone.utc))
        finally:
            self.manager._lock.release()
        self.assertEqual(result.status, "skipped_already_running")


if __name__ == "__main__":
    unittest.main()

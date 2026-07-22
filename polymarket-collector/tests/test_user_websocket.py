import asyncio, json, logging, os, sqlite3, tempfile, unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from live.market_websocket import UserWebSocketManager
from live.repository import LiveRepository

class UserWebSocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Path(self.tmp.name)/"live.sqlite3"
        self.repo=LiveRepository(self.db); self.repo.migrate()
        self.repo.upsert_market({"condition_id":"condition-current","event_id":"btc-updown-5m-1",
          "yes_token_id":"yes","no_token_id":"no","accepting_orders":True})
        self.manager=UserWebSocketManager(self.repo, stale_after_seconds=1)
        self.old={k:os.environ.get(k) for k in self.manager.AUTH_KEYS}
        for k,v in zip(self.manager.AUTH_KEYS,["key-value","secret-value","pass-value"]): os.environ[k]=v
    def tearDown(self):
        for k,v in self.old.items():
            if v is None: os.environ.pop(k,None)
            else: os.environ[k]=v
        self.tmp.cleanup()
    def test_auth_message_condition_ids_and_no_log(self):
        with self.assertLogs("live.user_ws",level=logging.CRITICAL) if False else patch.object(logging.Logger,"_log") as log:
            msg=self.manager.subscription_message(["condition-current"],self.manager.credentials())
        self.assertEqual(msg["markets"],["condition-current"]); self.assertEqual(msg["type"],"user")
        log.assert_not_called()
    def test_missing_credentials_fail_closed(self):
        os.environ.pop("POLYMARKET_API_SECRET")
        asyncio.run(self.manager.run("wss://unused",connect=AsyncMock()))
        self.assertEqual(self.manager.health()["status"],"AUTH_FAILED")
    def test_dynamic_subscribe_unsubscribe(self):
        self.assertEqual(self.manager.dynamic_subscription_message(["c"],"subscribe"),{"operation":"subscribe","markets":["c"]})
        self.assertEqual(self.manager.dynamic_subscription_message(["c"],"unsubscribe"),{"operation":"unsubscribe","markets":["c"]})
    def test_pong_updates_timestamp(self):
        asyncio.run(self.manager._receive("PONG")); self.assertIsNotNone(self.manager.last_pong_at)
    def test_stale_state_and_auth_failure(self):
        self.manager._set_state("STALE","timeout"); self.assertTrue(self.manager.health()["stale"])
        self.manager._set_state("AUTH_FAILED","authentication failed"); self.assertFalse(self.manager.health()["connected"])
    def test_graceful_shutdown(self):
        asyncio.run(self.manager.stop()); self.assertEqual(self.manager.health()["status"],"STOPPED")
    def test_order_lifecycle_and_partial_fill(self):
        for status in ("PLACEMENT","UPDATE","CANCELLATION"):
            stored=self.manager.process_message({"event_type":"order","status":status,"id":"o-"+status,
              "market":"condition-current","asset_id":"yes","side":"BUY","price":"0.5","size":"10","size_matched":"4"})
            self.assertTrue(stored)
        row=self.repo.list_table("live_websocket_events",1)[0]
        self.assertEqual(row["remaining_size"],6.0); self.assertEqual(row["outcome"],"YES")
    def test_all_trade_statuses(self):
        for status in ("MATCHED","MINED","CONFIRMED","RETRYING","FAILED"):
            self.assertTrue(self.manager.process_message({"event_type":"trade","status":status,"id":"t-"+status,
              "market":"condition-current","asset_id":"no","side":"SELL","price":"0.4","size":"2",
              "transaction_hash":"0xabc"}))
        self.assertEqual(self.manager.trade_events_received,5)
    def test_duplicate_is_idempotent(self):
        msg={"event_type":"trade","status":"MATCHED","id":"duplicate","market":"condition-current"}
        self.assertTrue(self.manager.process_message(msg)); self.assertFalse(self.manager.process_message(msg))
        self.assertEqual(len(self.repo.list_table("live_websocket_events",10)),1)
    def test_sanitized_raw_payload_and_exception(self):
        msg={"event_type":"order","status":"PLACEMENT","id":"safe","auth":{"apiKey":"leak"},
             "api_secret":"leak2","market":"condition-current"}
        self.manager.process_message(msg)
        raw=self.repo.list_table("live_websocket_events",1)[0]["raw_message"]
        self.assertNotIn("leak",raw); self.assertIn("[REDACTED]",raw)
        self.assertNotIn("secret-value",self.manager._safe_error(Exception("secret-value")))
    def test_live_demo_separation(self):
        demo=Path(self.tmp.name)/"demo.sqlite3"; sqlite3.connect(demo).execute("create table events(id integer)").connection.close()
        self.manager.process_message({"event_type":"trade","status":"MATCHED","id":"separate"})
        c=sqlite3.connect(demo); tables={r[0] for r in c.execute("select name from sqlite_master where type='table'")}; c.close()
        self.assertNotIn("live_websocket_events",tables)
    def test_no_order_write_methods(self):
        import inspect
        source=inspect.getsource(UserWebSocketManager)
        for forbidden in (".create_order(", ".cancel_order(", ".cancel_orders(", ".cancel_all_orders("):
            self.assertNotIn(forbidden,source)
    def test_out_of_order_preserves_history(self):
        newer={"event_type":"order","status":"UPDATE","id":"same","timestamp":"200","market":"condition-current"}
        older={"event_type":"order","status":"PLACEMENT","id":"same","timestamp":"100","market":"condition-current"}
        self.assertTrue(self.manager.process_message(newer)); self.assertTrue(self.manager.process_message(older))
        self.assertEqual(len(self.repo.list_table("live_websocket_events",10)),2)

if __name__=="__main__": unittest.main()

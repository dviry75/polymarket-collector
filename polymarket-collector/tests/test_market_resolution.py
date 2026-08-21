import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from live.market_resolution import MarketResolutionReconciler
from live.repository import LiveRepository


class FakeClient:
    def __init__(self, events): self.events, self.closed = events, False
    async def get_event(self, *, slug):
        value = self.events[slug]
        if isinstance(value, Exception): raise value
        return value
    async def close(self): self.closed = True


class MarketResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "live.sqlite3"
        self.repo = LiveRepository(self.db_path); self.repo.migrate()
    def tearDown(self): self.temp_dir.cleanup()
    def add_market(self):
        self.repo.upsert_market({"event_id": "btc-updown-5m-1700000000",
            "condition_id": "condition", "yes_token_id": "yes-token",
            "no_token_id": "no-token", "accepting_orders": True,
            "market_resolved": False})
    def read_market(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute("SELECT * FROM live_markets WHERE condition_id='condition'").fetchone())
    def reconciler(self, client):
        return MarketResolutionReconciler(self.repo, client_factory=lambda: client,
            grace_seconds=0, clock=lambda: 1_700_000_400)
    def test_resolves_closed_market_and_closes_orders(self):
        self.add_market()
        client = FakeClient({"btc-updown-5m-1700000000": {"markets": [{
            "condition_id": "condition", "state": {"closed": True},
            "outcomes": {"yes": {"label": "Up", "price": "0", "token_id": "yes-token"},
                         "no": {"label": "Down", "price": "1", "token_id": "no-token"}}}]}})
        result = asyncio.run(self.reconciler(client).run_once())
        self.assertEqual(result, {"checked": 1, "resolved": 1, "pending": 0, "errors": 0})
        market = self.read_market()
        self.assertEqual((market["market_resolved"], market["accepting_orders"]), (1, 0))
        self.assertEqual((market["winning_asset_id"], market["winning_outcome"]), ("no-token", "Down"))
        self.assertEqual(market["source"], "POLYMARKET_PUBLIC_REST")
        self.assertTrue(client.closed)
    def test_incomplete_result_remains_pending_for_retry(self):
        self.add_market()
        client = FakeClient({"btc-updown-5m-1700000000": {"markets": [{
            "condition_id": "condition", "state": {"closed": False}, "outcomes": {}}]}})
        result = asyncio.run(self.reconciler(client).run_once())
        self.assertEqual(result["pending"], 1)
        self.assertEqual(self.read_market()["market_resolved"], 0)
    def test_api_error_is_isolated_and_retried_later(self):
        self.add_market()
        client = FakeClient({"btc-updown-5m-1700000000": RuntimeError("temporary")})
        result = asyncio.run(self.reconciler(client).run_once())
        self.assertEqual(result["errors"], 1)
        self.assertEqual(self.read_market()["market_resolved"], 0)


if __name__ == "__main__": unittest.main()

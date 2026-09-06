import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.entry_liquidity import EntryLiquidityCollector
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository


def _book(bids, *, gen=1, msg="hash-1", best_ask="0.74", inline=True):
    top = bids[0]["price"] if bids else None
    update = {
        "asset_id": "tok-1",
        "event_type": "book",
        "best_bid": top,
        "best_bid_size": bids[0]["size"] if bids else None,
        "best_ask": best_ask,
        "generation": gen,
        "update_number": gen,
        "message_hash": msg,
        "exchange_timestamp_ms": 1_000_000 + gen,
        "exchange_age_ms": 20,
        "receive_latency_ms": 5,
    }
    if inline:
        update["bids"] = bids
        update["asks"] = [{"price": best_ask, "size": "5"}]
    return update


class EntryLiquidityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = LiveRepository(Path(self.temp.name) / "live.sqlite3")
        base.migrate()
        self.repo = StrategyRepository(base)
        self.repo.migrate()
        self.collector = EntryLiquidityCollector(
            self.repo,
            bucket_levels=[Decimal("0.66"), Decimal("0.60"),
                           Decimal("0.55"), Decimal("0.46")],
            buffer_multiples=[Decimal("3"), Decimal("5")],
            deep_capture_max_vwap="0.60",
        )

    def _signal(self, update, *, shares="5"):
        self.collector.note_signal(
            "intent-1", event_id="btc-updown-5m-1", condition_id="cond-1",
            token_id="tok-1", side="YES", requested_shares=shares,
            update=update,
        )

    def test_full_flow_healthy_book_no_deep_capture(self):
        deep_bids = [{"price": "0.73", "size": "50"}]
        self._signal(_book(deep_bids))
        self.collector.note_revalidation("intent-1", update=_book(deep_bids, gen=2))
        self.collector.note_submit("intent-1", update=_book(deep_bids, gen=3),
                                   requested_shares="5")
        self.collector.note_submit_result("intent-1", clob_status="matched")
        self.collector.note_fill("intent-1", filled_shares="5", fill_price="0.74")
        self.collector.drain_pending()

        row = self.repo.entry_audit("intent-1")
        self.assertIsNotNone(row)
        self.assertEqual(row["entry_liquidity_phase_primary"], "SUBMIT")
        self.assertEqual(row["full_ladder_captured_synchronously"], 1)
        self.assertEqual(row["depth_at_066_text"], "50")
        self.assertEqual(row["depth_at_046_text"], "50")
        self.assertEqual(row["sim_exit_shares_text"], "5")
        self.assertEqual(row["sim_exit_vwap_text"], "0.73")
        self.assertEqual(row["sim_exit_worst_price_text"], "0.73")
        self.assertEqual(row["sim_exit_method"], "ORDER_BOOK_VWAP")
        self.assertEqual(row["entry_liquidity_deep_capture"], 0)
        self.assertEqual(row["entry_liquidity_evidence_quality"], "OK")
        buffers = json.loads(row["sim_exit_buffers_json"])
        self.assertEqual(set(buffers), {"3", "5"})
        self.assertEqual(buffers["3"]["vwap"], "0.73")
        self.assertEqual(self.repo.entry_book_snapshots("intent-1"), [])

    def test_thin_book_triggers_deep_capture_and_snapshots(self):
        thin = [
            {"price": "0.67", "size": "0.5"},
            {"price": "0.55", "size": "1"},
            {"price": "0.47", "size": "3"},
        ]
        self._signal(_book(thin))
        self.collector.note_submit("intent-1", update=_book(thin, gen=3))
        self.collector.note_fill("intent-1", filled_shares="5")
        self.collector.drain_pending()

        row = self.repo.entry_audit("intent-1")
        self.assertEqual(row["entry_liquidity_deep_capture"], 1)
        # walk 5 shares: 0.5@0.67 + 1@0.55 + 3@0.47 -> worst 0.47, partial
        self.assertEqual(row["sim_exit_worst_price_text"], "0.47")
        self.assertEqual(row["sim_exit_method"], "ORDER_BOOK_VWAP_PARTIAL")
        self.assertEqual(row["depth_at_066_text"], "0.5")
        self.assertEqual(row["depth_at_055_text"], "1.5")
        self.assertEqual(row["depth_at_046_text"], "4.5")
        snaps = {s["phase"]: s for s in self.repo.entry_book_snapshots("intent-1")}
        self.assertEqual(set(snaps), {"SIGNAL", "SUBMIT"})
        self.assertIn("0.47", snaps["SUBMIT"]["bids_json"])
        self.assertIsNotNone(snaps["SUBMIT"]["cumulative_depth_json"])

    def test_callable_book_marks_not_synchronous(self):
        deep_bids = [{"price": "0.73", "size": "50"}]
        self.collector.set_book_provider(lambda tok: _book(deep_bids, gen=9))
        self._signal(_book([], inline=False))  # top-only, no ladder
        self.collector.note_fill("intent-1", filled_shares="5")
        self.collector.drain_pending()

        row = self.repo.entry_audit("intent-1")
        self.assertEqual(row["entry_liquidity_phase_primary"], "SIGNAL")
        self.assertEqual(row["full_ladder_captured_synchronously"], 0)
        self.assertEqual(row["depth_at_066_text"], "50")

    def test_abort_after_signal_finalizes_without_fill(self):
        self._signal(_book([{"price": "0.62", "size": "1"}]))
        self.collector.note_abort("intent-1", reason="ENTRY_SIGNAL_EXPIRED")
        self.collector.drain_pending()

        row = self.repo.entry_audit("intent-1")
        self.assertIsNotNone(row)
        self.assertEqual(row["entry_liquidity_phase_primary"], "SIGNAL")
        self.assertIsNone(row["fill_price_text"])
        self.assertEqual(row["sim_exit_shares_text"], "5")  # requested size
        self.assertNotIn("intent-1", self.collector._episodes)

    def test_config_bucket_levels_and_multiples_are_used(self):
        collector = EntryLiquidityCollector(
            self.repo,
            bucket_levels=[Decimal("0.70"), Decimal("0.50")],
            buffer_multiples=[Decimal("2")],
        )
        collector.note_signal(
            "intent-2", event_id="e", condition_id="c", token_id="tok-1",
            side="YES", requested_shares="5",
            update=_book([{"price": "0.72", "size": "40"}]),
        )
        collector.note_fill("intent-2", filled_shares="5")
        collector.drain_pending()
        row = self.repo.entry_audit("intent-2")
        self.assertEqual(row["liquidity_bucket_levels_text"], "0.70,0.50")
        self.assertEqual(row["liquidity_buffer_multiples_text"], "2")
        self.assertEqual(row["depth_at_066_text"], "40")  # level[0] == 0.70
        self.assertEqual(set(json.loads(row["sim_exit_buffers_json"])), {"2"})

    def test_queue_overflow_marks_degraded(self):
        self._signal(_book([{"price": "0.66", "size": "1"}]))
        for _ in range(700):
            self.collector.note_submit_result("intent-1", clob_status="matched")
        self.assertGreater(self.collector.dropped, 0)
        self.assertEqual(
            self.collector._episodes["intent-1"].degraded,
            "DEGRADED_EVIDENCE_DROPPED",
        )
        self.collector.drain_pending()
        row = self.repo.entry_audit("intent-1")
        self.assertEqual(
            row["entry_liquidity_evidence_quality"], "DEGRADED_EVIDENCE_DROPPED"
        )

    def test_drain_swallows_repo_errors(self):
        self._signal(_book([{"price": "0.66", "size": "1"}]))

        def boom(*a, **k):
            raise RuntimeError("db down")

        self.repo.record_entry_audit = boom
        self.collector.drain_pending()  # must not raise
        self.assertIn(
            "ENTRY_LIQUIDITY:RuntimeError:db down", self.collector.last_error
        )


if __name__ == "__main__":
    unittest.main()

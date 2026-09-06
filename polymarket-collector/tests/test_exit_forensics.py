import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.exit_forensics import ExitEvidenceCollector
from live.repository import LiveRepository, now_iso
from live.strategy_repository import StrategyRepository

STOP = Decimal("0.66")


def _book(bids, *, gen=1, msg="hash-1", best_bid=None, source="book"):
    top = bids[0]["price"] if bids else best_bid
    return {
        "asset_id": "tok-1",
        "event_type": source,
        "best_bid": best_bid if best_bid is not None else top,
        "best_bid_size": bids[0]["size"] if bids else None,
        "best_ask": "0.70",
        "generation": gen,
        "update_number": gen,
        "message_hash": msg,
        "exchange_timestamp_ms": 1_000_000 + gen,
        "exchange_age_ms": 12,
        "receive_latency_ms": 4,
        "bids": bids,
        "asks": [{"price": "0.70", "size": "5"}],
    }


class ExitForensicsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = LiveRepository(Path(self.temp.name) / "live.sqlite3")
        base.migrate()
        self.repo = StrategyRepository(base)
        self.repo.migrate()
        self.collector = ExitEvidenceCollector(
            self.repo,
            deep_capture_max_vwap="0.55",
            acceptable_min_vwap="0.60",
        )
        self.position = {
            "position_id": "pos-1",
            "event_id": "btc-updown-5m-1",
            "condition_id": "cond-1",
            "token_id": "tok-1",
        }

    def _add_fill(self, intent_id, shares, price, *, fee="0", tid=None, matched_at=None):
        with self.repo.base.connect() as conn:
            ts = now_iso()
            conn.execute(
                "INSERT INTO live_strategy_fills(fill_id,intent_id,remote_trade_id,"
                "shares_text,price_text,fee_text,status,matched_at,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"fill-{intent_id}-{price}", intent_id, tid or f"t-{price}",
                 str(shares), str(price), str(fee), "MATCHED",
                 matched_at or ts, ts, ts),
            )
            conn.commit()

    # ------------------------------------------------------------------ #
    def test_cross_without_latch_writes_nothing(self):
        self.collector.note_cross(
            self.position, bid=Decimal("0.65"), bid_size="3",
            update=_book([{"price": "0.65", "size": "3"}]),
            received_at="2026-09-06T12:00:00.000+00:00", stop_price=STOP,
        )
        self.collector.drain_pending()
        self.assertIsNone(self.repo.exit_audit("pos-1"))
        # position recovers and leaves the active set
        self.collector.reconcile_active(set())
        self.collector.drain_pending()
        self.assertIsNone(self.repo.exit_audit("pos-1"))
        self.assertNotIn("pos-1", self.collector._episodes)

    def test_latch_flushes_cross_and_latch_fields(self):
        self.collector.note_cross(
            self.position, bid=Decimal("0.66"), bid_size="1",
            update=_book([{"price": "0.66", "size": "1"}], msg="cross-hash"),
            received_at="2026-09-06T12:00:00.000+00:00", stop_price=STOP,
        )
        self.collector.note_latch(
            self.position, bid=Decimal("0.62"),
            update=_book([{"price": "0.62", "size": "1"}], gen=2, msg="latch-hash"),
            source="supervisor", liquidity_hash="lh-1",
        )
        self.collector.drain_pending()
        row = self.repo.exit_audit("pos-1")
        self.assertIsNotNone(row)
        self.assertEqual(row["cross_best_bid_text"], "0.66")
        self.assertEqual(row["cross_book_hash"], "cross-hash")
        self.assertEqual(row["latch_best_bid_text"], "0.62")
        self.assertEqual(row["latch_source"], "supervisor")
        self.assertEqual(row["exit_outcome"], "PENDING")
        self.assertEqual(self.repo.exit_book_snapshots(row["exit_audit_id"]), [])

    def test_full_bad_exit_captures_depth_and_attribution(self):
        self.collector.note_cross(
            self.position, bid=Decimal("0.66"), bid_size="1",
            update=_book([{"price": "0.66", "size": "1"}]),
            received_at="2026-09-06T12:00:00.000+00:00", stop_price=STOP,
        )
        self.collector.note_latch(
            self.position, bid=Decimal("0.60"),
            update=_book([{"price": "0.60", "size": "1"}], gen=2),
            source="supervisor",
            latched_at="2026-09-06T12:00:00.030+00:00",
        )
        self.collector.note_tp_cancel(
            self.position, intent_id="tp-1", event="started",
        )
        self.collector.note_tp_cancel(
            self.position, intent_id="tp-1", event="confirmed", result="ACK",
        )
        self.collector.note_submit(
            self.position, exit_intent_id="exit-1", purpose="STOP_066",
            min_price="0.01", requested_shares="5", frame_hash="fh-1",
            update=_book(
                [{"price": "0.50", "size": "1"}, {"price": "0.30", "size": "20"}],
                gen=3,
            ),
            submitted_at="2026-09-06T12:00:07.500+00:00",
            stop_to_submit_seconds=7.4, frame_to_submit_seconds=7.5,
            attempt_count=1,
        )
        self.collector.note_submit_result(
            self.position, clob_status="MATCHED", remote_order_id="ord-1",
        )
        self._add_fill("exit-1", "1", "0.50",
                       matched_at="2026-09-06T12:00:07.600+00:00")
        self._add_fill("exit-1", "4", "0.30",
                       matched_at="2026-09-06T12:00:07.900+00:00")
        self.collector.reconcile_active(set())
        self.collector.drain_pending()

        row = self.repo.exit_audit("pos-1")
        self.assertEqual(row["exit_purpose"], "STOP_066")
        self.assertEqual(row["tp_cancel_result"], "ACK")
        self.assertEqual(row["clob_status"], "MATCHED")
        self.assertEqual(row["exit_outcome"], "BAD_EXIT")
        self.assertEqual(row["deep_capture"], 1)
        self.assertEqual(row["evidence_quality"], "OK")
        # VWAP = (1*0.5 + 4*0.3)/5 = 0.34
        self.assertEqual(Decimal(row["actual_fill_vwap_text"]), Decimal("0.34"))
        # loss_detection = 0.66 - 0.60 = 0.06 ; loss_execution = 0.60 - 0.50 = 0.10
        self.assertEqual(Decimal(row["loss_detection_text"]), Decimal("0.06"))
        self.assertEqual(Decimal(row["loss_execution_text"]), Decimal("0.10"))
        self.assertEqual(row["detection_latency_ms"], 30)
        self.assertEqual(row["execution_latency_ms"], 7470)
        self.assertEqual(row["first_fill_at"], "2026-09-06T12:00:07.600+00:00")
        self.assertEqual(row["last_fill_at"], "2026-09-06T12:00:07.900+00:00")

        snaps = {s["phase"]: s for s in self.repo.exit_book_snapshots(row["exit_audit_id"])}
        self.assertEqual(set(snaps), {"CROSS", "LATCH", "SUBMIT"})
        self.assertIn("0.50", snaps["SUBMIT"]["bids_json"])
        self.assertEqual(snaps["SUBMIT"]["level_count"], 2)
        # expected VWAP walking the submit book for 5 shares:
        # (1*0.50 + 4*0.30) / 5 = 1.70/5 = 0.34
        self.assertEqual(
            Decimal(row["expected_vwap_at_submit_text"]), Decimal("0.34")
        )
        self.assertEqual(row["expected_vwap_method"], "ORDER_BOOK_VWAP")
        # book-implied and realized match -> loss_fill == 0
        self.assertEqual(Decimal(row["loss_fill_text"]), Decimal("0.00"))
        # entry-liquidity parity columns on the submit book (0.50 + 0.30 levels)
        self.assertEqual(row["full_ladder_captured_synchronously"], 1)
        self.assertEqual(row["submit_best_ask_text"], "0.70")
        self.assertEqual(row["submit_depth_at_055_text"], "0")
        # only the 0.50 level clears the 0.46 floor; the 0.30 level does not
        self.assertEqual(row["submit_depth_at_046_text"], "1")

    def test_acceptable_exit_keeps_only_light_row(self):
        self.collector.note_cross(
            self.position, bid=Decimal("0.66"), bid_size="10",
            update=_book([{"price": "0.66", "size": "10"}]),
            received_at="2026-09-06T12:00:00.000+00:00", stop_price=STOP,
        )
        self.collector.note_latch(
            self.position, bid=Decimal("0.65"),
            update=_book([{"price": "0.65", "size": "10"}], gen=2),
            source="supervisor",
        )
        self.collector.note_submit(
            self.position, exit_intent_id="exit-2", purpose="STOP_066",
            min_price="0.01", requested_shares="5", frame_hash="fh",
            update=_book([{"price": "0.64", "size": "30"}], gen=3),
            submitted_at="2026-09-06T12:00:00.300+00:00",
            stop_to_submit_seconds=0.2, frame_to_submit_seconds=0.3,
        )
        self._add_fill("exit-2", "5", "0.63")
        self.collector.reconcile_active(set())
        self.collector.drain_pending()

        row = self.repo.exit_audit("pos-1")
        self.assertEqual(row["exit_outcome"], "ACCEPTABLE")
        self.assertEqual(row["deep_capture"], 0)
        self.assertEqual(row["evidence_quality"], "OK")
        self.assertEqual(self.repo.exit_book_snapshots(row["exit_audit_id"]), [])

    def test_zero_fill_is_no_execution_and_deep(self):
        self.collector.note_cross(
            self.position, bid=Decimal("0.66"), bid_size="1",
            update=_book([{"price": "0.66", "size": "1"}]),
            received_at="2026-09-06T12:00:00.000+00:00", stop_price=STOP,
        )
        self.collector.note_latch(
            self.position, bid=Decimal("0.20"),
            update=_book([{"price": "0.20", "size": "1"}], gen=2),
            source="supervisor",
        )
        self.collector.note_submit(
            self.position, exit_intent_id="exit-3", purpose="STOP_066",
            min_price="0.01", requested_shares="5", frame_hash="fh",
            update=_book([{"price": "0.20", "size": "1"}], gen=3),
            submitted_at="2026-09-06T12:00:01.000+00:00",
            stop_to_submit_seconds=1.0, frame_to_submit_seconds=1.0,
        )
        self.collector.note_submit_result(
            self.position, clob_status="FAK_NOT_FILLED", remote_order_id="ord-3",
        )
        self.collector.reconcile_active(set())
        self.collector.drain_pending()

        row = self.repo.exit_audit("pos-1")
        self.assertEqual(row["exit_outcome"], "NO_EXECUTION")
        self.assertEqual(row["deep_capture"], 1)
        self.assertIsNone(row["actual_fill_vwap_text"])

    def test_queue_overflow_marks_degraded(self):
        self.collector.note_cross(
            self.position, bid=Decimal("0.66"), bid_size="1",
            update=_book([{"price": "0.66", "size": "1"}]),
            received_at="2026-09-06T12:00:00.000+00:00", stop_price=STOP,
        )
        for _ in range(600):
            self.collector.note_tp_cancel(
                self.position, intent_id="tp", event="started",
            )
        self.assertGreater(self.collector.dropped, 0)
        self.assertEqual(
            self.collector._episodes["pos-1"].degraded,
            "DEGRADED_EVIDENCE_DROPPED",
        )

    def test_drain_swallows_repo_errors(self):
        self.collector.note_cross(
            self.position, bid=Decimal("0.66"), bid_size="1",
            update=_book([{"price": "0.66", "size": "1"}]),
            received_at="2026-09-06T12:00:00.000+00:00", stop_price=STOP,
        )
        self.collector.note_latch(
            self.position, bid=Decimal("0.60"),
            update=_book([{"price": "0.60", "size": "1"}], gen=2),
            source="supervisor",
        )

        def boom(*a, **k):
            raise RuntimeError("db down")

        self.repo.record_exit_audit = boom
        self.collector.drain_pending()  # must not raise
        self.assertIn("EXIT_EVIDENCE:RuntimeError:db down", self.collector.last_error)


if __name__ == "__main__":
    unittest.main()

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository, stable_id


class ExitAuditRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = LiveRepository(Path(self.temp.name) / "live.sqlite3")
        base.migrate()
        self.repo = StrategyRepository(base)
        self.repo.migrate()

    def test_migrate_creates_exit_forensic_tables(self):
        with self.repo.base.connect() as conn:
            names = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'live_strategy_exit_%'"
                )
            }
        self.assertEqual(
            names,
            {"live_strategy_exit_audit", "live_strategy_exit_book_snapshots"},
        )

    def test_migrate_is_idempotent(self):
        self.repo.migrate()  # second run must not raise

    def test_record_exit_audit_upserts_incrementally(self):
        audit_id = stable_id("exit-audit", "pos-1")
        self.repo.record_exit_audit(
            audit_id,
            position_id="pos-1",
            event_id="evt-1",
            condition_id="cond-1",
            token_id="tok-1",
            exit_purpose="STOP_066",
            cross_detected_at="2026-09-06T12:00:00.000+00:00",
            cross_best_bid_text="0.66",
            not_a_column="ignored",
        )
        self.repo.record_exit_audit(
            audit_id,
            position_id="pos-1",
            event_id="evt-1",
            latched_at="2026-09-06T12:00:00.030+00:00",
            latch_best_bid_text="0.62",
        )
        self.repo.record_exit_audit(
            audit_id,
            position_id="pos-1",
            event_id="evt-1",
            actual_fill_vwap_text="0.41",
            exit_outcome="BAD_EXIT",
            evidence_quality="OK",
            deep_capture=1,
        )

        row = self.repo.exit_audit("pos-1")
        self.assertIsNotNone(row)
        self.assertEqual(row["exit_audit_id"], audit_id)
        self.assertEqual(row["episode_seq"], 1)
        self.assertEqual(row["condition_id"], "cond-1")
        self.assertEqual(row["exit_purpose"], "STOP_066")
        self.assertEqual(row["cross_best_bid_text"], "0.66")
        self.assertEqual(row["latch_best_bid_text"], "0.62")
        self.assertEqual(row["actual_fill_vwap_text"], "0.41")
        self.assertEqual(row["exit_outcome"], "BAD_EXIT")
        self.assertEqual(row["deep_capture"], 1)
        self.assertNotIn("not_a_column", row)

    def test_exit_book_snapshot_is_idempotent_per_phase(self):
        audit_id = stable_id("exit-audit", "pos-2")
        self.repo.record_exit_audit(
            audit_id, position_id="pos-2", event_id="evt-2"
        )
        self.repo.record_exit_book_snapshot(
            stable_id("exit-book", "pos-2:SUBMIT"),
            exit_audit_id=audit_id,
            position_id="pos-2",
            phase="SUBMIT",
            captured_at="2026-09-06T12:00:00.100+00:00",
            bids_json='[{"price": "0.64", "size": "30"}]',
            level_count=1,
        )
        # a second write for the same (exit_audit_id, phase) is ignored
        self.repo.record_exit_book_snapshot(
            stable_id("exit-book", "pos-2:SUBMIT:dup"),
            exit_audit_id=audit_id,
            position_id="pos-2",
            phase="SUBMIT",
            captured_at="2026-09-06T12:00:00.200+00:00",
            bids_json="[]",
        )
        snaps = self.repo.exit_book_snapshots(audit_id)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["phase"], "SUBMIT")
        self.assertEqual(snaps[0]["bids_json"], '[{"price": "0.64", "size": "30"}]')

    def test_second_episode_on_same_position(self):
        self.repo.record_exit_audit(
            stable_id("exit-audit", "pos-3:1"),
            position_id="pos-3",
            event_id="evt-3",
            episode_seq=1,
            exit_purpose="STOP_066",
        )
        self.repo.record_exit_audit(
            stable_id("exit-audit", "pos-3:2"),
            position_id="pos-3",
            event_id="evt-3",
            episode_seq=2,
            exit_purpose="EMERGENCY_060",
        )
        self.assertEqual(
            self.repo.exit_audit("pos-3", episode_seq=1)["exit_purpose"],
            "STOP_066",
        )
        self.assertEqual(
            self.repo.exit_audit("pos-3", episode_seq=2)["exit_purpose"],
            "EMERGENCY_060",
        )


if __name__ == "__main__":
    unittest.main()

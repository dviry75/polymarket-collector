import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.exit_classifier import classify_exit


def _ladder(levels):
    return json.dumps([{"price": p, "size": s} for p, s in levels])


BASE = {
    "requested_shares_text": "5",
    "cross_best_bid_text": "0.66",
    "latch_best_bid_text": "0.65",
    "submit_best_bid_text": "0.64",
    "actual_fill_vwap_text": "0.63",
    "expected_vwap_at_submit_text": "0.63",
    "detection_latency_ms": 40,
    "execution_latency_ms": 120,
    "loss_detection_text": "0.01",
    "loss_execution_text": "0.01",
}


class ExitClassifierTests(unittest.TestCase):
    def test_healthy(self):
        out = classify_exit(dict(BASE))
        self.assertEqual(out["technical_behavior"], "HEALTHY")
        self.assertEqual(out["exit_outcome"], "ACCEPTABLE")

    def test_execution_delay_dominates(self):
        row = dict(
            BASE,
            latch_best_bid_text="0.65",
            submit_best_bid_text="0.45",
            actual_fill_vwap_text="0.44",
            expected_vwap_at_submit_text="0.44",
            execution_latency_ms=7200,
            loss_execution_text="0.20",
        )
        out = classify_exit(row)
        self.assertEqual(out["root_cause"], "EXECUTION_DELAY")
        self.assertEqual(out["technical_behavior"], "DELAY")
        self.assertEqual(out["exit_outcome"], "BAD_EXIT")

    def test_execution_gap_is_market_not_delay(self):
        row = dict(
            BASE,
            submit_best_bid_text="0.45",
            actual_fill_vwap_text="0.44",
            expected_vwap_at_submit_text="0.44",
            execution_latency_ms=90,        # fast, but price gapped
            loss_execution_text="0.20",
        )
        out = classify_exit(row)
        self.assertEqual(out["root_cause"], "MARKET")
        self.assertEqual(out["technical_behavior"], "MARKET")

    def test_detection_delay(self):
        row = dict(
            BASE,
            cross_best_bid_text="0.66",
            latch_best_bid_text="0.40",
            submit_best_bid_text="0.39",
            actual_fill_vwap_text="0.39",
            expected_vwap_at_submit_text="0.39",
            detection_latency_ms=3300,
            loss_detection_text="0.26",
        )
        out = classify_exit(row)
        self.assertEqual(out["root_cause"], "DETECTION_DELAY")

    def test_book_fill_mismatch(self):
        row = dict(
            BASE,
            submit_best_bid_text="0.64",
            expected_vwap_at_submit_text="0.63",
            actual_fill_vwap_text="0.40",
            loss_execution_text="0.00",
            loss_detection_text="0.01",
        )
        snaps = [{
            "phase": "SUBMIT", "exchange_age_ms": 200,
            "bids_json": _ladder([("0.64", "30"), ("0.63", "40")]),
        }]
        out = classify_exit(row, snaps)
        self.assertEqual(out["root_cause"], "BOOK_FILL_MISMATCH")
        self.assertEqual(out["technical_behavior"], "MISMATCH")

    def test_liquidity_when_thin_book(self):
        row = dict(
            BASE,
            submit_best_bid_text="0.64",
            expected_vwap_at_submit_text="0.50",
            actual_fill_vwap_text="0.38",
            loss_detection_text="0.01",
            loss_execution_text="0.00",
        )
        snaps = [{
            "phase": "SUBMIT", "exchange_age_ms": 200,
            "bids_json": _ladder([("0.64", "1"), ("0.30", "2")]),
        }]
        out = classify_exit(row, snaps)
        self.assertEqual(out["root_cause"], "LIQUIDITY")

    def test_mixed(self):
        row = dict(
            BASE,
            latch_best_bid_text="0.55",
            submit_best_bid_text="0.42",
            actual_fill_vwap_text="0.41",
            expected_vwap_at_submit_text="0.41",
            detection_latency_ms=3000,
            execution_latency_ms=4000,
            loss_detection_text="0.11",
            loss_execution_text="0.13",
        )
        out = classify_exit(row)
        self.assertEqual(out["technical_behavior"], "MIXED")
        self.assertIn(out["primary_cause"], {"EXECUTION_DELAY", "DETECTION_DELAY"})
        self.assertIn("secondary_cause", out)

    def test_no_execution_reject(self):
        row = {
            "requested_shares_text": "5",
            "clob_status": "PRICE_PROTECTION_REJECT",
            "actual_fill_vwap_text": None,
        }
        out = classify_exit(row)
        self.assertEqual(out["exit_outcome"], "NO_EXECUTION")
        self.assertEqual(out["root_cause"], "EXECUTION_DELAY")

    def test_no_execution_liquidity(self):
        row = {
            "requested_shares_text": "5",
            "clob_status": "FAK_NOT_FILLED",
            "submit_best_bid_text": "0.20",
            "actual_fill_vwap_text": None,
        }
        snaps = [{
            "phase": "SUBMIT", "exchange_age_ms": 100,
            "bids_json": _ladder([("0.20", "1")]),
        }]
        out = classify_exit(row, snaps)
        self.assertEqual(out["root_cause"], "LIQUIDITY")

    def test_unknown_when_no_prices(self):
        row = {"requested_shares_text": "5", "actual_fill_vwap_text": "0.40"}
        out = classify_exit(row)
        self.assertEqual(out["root_cause"], "UNKNOWN")
        self.assertEqual(out["exit_outcome"], "BAD_EXIT")


if __name__ == "__main__":
    unittest.main()

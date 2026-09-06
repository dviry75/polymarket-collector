import sys
import unittest
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.book_walk import book_walk_vwap, normalize_bid_levels


class NormalizeBidLevelsTests(unittest.TestCase):
    def test_sorts_descending_and_drops_bad_levels(self):
        levels = normalize_bid_levels(
            [
                {"price": "0.61", "size": "3"},
                {"price": "0.66", "size": "1"},
                {"price": "0.63", "size": "0"},        # non-positive size
                {"price": "1.5", "size": "2"},          # out of range
                {"price": "bad", "size": "2"},          # unparseable
                "not-a-dict",
            ]
        )
        self.assertEqual(
            levels, [(Decimal("0.66"), Decimal("1")), (Decimal("0.61"), Decimal("3"))]
        )

    def test_accepts_json_string(self):
        self.assertEqual(
            normalize_bid_levels('[{"price": "0.5", "size": "2"}]'),
            [(Decimal("0.5"), Decimal("2"))],
        )

    def test_bad_json_is_empty(self):
        self.assertEqual(normalize_bid_levels("{not json"), [])
        self.assertEqual(normalize_bid_levels(None), [])


class BookWalkVwapTests(unittest.TestCase):
    def test_full_fill_returns_vwap(self):
        vwap, method, fillable = book_walk_vwap(
            [{"price": "0.94", "size": "1"}, {"price": "0.93", "size": "10"}],
            Decimal("2.7"),
        )
        self.assertEqual(method, "ORDER_BOOK_VWAP")
        self.assertEqual(fillable, Decimal("2.7"))
        expected = (Decimal("0.94") * 1 + Decimal("0.93") * Decimal("1.7")) / Decimal("2.7")
        self.assertEqual(vwap, expected)

    def test_partial_fill_returns_partial_vwap_over_available(self):
        vwap, method, fillable = book_walk_vwap(
            [{"price": "0.66", "size": "1"}, {"price": "0.60", "size": "1"}],
            Decimal("5"),
        )
        self.assertEqual(method, "ORDER_BOOK_VWAP_PARTIAL")
        self.assertEqual(fillable, Decimal("2"))
        self.assertEqual(vwap, (Decimal("0.66") + Decimal("0.60")) / Decimal("2"))

    def test_no_levels_falls_back_to_best_bid(self):
        self.assertEqual(
            book_walk_vwap([], Decimal("5"), best_bid="0.63"),
            (Decimal("0.63"), "BEST_BID_FALLBACK", Decimal("0")),
        )

    def test_no_levels_no_best_bid_is_none(self):
        self.assertEqual(
            book_walk_vwap([], Decimal("5")),
            (None, "NONE", Decimal("0")),
        )

    def test_non_positive_size_falls_back(self):
        self.assertEqual(
            book_walk_vwap([{"price": "0.9", "size": "1"}], Decimal("0"), best_bid="0.9"),
            (Decimal("0.9"), "BEST_BID_FALLBACK", Decimal("0")),
        )


if __name__ == "__main__":
    unittest.main()

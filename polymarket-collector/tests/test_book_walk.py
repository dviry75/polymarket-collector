import sys
import unittest
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.book_walk import (
    book_walk_detail,
    book_walk_vwap,
    cumulative_bid_depth,
    normalize_bid_levels,
)


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


class BookWalkDetailTests(unittest.TestCase):
    def test_full_fill_reports_worst_price_and_levels(self):
        detail = book_walk_detail(
            [
                {"price": "0.67", "size": "2"},
                {"price": "0.66", "size": "2"},
                {"price": "0.64", "size": "10"},
            ],
            Decimal("5"),
        )
        self.assertEqual(detail.method, "ORDER_BOOK_VWAP")
        self.assertEqual(detail.fillable_shares, Decimal("5"))
        self.assertEqual(detail.worst_price, Decimal("0.64"))
        self.assertEqual(detail.levels_consumed, 3)
        expected = (
            Decimal("0.67") * 2 + Decimal("0.66") * 2 + Decimal("0.64") * 1
        ) / Decimal("5")
        self.assertEqual(detail.vwap, expected)

    def test_fill_within_top_level_touches_one_level(self):
        detail = book_walk_detail([{"price": "0.7", "size": "20"}], Decimal("5"))
        self.assertEqual(detail.worst_price, Decimal("0.7"))
        self.assertEqual(detail.levels_consumed, 1)

    def test_partial_fill_worst_price_is_last_level(self):
        detail = book_walk_detail(
            [{"price": "0.66", "size": "0.5"}, {"price": "0.47", "size": "3"}],
            Decimal("5"),
        )
        self.assertEqual(detail.method, "ORDER_BOOK_VWAP_PARTIAL")
        self.assertEqual(detail.fillable_shares, Decimal("3.5"))
        self.assertEqual(detail.worst_price, Decimal("0.47"))
        self.assertEqual(detail.levels_consumed, 2)

    def test_empty_ladder_fallback_has_no_worst_price(self):
        detail = book_walk_detail([], Decimal("5"), best_bid="0.63")
        self.assertEqual(detail.method, "BEST_BID_FALLBACK")
        self.assertIsNone(detail.worst_price)
        self.assertEqual(detail.levels_consumed, 0)

    def test_vwap_wrapper_still_returns_three_tuple(self):
        result = book_walk_vwap(
            [{"price": "0.9", "size": "10"}], Decimal("2"),
        )
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[1], "ORDER_BOOK_VWAP")


class CumulativeBidDepthTests(unittest.TestCase):
    def test_buckets_are_cumulative_and_inclusive(self):
        depth = cumulative_bid_depth(
            [
                {"price": "0.67", "size": "2"},
                {"price": "0.60", "size": "3"},
                {"price": "0.55", "size": "4"},
                {"price": "0.46", "size": "10"},
            ],
            [Decimal("0.66"), Decimal("0.60"), Decimal("0.55"), Decimal("0.46")],
        )
        self.assertEqual(depth[Decimal("0.66")], Decimal("2"))
        self.assertEqual(depth[Decimal("0.60")], Decimal("5"))
        self.assertEqual(depth[Decimal("0.55")], Decimal("9"))
        self.assertEqual(depth[Decimal("0.46")], Decimal("19"))

    def test_price_equal_to_level_counts(self):
        depth = cumulative_bid_depth(
            [{"price": "0.66", "size": "7"}], [Decimal("0.66")]
        )
        self.assertEqual(depth[Decimal("0.66")], Decimal("7"))

    def test_all_bids_below_lowest_level(self):
        depth = cumulative_bid_depth(
            [{"price": "0.40", "size": "5"}], [Decimal("0.46")]
        )
        self.assertEqual(depth[Decimal("0.46")], Decimal("0"))

    def test_empty_ladder_is_zero(self):
        depth = cumulative_bid_depth([], [Decimal("0.66")])
        self.assertEqual(depth[Decimal("0.66")], Decimal("0"))


if __name__ == "__main__":
    unittest.main()

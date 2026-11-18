from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from banknifty.depth_alignment import align_latest_depth
from banknifty.depth_feed import DepthTick


START = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
SYMBOLS = tuple(f"NSE-B{i}" for i in range(14))


def books():
    return {symbol: DepthTick(symbol, START + timedelta(milliseconds=100 * index),
                              START + timedelta(milliseconds=500 + 100 * index),
                              ((100.0, 10.0),), ((101.0, 5.0),))
            for index, symbol in enumerate(SYMBOLS)}


class DepthAlignmentTests(unittest.TestCase):
    def test_all_fourteen_near_time_books_can_be_summed(self):
        report = align_latest_depth(books(), SYMBOLS)
        self.assertEqual(report["status"], "aligned_trial")
        self.assertEqual((report["buy_shares"], report["sell_shares"]), (140, 70))
        self.assertEqual((report["buy_order_value_inr"], report["sell_order_value_inr"]),
                         (14000, 7070))
        self.assertLessEqual(report["exchange_span_seconds"], 2)

    def test_missing_stale_or_incomplete_book_has_no_total(self):
        complete = books()
        missing = dict(complete)
        del missing[SYMBOLS[-1]]
        self.assertIsNone(align_latest_depth(missing, SYMBOLS)["buy_shares"])
        stale = dict(complete)
        stale[SYMBOLS[-1]] = replace(stale[SYMBOLS[-1]],
                                     exchange_at=START - timedelta(seconds=10))
        self.assertEqual(align_latest_depth(stale, SYMBOLS)["reason"], "books_not_near_time")
        one_sided = dict(complete)
        one_sided[SYMBOLS[-1]] = replace(one_sided[SYMBOLS[-1]], sell_levels=())
        self.assertEqual(align_latest_depth(one_sided, SYMBOLS)["reason"], "incomplete_book_side")
        late = dict(complete)
        late[SYMBOLS[-1]] = replace(late[SYMBOLS[-1]],
                                    received_at=START + timedelta(seconds=10))
        self.assertIsNone(align_latest_depth(late, SYMBOLS)["buy_shares"])


if __name__ == "__main__":
    unittest.main()

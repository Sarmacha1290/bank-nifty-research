"""Check declared candle rules and point-in-time requirements."""
from datetime import datetime, timedelta, timezone
import unittest

from banknifty.candle_patterns import PATTERN_NAMES, analyze_candle_patterns
from banknifty.core import Candle


START = datetime(2026, 10, 5, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))


def series(prices):
    return [Candle("NSE-BANKNIFTY", "index", START + timedelta(minutes=i),
                   START + timedelta(minutes=i + 1), *price, "synthetic")
            for i, price in enumerate(prices)]


class CandlePatternTests(unittest.TestCase):
    def check(self, prices):
        rows = series(prices)
        return analyze_candle_patterns(rows, rows[-1].end)

    def test_doji_and_wick_ratios(self):
        result = self.check([(100, 102, 99, 101), (101, 103, 100, 102),
                             (102, 104, 100, 102)])
        self.assertTrue(result["patterns"]["doji"])
        self.assertTrue(result["patterns"]["long_legged_doji"])
        self.assertEqual(result["body_to_range_pct"], 0)
        self.assertEqual(set(result["patterns"]), set(PATTERN_NAMES))

    def test_engulfing_and_inside_bar_do_not_use_future(self):
        prices = [(100, 102, 98, 101), (101, 102, 95, 96), (95, 104, 94, 103)]
        result = self.check(prices)
        self.assertTrue(result["patterns"]["bullish_engulfing"])
        self.assertTrue(result["patterns"]["outside_bar"])
        rows = series(prices + [(103, 110, 102, 108)])
        at_third = analyze_candle_patterns(rows[:3], rows[2].end)
        self.assertEqual(at_third["patterns"], result["patterns"])
        with self.assertRaisesRegex(ValueError, "future"):
            analyze_candle_patterns(rows, rows[2].end)

    def test_hammer_shape_does_not_assert_reversal(self):
        result = self.check([(100, 101, 99, 100), (100, 101, 99, 100),
                             (100, 102.2, 94, 102)])
        self.assertTrue(result["patterns"]["hammer_shape"])
        self.assertIn("not_predictions", result["interpretation"])

    def test_gaps_and_zero_range(self):
        flat = self.check([(100, 100, 100, 100)] * 3)
        self.assertIsNone(flat["body_to_range_pct"])
        self.assertFalse(any(flat["patterns"].values()))
        rows = series([(100, 101, 99, 100)] * 3)
        broken = [rows[0], rows[2], Candle("NSE-BANKNIFTY", "index",
            START + timedelta(minutes=3), START + timedelta(minutes=4),
            100, 101, 99, 100, "synthetic")]
        with self.assertRaisesRegex(ValueError, "missing"):
            analyze_candle_patterns(broken, broken[-1].end)


if __name__ == "__main__":
    unittest.main()

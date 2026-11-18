from datetime import datetime, timedelta, timezone
import unittest

from banknifty.candle_charts import completed_chart_bars
from banknifty.core import Candle


START = datetime(2026, 10, 5, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))


def sample(count):
    return [Candle("NSE-BANKNIFTY", "index", START + timedelta(minutes=i),
                   START + timedelta(minutes=i+1), 100+i, 102+i, 99+i,
                   101+i, "synthetic") for i in range(count)]


class CandleChartTests(unittest.TestCase):
    def test_five_minute_ohlc_and_partial_exclusion(self):
        bars = completed_chart_bars(sample(11), 5)
        self.assertEqual(len(bars), 2)
        self.assertEqual((bars[0].open, bars[0].high, bars[0].low, bars[0].close),
                         (100, 106, 99, 105))
        self.assertIsNone(bars[0].volume)

    def test_gap_drops_only_affected_group(self):
        rows = sample(15)
        bars = completed_chart_bars(rows[:2] + rows[3:], 5)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].start, rows[5].start)
        self.assertEqual(bars[1].start, rows[10].start)

    def test_invalid_interval_rejected(self):
        with self.assertRaises(ValueError):
            completed_chart_bars(sample(10), 7)


if __name__ == "__main__":
    unittest.main()

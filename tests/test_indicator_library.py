"""Check known arithmetic and reject future or incomplete source coverage."""
from datetime import datetime, timedelta, timezone
from math import log
import unittest

from banknifty.core import Candle
from banknifty.indicator_library import calculate_aligned_beta, calculate_price_indicators


IST = timezone(timedelta(hours=5, minutes=30))
START = datetime(2026, 10, 5, 9, 15, tzinfo=IST)


def candles(*, kind="index", rising=False, count=61, volume=None):
    rows = []
    for minute in range(count):
        price = 100.0 + minute if rising else 100.0
        start = START + timedelta(minutes=minute)
        rows.append(Candle("BANKNIFTY" if kind == "index" else "BANKNIFTYFUT",
                           kind, start, start + timedelta(minutes=1),
                           price, price + 1, price - 1, price, "synthetic",
                           volume=volume))
    return rows


class IndicatorLibraryTests(unittest.TestCase):
    def test_flat_series_has_known_bands_range_and_no_index_volume(self):
        rows = candles()
        values = calculate_price_indicators(rows, rows[-1].end)["features"]
        self.assertEqual(values["return_60m_pct"], 0)
        self.assertEqual(values["macd"], 0)
        self.assertEqual(values["bollinger_width_20_pct"], 0)
        self.assertEqual(values["stochastic_k_14_pct"], 50)
        self.assertEqual(values["true_range"], 2)
        self.assertEqual(values["atr_14"], 2)
        self.assertIsNone(values["vwap"])
        self.assertIsNone(values["session_return_pct"])

    def test_rising_series_uses_only_past_and_previous_session_explicitly(self):
        rows = candles(rising=True, count=62)
        as_of = rows[60].end
        prior = Candle("BANKNIFTY", "index", START - timedelta(days=1),
                       START - timedelta(hours=18), 99, 105, 95, 100, "synthetic")
        values = calculate_price_indicators(rows[:61], as_of,
                                            session_open=START,
                                            previous_session=prior)["features"]
        self.assertAlmostEqual(values["return_1m_pct"], 100 * (160 / 159 - 1))
        self.assertAlmostEqual(values["log_return_1m_pct"], 100 * log(160 / 159))
        self.assertAlmostEqual(values["session_return_pct"], 60)
        self.assertEqual(values["opening_range_high"], 115)
        self.assertEqual(values["classic_pivot"], 100)
        self.assertEqual(values["momentum_alignment"], "up")
        self.assertAlmostEqual(values["macd"], 7)
        self.assertAlmostEqual(values["macd_histogram"], 0)
        with self.assertRaisesRegex(ValueError, "future"):
            calculate_price_indicators(rows, as_of)
        with self.assertRaisesRegex(ValueError, "before"):
            calculate_price_indicators(rows[:61], as_of, session_open=START,
                                       previous_session=rows[0])

    def test_traded_future_volume_is_required_and_zero_history_is_blank(self):
        rows = candles(kind="future", volume=10)
        values = calculate_price_indicators(rows, rows[-1].end)["features"]
        self.assertEqual(values["vwap"], 100)
        self.assertEqual(values["relative_volume_20"], 1)
        self.assertIsNone(values["volume_zscore_20"])
        no_volume = candles(kind="future")
        values = calculate_price_indicators(no_volume, no_volume[-1].end)["features"]
        self.assertIsNone(values["vwap"])
        with self.assertRaisesRegex(ValueError, "61"):
            calculate_price_indicators(rows[:60], rows[59].end)

    def test_rolling_bank_beta_requires_exactly_aligned_closes(self):
        index = candles(rising=True)
        bank = [Candle("NSE-BANK", "equity", row.start, row.end,
                       200 + 2 * minute, 201 + 2 * minute,
                       199 + 2 * minute, 200 + 2 * minute, "synthetic")
                for minute, row in enumerate(index)]
        result = calculate_aligned_beta(index, bank, index[-1].end)
        self.assertAlmostEqual(result["correlation"], 1, places=3)
        with self.assertRaises(ValueError):
            shifted = [Candle(row.symbol, row.kind,
                              row.start + timedelta(minutes=1), row.end + timedelta(minutes=1),
                              row.open, row.high, row.low, row.close, row.source) for row in bank]
            calculate_aligned_beta(index, shifted, shifted[-1].end)


if __name__ == "__main__":
    unittest.main()

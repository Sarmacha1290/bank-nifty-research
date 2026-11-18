"""Indicator correctness and point-in-time boundaries for the offline role."""
from datetime import datetime, timedelta, timezone
import unittest

from banknifty.core import Candle
from banknifty.technical_features import build_technical_snapshot


IST = timezone(timedelta(hours=5, minutes=30))
OPEN = datetime(2026, 10, 1, 9, 15, tzinfo=IST)


def make_candles(count=61, *, rising=False):
    candles = []
    for minute in range(count):
        price = float(100 + minute if rising else 100)
        start = OPEN + timedelta(minutes=minute)
        candles.append(Candle(symbol="DEMO-BANKNIFTY", kind="index", source="synthetic",
            start=start, end=start + timedelta(minutes=1),
            open=price, high=price + 1, low=price - 1, close=price))
    return candles


class TechnicalFeatureTests(unittest.TestCase):
    def test_flat_index_has_no_invented_volume_or_direction(self):
        candles = make_candles()
        result = build_technical_snapshot(candles, candles[-1].end, generated_at=candles[-1].end)
        self.assertEqual(result.return_60m_pct, 0)
        self.assertEqual(result.rsi_14, 50)
        self.assertEqual(result.trend_strength, 0)
        self.assertAlmostEqual(result.atr_pct, 2)
        self.assertIsNone(result.vwap_distance_pct)
        self.assertIsNone(result.volume_zscore)

    def test_rising_series_uses_only_past_completed_closes(self):
        candles = make_candles(62, rising=True)
        as_of = candles[60].end
        result = build_technical_snapshot(candles[:61], as_of, generated_at=as_of)
        self.assertEqual(result.current_price, 160)
        self.assertAlmostEqual(result.return_60m_pct, 60)
        self.assertEqual(result.rsi_14, 100)
        self.assertAlmostEqual(result.trend_strength, 100)
        with self.assertRaisesRegex(ValueError, "future"):
            build_technical_snapshot(candles, as_of, generated_at=as_of)

    def test_insufficient_or_gapped_series_is_rejected(self):
        candles = make_candles()
        with self.assertRaisesRegex(ValueError, "61"):
            build_technical_snapshot(candles[:60], candles[59].end, generated_at=candles[59].end)
        with self.assertRaisesRegex(ValueError, "missing"):
            build_technical_snapshot(candles[:20] + candles[21:], candles[-1].end,
                                     generated_at=candles[-1].end)


if __name__ == "__main__":
    unittest.main()

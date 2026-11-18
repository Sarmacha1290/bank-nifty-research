from datetime import datetime, timedelta, timezone
import json
import unittest

from banknifty.agents.luna import FeatureSnapshot, assess

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def snapshot(**changes):
    values = {
        "as_of": datetime(2026, 10, 1, 10, 0, tzinfo=IST),
        "symbol": "NSE-BANKNIFTY", "current_price": 55000.0,
        "return_5m_pct": 0.20, "return_15m_pct": 0.55,
        "return_30m_pct": 0.75, "return_60m_pct": 1.10,
        "vwap_distance_pct": 0.30, "ema_fast_distance_pct": 0.25,
        "ema_slow_distance_pct": 0.18, "rsi_14": 58.0, "atr_pct": 0.7,
        "trend_strength": 65.0, "data_freshness_seconds": 5.0,
        "source": "fixture", "volume_zscore": 1.0,
    }
    values.update(changes)
    return FeatureSnapshot(**values)


class LunaTests(unittest.TestCase):
    def test_bullish_assessment_is_structured_and_not_probability(self):
        result = assess(snapshot(), 15).to_dict()
        self.assertEqual(result["agent"], "Technical")
        self.assertEqual(result["bias"], "bullish")
        self.assertIsNone(result["confidence"])
        self.assertGreaterEqual(result["score"], 20)
        self.assertTrue(result["reasons"])
        json.dumps(result, allow_nan=False)

    def test_bearish_assessment(self):
        result = assess(snapshot(return_5m_pct=-0.5, return_15m_pct=-1.0,
                                return_30m_pct=-1.2, return_60m_pct=-1.5,
                                vwap_distance_pct=-0.5, ema_fast_distance_pct=-0.4,
                                ema_slow_distance_pct=-0.3, rsi_14=42), 30)
        self.assertEqual(result.bias, "bearish")
        self.assertLess(result.score, -20)

    def test_neutral_and_overbought_risk(self):
        result = assess(snapshot(return_5m_pct=0, return_15m_pct=0,
                                return_30m_pct=0, return_60m_pct=0,
                                vwap_distance_pct=0, ema_fast_distance_pct=0,
                                ema_slow_distance_pct=0, rsi_14=75,
                                trend_strength=10), 60)
        self.assertEqual(result.bias, "neutral")
        self.assertIn("RSI is overbought (75.0)", result.risks)
        self.assertIsNone(result.confidence)

    def test_stale_data_is_unavailable(self):
        result = assess(snapshot(data_freshness_seconds=120.001), 15)
        self.assertEqual(result.bias, "unavailable")
        self.assertIsNone(result.score)
        self.assertIsNone(result.confidence)

    def test_missing_features_and_invalid_ranges_are_rejected(self):
        with self.assertRaises(ValueError):
            snapshot(rsi_14=100.1)
        with self.assertRaises(ValueError):
            snapshot(current_price=0)
        with self.assertRaises(ValueError):
            snapshot(volume_zscore=float("nan"))

    def test_horizon_and_generated_at_are_validated(self):
        with self.assertRaises(ValueError):
            assess(snapshot(), 20)
        with self.assertRaises(ValueError):
            assess(snapshot(), 15, generated_at=datetime(2026, 10, 1, 9, 59, tzinfo=IST))

    def test_repeated_replay_is_deterministic(self):
        first = assess(snapshot(), 60).to_dict()
        second = assess(snapshot(), 60).to_dict()
        self.assertEqual(first, second)

    def test_naive_snapshot_time_is_rejected(self):
        with self.assertRaises(ValueError):
            snapshot(as_of=datetime(2026, 10, 1, 10, 0))


if __name__ == "__main__":
    unittest.main()

"""Opposing roles must show counterevidence and abstain on stale input."""
from dataclasses import replace
from datetime import datetime, timezone
import unittest

from banknifty.agents.bull_bear import build_bear_case, build_bull_case
from banknifty.agents.luna import FeatureSnapshot


def snapshot():
    return FeatureSnapshot(
        as_of=datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc),
        symbol="DEMO-BANKNIFTY", current_price=55000,
        return_5m_pct=-0.12, return_15m_pct=0.25,
        return_30m_pct=0.18, return_60m_pct=-0.2,
        vwap_distance_pct=None, ema_fast_distance_pct=0.1,
        ema_slow_distance_pct=-0.08, rsi_14=55,
        atr_pct=0.5, trend_strength=28,
        data_freshness_seconds=0, source="synthetic")


class BullBearTests(unittest.TestCase):
    def test_same_cutoff_produces_opposing_auditable_views(self):
        source = snapshot()
        bull = build_bull_case(source, 15)
        bear = build_bear_case(source, 15)
        self.assertEqual((bull.as_of, bear.as_of), (source.as_of, source.as_of))
        self.assertEqual(bull.supporting_observations, bear.opposing_observations)
        self.assertEqual(bear.supporting_observations, bull.opposing_observations)
        self.assertTrue(bull.supporting_observations and bear.supporting_observations)
        self.assertTrue(all("invented" in " ".join(row.cautions) for row in (bull, bear)))
        self.assertFalse(hasattr(bull, "confidence"))

    def test_stale_input_abstains_without_reusing_old_evidence(self):
        old = replace(snapshot(), data_freshness_seconds=121)
        for agent in (build_bull_case, build_bear_case):
            result = agent(old, 30)
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(result.supporting_observations, ())
            self.assertEqual(result.opposing_observations, ())

    def test_invalid_horizon_rejected(self):
        with self.assertRaises(ValueError):
            build_bull_case(snapshot(), 45)


if __name__ == "__main__":
    unittest.main()

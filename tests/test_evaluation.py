"""Later outcomes must be matched by exact timestamp and kept out of inputs."""

from dataclasses import replace
from datetime import timedelta
import unittest

from banknifty.agents.luna import assess
from banknifty.evaluation import match_outcomes, summarize_outcomes
from banknifty.technical_features import build_technical_snapshot
from banknifty.workspace import synthetic_session


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.session, self.candles = synthetic_session()
        self.as_of = self.session.open + timedelta(minutes=75)
        observed = [candle for candle in self.candles if candle.end <= self.as_of]
        snapshot = build_technical_snapshot(observed, self.as_of, generated_at=self.as_of)
        self.assessments = [assess(snapshot, horizon) for horizon in (15, 30, 60)]

    def test_exact_horizons_and_no_score_to_return_conversion(self):
        rows = match_outcomes(self.assessments, self.candles, self.session)
        self.assertEqual([row.horizon_minutes for row in rows], [15, 30, 60])
        for row in rows:
            self.assertEqual(row.target_at, self.as_of + timedelta(minutes=row.horizon_minutes))
            self.assertEqual(row.status, "ready")
            self.assertAlmostEqual(row.realized_return_pct,
                                   100 * (self.candles[74 + row.horizon_minutes].close /
                                          self.candles[74].close - 1))
            self.assertIsNone(row.direction_hit) if row.assessment_bias == "neutral" else self.assertIsInstance(row.direction_hit, bool)
        summary = summarize_outcomes(rows)
        self.assertEqual(set(summary), {15, 30, 60})
        self.assertTrue(all(value["label_coverage"] == 1 for value in summary.values()))

    def test_missing_future_minute_has_no_label(self):
        missing = [candle for candle in self.candles if candle.end != self.as_of + timedelta(minutes=7)]
        rows = match_outcomes(self.assessments, missing, self.session)
        self.assertTrue(all(row.status == "missing_data" and row.realized_return_pct is None for row in rows))
        self.assertTrue(all(value["label_coverage"] == 0 for value in summarize_outcomes(rows).values()))

    def test_rejects_late_or_mixed_origins(self):
        late = replace(self.assessments[0], generated_at=self.as_of + timedelta(minutes=1))
        with self.assertRaisesRegex(ValueError, "fixed at their origin"):
            match_outcomes([late], self.candles, self.session)
        moved = replace(self.assessments[0], as_of=self.as_of + timedelta(minutes=1))
        with self.assertRaisesRegex(ValueError, "shared origin"):
            match_outcomes([moved, self.assessments[1]], self.candles, self.session)
        wrong_source = replace(self.assessments[0], source="other")
        with self.assertRaisesRegex(ValueError, "match assessment"):
            match_outcomes([wrong_source], self.candles, self.session)


if __name__ == "__main__":
    unittest.main()

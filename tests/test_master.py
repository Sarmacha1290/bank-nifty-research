"""Master cannot manufacture a forecast or combine mismatched cutoffs."""
from dataclasses import replace
from datetime import timedelta
import unittest

from banknifty.agents.bull_bear import build_bear_case, build_bull_case
from banknifty.agents.luna import assess
from banknifty.agents.master import review_bundle
from banknifty.agents.risk import check_risk
from banknifty.agents.verification import verify_bundle
from banknifty.technical_features import build_technical_snapshot
from banknifty.workspace import synthetic_session


def sample_bundle():
    session, history = synthetic_session()
    as_of = session.open + timedelta(minutes=75)
    candles = [row for row in history if row.end <= as_of]
    snapshot = build_technical_snapshot(candles, as_of, generated_at=as_of)
    technical = [assess(snapshot, horizon) for horizon in (15, 30, 60)]
    bull = [build_bull_case(snapshot, horizon) for horizon in (15, 30, 60)]
    bear = [build_bear_case(snapshot, horizon) for horizon in (15, 30, 60)]
    verification = verify_bundle(candles, snapshot, technical, session)
    risk = check_risk(snapshot, technical, verification, session)
    return snapshot, technical, bull, bear, verification, risk


class MasterTests(unittest.TestCase):
    def test_invented_evidence_is_blocked_for_all_horizons(self):
        result = review_bundle(*sample_bundle())
        self.assertEqual([row.horizon_minutes for row in result.horizons], [15, 30, 60])
        self.assertTrue(all(row.status == "blocked" for row in result.horizons))
        self.assertFalse(result.forecast_available)
        self.assertFalse(result.trading_allowed)

    def test_rejects_mismatched_or_duplicate_specialist_records(self):
        snapshot, technical, bull, bear, verification, risk = sample_bundle()
        shifted = [replace(bull[0], as_of=bull[0].as_of + timedelta(minutes=1)), *bull[1:]]
        with self.assertRaisesRegex(ValueError, "cutoff"):
            review_bundle(snapshot, technical, shifted, bear, verification, risk)
        with self.assertRaisesRegex(ValueError, "one valid record"):
            review_bundle(snapshot, technical, bull[:2] + bull[:1], bear, verification, risk)


if __name__ == "__main__":
    unittest.main()

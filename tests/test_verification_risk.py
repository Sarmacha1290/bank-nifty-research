from dataclasses import replace
from datetime import datetime, timedelta
import unittest

from banknifty.agents.luna import assess
from banknifty.agents.risk import check_risk
from banknifty.agents.verification import verify_bundle
from banknifty.feed import IndexTick
from banknifty.feed_technical import build_feed_evidence
from banknifty.groww import IST
from banknifty.market_calendar import regular_session
from banknifty.technical_features import build_technical_snapshot
from banknifty.workspace import synthetic_session


class VerificationRiskTests(unittest.TestCase):
    def test_synthetic_structure_can_pass_but_paper_gate_stays_closed(self):
        session, candles = synthetic_session()
        as_of = session.open + timedelta(minutes=75)
        observed = candles[:75]
        snapshot = build_technical_snapshot(observed, as_of, generated_at=as_of)
        rows = [assess(snapshot, minutes) for minutes in (15, 30, 60)]
        verification = verify_bundle(observed, snapshot, rows, session)
        self.assertTrue(verification.passed)
        self.assertIn("invented", " ".join(verification.limitations))
        gate = check_risk(snapshot, rows, verification, session)
        self.assertTrue(all(not item.paper_eligible for item in gate.horizons))
        self.assertFalse(gate.public_forecast_allowed)
        self.assertFalse(gate.trading_allowed)
        wrong = [replace(rows[0], source="other"), *rows[1:]]
        self.assertFalse(verify_bundle(observed, snapshot, wrong, session).passed)

    def test_recent_groww_sample_may_enter_paper_log_but_not_public_forecast(self):
        start = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
        ticks = [IndexTick("NSE-BANKNIFTY", at, at + timedelta(seconds=1),
                           55000 + minute * 0.5 + second * 0.01)
                 for minute in range(61) for second in range(0, 60, 5)
                 for at in [start + timedelta(minutes=minute, seconds=second)]]
        quality, candles, snapshot, rows = build_feed_evidence(
            ticks, generated_at=start + timedelta(minutes=61, seconds=20))
        session = regular_session(start.date())
        verification = verify_bundle(candles, snapshot, rows, session)
        self.assertTrue(verification.passed)
        gate = check_risk(snapshot, rows, verification, session)
        self.assertEqual([item.paper_eligible for item in gate.horizons], [True, True, True])
        self.assertFalse(gate.public_forecast_allowed)


if __name__ == "__main__":
    unittest.main()

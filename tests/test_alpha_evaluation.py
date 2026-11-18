"""Alpha requires matched forward net returns, not spot direction labels."""
from datetime import date, datetime, timedelta, timezone
import unittest

from banknifty.alpha_evaluation import MatchedPaperReturn, evaluate_paper_alpha
from banknifty.groww import IST
from banknifty.market_calendar import regular_session_date


class AlphaEvaluationTests(unittest.TestCase):
    def rows(self):
        start = date(2026, 1, 1)
        result = []
        day = start
        while len(result) < 20:
            if not regular_session_date(day)[0]:
                day += timedelta(days=1)
                continue
            index = len(result)
            decided = datetime(day.year, day.month, day.day, 10, 0, tzinfo=IST)
            benchmark = float(index % 5 - 2)
            result.append(MatchedPaperReturn(day, decided, decided + timedelta(hours=5),
                                             0.1 + 1.5 * benchmark, benchmark,
                                             "verified_forward_paper"))
            day += timedelta(days=1)
        return result

    def test_known_intercept_and_beta(self):
        result = evaluate_paper_alpha(self.rows())
        self.assertEqual(result["days"], 20)
        self.assertAlmostEqual(result["beta"], 1.5)
        self.assertAlmostEqual(result["regression_alpha_pct_per_day"], 0.1)
        self.assertIn("not_significance", result["interpretation"])

    def test_rejects_small_unverified_or_unvarying_samples(self):
        rows = self.rows()
        with self.assertRaisesRegex(ValueError, "20"):
            evaluate_paper_alpha(rows[:19])
        rows[0] = MatchedPaperReturn(rows[0].session_date, rows[0].decided_at,
                                     rows[0].realized_at, 10, 0, "retrospective")
        with self.assertRaisesRegex(ValueError, "verified"):
            evaluate_paper_alpha(rows)
        rows = self.rows()
        flat = [MatchedPaperReturn(row.session_date, row.decided_at, row.realized_at,
                                   row.strategy_net_return_pct, 0,
                                   row.source_status) for row in rows]
        with self.assertRaisesRegex(ValueError, "no variation"):
            evaluate_paper_alpha(flat)


if __name__ == "__main__":
    unittest.main()

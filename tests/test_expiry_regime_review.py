import unittest

from banknifty.expiry_regime_review import review_expiry_regimes


def row(day, horizon, regime, change, *, label="ready"):
    return {"session_date_ist": day, "partition": "development",
            "horizon_minutes": horizon, "is_expiry_day": regime == "expiry" if regime else None,
            "is_pre_expiry_session": regime == "pre_expiry" if regime else None,
            "label_status": label, "target_return_pct": change}


class ExpiryRegimeReviewTests(unittest.TestCase):
    def test_separate_regimes_and_unknown_or_missing_outcomes(self):
        report = {"structural_audit": {"status": "structural_checks_passed_not_source_authentication",
                                       "checked_rows": 5},
                  "rows": [row("2026-10-27", 15, "expiry", 1.2),
                           row("2026-10-26", 15, "pre_expiry", -.4),
                           row("2026-10-05", 15, "ordinary", .2),
                           row("2026-10-06", 15, None, 2),
                           row("2026-10-27", 30, "expiry", None, label="missing_data")]}
        result = review_expiry_regimes(report)
        self.assertEqual(result["unknown_expiry_rows"], 1)
        self.assertEqual(result["unavailable_label_rows"], 1)
        self.assertEqual(len(result["summary"]), 3)
        expiry = next(item for item in result["summary"] if item["regime"] == "expiry")
        self.assertEqual(expiry["mean_absolute_move_pct"], 1.2)
        self.assertEqual(expiry["session_days"], 1)
        self.assertIn("insufficient", expiry["evidence_status"])

    def test_unverified_rows_rejected(self):
        with self.assertRaises(ValueError):
            review_expiry_regimes({"rows": [], "structural_audit": {"status": "unverified"}})


if __name__ == "__main__":
    unittest.main()

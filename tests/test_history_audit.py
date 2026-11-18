"""Coverage failures seen in real Groww responses must remain visible."""
from datetime import date, datetime, timedelta
import unittest

from banknifty.history_audit import audit_session


DAY = date(2026, 9, 25)


def response(count):
    opening = datetime(2026, 9, 25, 9, 15)
    return {"status": "SUCCESS", "payload": {"interval_in_minutes": 1,
        "candles": [[(opening + timedelta(minutes=i)).isoformat(), 100, 101, 99, 100.5, None, None]
                    for i in range(count)]}}


class HistoryAuditTests(unittest.TestCase):
    def test_inclusive_376_row_response_stays_boundary_ambiguous(self):
        result = audit_session(response(376), DAY)
        self.assertEqual(result["status"], "boundary_ambiguous")
        self.assertEqual(result["missing_if_start_stamped"], 0)
        self.assertEqual(result["missing_if_end_stamped"], 0)
        self.assertFalse(result["ready_for_forecast_archive"])

    def test_partial_or_absent_response_is_not_usable(self):
        partial = audit_session(response(290), DAY)
        self.assertEqual(partial["status"], "incomplete_or_irregular")
        self.assertGreater(partial["missing_if_start_stamped"], 0)
        self.assertEqual(audit_session(response(0), DAY)["status"], "no_data")

    def test_wrong_date_and_duplicates_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "date"):
            audit_session(response(1), date(2026, 9, 26))
        duplicated = response(2)
        duplicated["payload"]["candles"][1][0] = duplicated["payload"]["candles"][0][0]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            audit_session(duplicated, DAY)


if __name__ == "__main__":
    unittest.main()

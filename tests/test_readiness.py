import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path
from banknifty.groww import IST

from banknifty.readiness import check_local_readiness, next_reviewed_session




class ReadinessTests(unittest.TestCase):
    def test_sunday_points_to_next_reviewed_open(self):
        instant = datetime(2026, 10, 4, 8, 0, tzinfo=IST)
        self.assertEqual(next_reviewed_session(instant),
                         datetime(2026, 10, 5, 9, 15, tzinfo=IST))

    def test_open_session_does_not_report_past_opening_as_next(self):
        self.assertEqual(next_reviewed_session(
            datetime(2026, 10, 5, 10, 0, tzinfo=IST)),
            datetime(2026, 10, 6, 9, 15, tzinfo=IST))

    def test_unknown_next_calendar_year_fails_closed(self):
        self.assertIsNone(next_reviewed_session(datetime(2026, 12, 31, 17, 0, tzinfo=IST)))

    def test_local_checks_do_not_need_token_or_feed(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "market"
            result = check_local_readiness(folder,
                                           now=datetime(2026, 10, 4, 8, 0, tzinfo=IST))
            self.assertTrue(result["archive_writable"])
            self.assertEqual(result["recorded_ticks"], 0)
            self.assertEqual(result["market_status"], "weekend")
            self.assertEqual(result["next_reviewed_open_ist"], "2026-10-05T09:15:00+05:30")
            self.assertIsInstance(result["groww_socket_guard_reviewed"], bool)
            self.assertEqual(list(folder.iterdir()), [])

    def test_new_sdk_version_is_not_automatically_cleared_for_feed(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("banknifty.groww_feed_security.version", return_value="future-version"):
                report = check_local_readiness(Path(temporary),
                    now=datetime(2026, 10, 4, 8, 0, tzinfo=IST))
        self.assertFalse(report["groww_socket_guard_reviewed"])


if __name__ == "__main__":
    unittest.main()

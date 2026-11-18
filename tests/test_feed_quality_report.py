import csv
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from banknifty.groww import IST

from banknifty.feed import IndexTick
from banknifty.feed_quality_report import export_feed_quality_csv, summarize_recorded_feed
from banknifty.tick_history import count_index_ticks, save_index_tick




class FeedQualityReportTests(unittest.TestCase):
    def test_full_session_map_distinguishes_accepted_weak_and_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "market.sqlite3"
            start = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
            for offset in (*range(0, 60, 5), 60, 65):
                exchange_at = start + timedelta(seconds=offset)
                save_index_tick(database, IndexTick("NSE-BANKNIFTY", exchange_at,
                                                    exchange_at + timedelta(seconds=1), 50000.0))
            destination = Path(temporary) / "quality.csv"
            result = export_feed_quality_csv(database, destination)
            self.assertEqual(result, {"rows": 375, "session_dates": 1,
                                      "observed_minutes": 2, "accepted_minutes": 1})
            with destination.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["coverage_status"], "accepted")
            self.assertEqual(rows[0]["sample_count"], "12")
            self.assertEqual(rows[1]["coverage_status"], "too_few_samples")
            self.assertEqual(rows[2]["coverage_status"], "missing_whole_minute")
            self.assertEqual(rows[-1]["expected_minute_end_ist"], "2026-10-05T15:30:00+05:30")
            self.assertEqual(count_index_ticks(database), 14)
            daily = summarize_recorded_feed(database)
            self.assertEqual(len(daily), 1)
            self.assertEqual(daily[0]["accepted_minutes"], 1)
            self.assertEqual(daily[0]["rejected_minutes"], 1)
            self.assertEqual(daily[0]["missing_whole_minutes"], 373)
            with self.assertRaisesRegex(ValueError, "must not overwrite"):
                export_feed_quality_csv(database, database)

    def test_no_observations_creates_no_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "quality.csv"
            with self.assertRaisesRegex(ValueError, "No regular-session"):
                export_feed_quality_csv(Path(temporary) / "missing.sqlite3", destination)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()

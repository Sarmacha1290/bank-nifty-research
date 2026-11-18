"""A daily range needs every regular-session minute and exact future labels."""
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from decimal import Decimal

from banknifty.core import Candle
from banknifty.daily_range import (build_daily_range_history, export_daily_range_csv,
                                   label_complete_day, compare_official_and_sampled,
                                   export_range_comparison_csv)
from banknifty.feed import IndexTick
from banknifty.feed_quality import SAMPLED_SOURCE
from banknifty.market_calendar import regular_session
from banknifty.tick_history import save_index_tick
from banknifty.official_daily import OfficialDailyBar, official_url, save_official_day


SESSION = regular_session(date(2026, 10, 5))


def complete_bars():
    rows = []
    for offset in range(375):
        start = SESSION.open + timedelta(minutes=offset)
        high = 110 if offset == 0 else 101
        low = 80 if offset == 374 else 99
        rows.append(Candle("NSE-BANKNIFTY", "index", start,
                           start + timedelta(minutes=1), 100, high, low, 100,
                           SAMPLED_SOURCE))
    return rows


class DailyRangeTests(unittest.TestCase):
    def test_complete_day_and_exact_remaining_range_are_offline_labels(self):
        rows = complete_bars()
        label = label_complete_day(rows, SESSION, as_of=SESSION.open + timedelta(minutes=45))
        self.assertEqual((label["sampled_day_high"], label["sampled_day_low"]), (110, 80))
        self.assertEqual(label["sampled_range_points"], 30)
        self.assertEqual((label["sampled_remaining_high"], label["sampled_remaining_low"]), (101, 80))
        self.assertEqual(label["sampled_remaining_range_points"], 21)
        self.assertIn("not_official_ohlc_or_prediction", label["interpretation"])

    def test_gap_wrong_source_and_invalid_cutoff_have_no_label(self):
        rows = complete_bars()
        for changed in (rows[1:], rows[:50] + rows[51:],
                        [replace(rows[0], source="synthetic"), *rows[1:]]):
            with self.assertRaises(ValueError):
                label_complete_day(changed, SESSION)
        with self.assertRaisesRegex(ValueError, "Cutoff"):
            label_complete_day(rows, SESSION, as_of=SESSION.close)

    def test_partial_recording_exports_coverage_but_no_range(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            for offset in range(0, 60, 5):
                at = SESSION.open + timedelta(seconds=offset)
                save_index_tick(database, IndexTick("NSE-BANKNIFTY", at,
                                                     at + timedelta(seconds=1), 100.0))
            row = build_daily_range_history(database)[0]
            self.assertEqual((row["observed_minutes"], row["accepted_minutes"]), (1, 1))
            self.assertEqual(row["status"], "insufficient_coverage")
            self.assertIsNone(row["sampled_range_points"])
            destination = Path(folder) / "range.csv"
            self.assertEqual(export_daily_range_csv(database, destination), 1)
            self.assertIn("insufficient_coverage", destination.read_text(encoding="utf-8-sig"))
            with self.assertRaises(ValueError):
                export_daily_range_csv(database, database)

    def test_empty_archive_exports_nothing(self):
        with TemporaryDirectory() as folder:
            database, destination = Path(folder) / "market.sqlite3", Path(folder) / "range.csv"
            self.assertEqual(build_daily_range_history(database), [])
            with self.assertRaises(ValueError):
                export_daily_range_csv(database, destination)
            self.assertFalse(destination.exists())

    def test_official_comparison_separates_missing_complete_and_outside_sample(self):
        day = SESSION.open.date()
        official = OfficialDailyBar(day, Decimal("100"), Decimal("110"),
            Decimal("80"), Decimal("100"), official_url(day), "a" * 64,
            datetime(2026, 10, 6, tzinfo=timezone.utc))
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            save_official_day(database, official)
            self.assertEqual(compare_official_and_sampled(database)[0]["status"],
                             "no_sampled_session")
            destination = Path(folder) / "comparison.csv"
            self.assertEqual(export_range_comparison_csv(database, destination), 1)
            self.assertIn("no_sampled_session", destination.read_text(encoding="utf-8-sig"))
            with self.assertRaises(ValueError):
                export_range_comparison_csv(database, database)
            sample = {"session_date_ist": day.isoformat(), "status": "insufficient_coverage"}
            with patch("banknifty.daily_range.build_daily_range_history", return_value=[sample]):
                self.assertEqual(compare_official_and_sampled(database)[0]["status"],
                                 "sampled_incomplete")
            sample.update(status="complete_sampled_session", sampled_day_high=109.5,
                          sampled_day_low=80.5)
            with patch("banknifty.daily_range.build_daily_range_history", return_value=[sample]):
                row = compare_official_and_sampled(database)[0]
                self.assertEqual(row["status"], "sampled_within_official_range")
                self.assertEqual(row["official_minus_sampled_range_points"], "1.0")
            sample["sampled_day_high"] = 110.02
            with patch("banknifty.daily_range.build_daily_range_history", return_value=[sample]):
                self.assertEqual(compare_official_and_sampled(database)[0]["status"],
                                 "sampled_outside_official_range")


if __name__ == "__main__":
    unittest.main()

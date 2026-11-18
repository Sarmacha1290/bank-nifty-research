"""Reference evaluation must use earlier official days and fail closed on gaps."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.daily_reference import (evaluate_daily_reference,
                                        summarize_daily_reference,
                                        export_daily_reference_csv)
from banknifty.market_calendar import regular_session_date
from banknifty.official_daily import OfficialDailyBar, official_url, save_official_day


def daily_bars(count=45):
    day = date(2026, 1, 2)
    rows = []
    while len(rows) < count:
        if regular_session_date(day)[0]:
            price = Decimal(100 + len(rows))
            rows.append(OfficialDailyBar(day, price, price + 10, price - 10,
                price, official_url(day), "a" * 64,
                datetime(2026, 10, 4, tzinfo=timezone.utc)))
        day += timedelta(days=1)
    return rows


class DailyReferenceTests(unittest.TestCase):
    def test_evaluation_uses_past_only_and_reports_realized_errors(self):
        bars = daily_bars()
        original = evaluate_daily_reference(bars)
        self.assertEqual(len(original), 4)
        changed = list(bars)
        changed[41] = replace(changed[41], high=changed[41].high + 100)
        altered = evaluate_daily_reference(changed)
        self.assertEqual(original[0]["reference_lower"], altered[0]["reference_lower"])
        self.assertEqual(original[0]["reference_upper"], altered[0]["reference_upper"])
        self.assertNotEqual(original[0]["upper_absolute_error_points"],
                            altered[0]["upper_absolute_error_points"])
        self.assertEqual(summarize_daily_reference(original)["evaluated_days"], 4)
        self.assertEqual(summarize_daily_reference(original)["reconstructed_from_later_retrieval_days"], 4)
        self.assertTrue(all(row["source_availability"] == "reconstructed_from_later_retrieval"
                            for row in original))

    def test_source_archive_timing_is_separate_from_past_day_calculation(self):
        bars = [replace(bar, retrieved_at_utc=datetime.combine(
            bar.day + timedelta(days=1), datetime.min.time(), timezone.utc))
            for bar in daily_bars()]
        rows = evaluate_daily_reference(bars)
        self.assertEqual(len(rows), 4)
        self.assertEqual(summarize_daily_reference(rows)["archived_before_target_open_days"], 4)
        self.assertEqual(rows[0]["source_availability"], "archived_before_target_open")

    def test_gap_and_short_history_have_no_evaluated_row(self):
        bars = daily_bars()
        self.assertEqual(evaluate_daily_reference(bars[:41]), [])
        self.assertEqual(evaluate_daily_reference(bars[:20] + bars[21:]), [])
        with self.assertRaises(ValueError):
            evaluate_daily_reference(list(reversed(bars)))
        with self.assertRaises(ValueError):
            evaluate_daily_reference(bars, lookback=10)

    def test_export_is_exact_historical_rows_and_never_overwrites_db(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            for bar in daily_bars(42):
                save_official_day(database, bar)
            destination = Path(folder) / "reference.csv"
            self.assertEqual(export_daily_reference_csv(database, destination), 1)
            self.assertIn("exploratory_historical_reference",
                          destination.read_text(encoding="utf-8-sig"))
            with self.assertRaises(ValueError):
                export_daily_reference_csv(database, database)


if __name__ == "__main__":
    unittest.main()

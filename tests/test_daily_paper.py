"""Prospective range references are fixed before the target session."""
from dataclasses import replace
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from banknifty.daily_paper import (build_next_reference, load_scored_references,
                                   next_regular_day, record_next_reference,
                                   export_daily_paper_csv)
from banknifty.market_calendar import regular_session_date
from banknifty.official_daily import OfficialDailyBar, official_url, save_official_day


NOW = datetime(2026, 10, 4, 6, tzinfo=timezone.utc)
TARGET = date(2026, 10, 5)


def prior_bars():
    days = []
    cursor = date(2026, 10, 1)
    while len(days) < 42:
        if regular_session_date(cursor)[0]:
            days.append(cursor)
        cursor -= timedelta(days=1)
    rows = []
    for index, day in enumerate(reversed(days)):
        price = Decimal(55000 + index * 10)
        rows.append(OfficialDailyBar(day, price, price + 400, price - 300, price,
                    official_url(day), "a" * 64,
                    datetime(2026, 10, 3, tzinfo=timezone.utc)))
    return rows


class DailyPaperTests(unittest.TestCase):
    def test_next_date_and_past_only_reference(self):
        self.assertEqual(next_regular_day(NOW), TARGET)
        self.assertEqual(next_regular_day(datetime(2026, 10, 5, 5, tzinfo=timezone.utc)),
                         date(2026, 10, 6))
        bars = prior_bars()
        reference = build_next_reference(bars, recorded_at=NOW)
        self.assertEqual(reference["session_date_ist"], "2026-10-05")
        self.assertEqual(reference["training_days"], 40)
        self.assertEqual(reference["previous_official_date"], "2026-10-01")
        self.assertIn("not_validated", reference["interpretation"])
        with self.assertRaisesRegex(ValueError, "source was not available"):
            build_next_reference(bars[:-1] + [replace(bars[-1],
                retrieved_at_utc=NOW + timedelta(seconds=1))], recorded_at=NOW)
        with self.assertRaisesRegex(ValueError, "preceding regular session"):
            build_next_reference(bars[:-1], recorded_at=NOW)

    def test_append_once_then_score_after_official_day(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            for bar in prior_bars():
                save_official_day(database, bar)
            status, record = record_next_reference(database, recorded_at=NOW)
            self.assertEqual(status, "inserted")
            self.assertEqual(record_next_reference(database,
                recorded_at=NOW + timedelta(seconds=1))[0], "duplicate")
            self.assertEqual(load_scored_references(database)[0]["status"],
                             "awaiting_official_day")
            destination = Path(folder) / "paper.csv"
            self.assertEqual(export_daily_paper_csv(database, destination), 1)
            self.assertIn("awaiting_official_day", destination.read_text(encoding="utf-8-sig"))
            with self.assertRaises(ValueError):
                export_daily_paper_csv(database, database)
            price = Decimal("55400")
            save_official_day(database, OfficialDailyBar(TARGET, price, price + 450,
                price - 350, price, official_url(TARGET), "b" * 64,
                datetime(2026, 10, 6, tzinfo=timezone.utc)))
            scored = load_scored_references(database)[0]
            self.assertEqual(scored["status"], "scored_official_day")
            self.assertIsInstance(scored["both_extremes_contained"], bool)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE paper_daily_range_reference SET payload_sha256='bad'")
                connection.commit()
            with self.assertRaisesRegex(ValueError, "integrity"):
                load_scored_references(database)

    def test_target_day_is_not_scored_from_a_preclose_source(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            for bar in prior_bars():
                save_official_day(database, bar)
            record_next_reference(database, recorded_at=NOW)
            price = Decimal("55400")
            with self.assertRaisesRegex(ValueError, "before session close"):
                save_official_day(database, OfficialDailyBar(TARGET, price, price + 450,
                    price - 350, price, official_url(TARGET), "b" * 64,
                    datetime(2026, 10, 5, 5, tzinfo=timezone.utc)))
            self.assertEqual(load_scored_references(database)[0]["status"],
                             "awaiting_official_day")


if __name__ == "__main__":
    unittest.main()

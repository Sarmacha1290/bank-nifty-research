"""Official daily OHLC import preserves provenance and rejects malformed rows."""
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from banknifty.official_daily import (load_official_days, official_url,
                                      parse_official_csv, save_official_day,
                                      recent_completed_regular_days,
                                      export_official_days_csv,
                                      sync_recent_official_days,
                                      older_or_missing_regular_days,
                                      sync_older_official_days)


DAY = date(2026, 9, 30)
HEADER = b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value\n"
ROW = b"Nifty Bank,30-09-2026,54175.9,55135.5,54174.3,54633.05\n"
AT = datetime(2026, 10, 4, 5, 0, tzinfo=timezone.utc)


class OfficialDailyTests(unittest.TestCase):
    def test_validated_nse_row_archives_exact_prices_and_source_hash(self):
        bar = parse_official_csv(HEADER + ROW, DAY, official_url(DAY), AT)
        self.assertEqual(str(bar.range_points), "961.2")
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            self.assertEqual(save_official_day(database, bar), "inserted")
            self.assertEqual(save_official_day(database, bar), "duplicate")
            recovered = load_official_days(database)[0]
            self.assertEqual(recovered, bar)
            self.assertEqual(recovered.source_url, official_url(DAY))
            destination = Path(folder) / "official.csv"
            self.assertEqual(export_official_days_csv(database, destination), 1)
            self.assertIn(bar.sha256_hex, destination.read_text(encoding="utf-8-sig"))
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                save_official_day(database, replace(bar, high=bar.high + 1))

    def test_wrong_document_or_date_never_enters_archive(self):
        for content in (b"<html>blocked</html>", HEADER + ROW + ROW,
                        HEADER + ROW.replace(b"30-09-2026", b"01-10-2026"),
                        HEADER + ROW.replace(b"55135.5", b"50000")):
            with self.assertRaises(ValueError):
                parse_official_csv(content, DAY, official_url(DAY), AT)
        with self.assertRaises(ValueError):
            parse_official_csv(HEADER + ROW, DAY, "https://example.com/other.csv", AT)
        with self.assertRaises(ValueError):
            official_url(date(2026, 10, 4))
        with self.assertRaisesRegex(ValueError, "before session close"):
            parse_official_csv(HEADER + ROW, DAY, official_url(DAY),
                               datetime(2026, 9, 30, 5, 0, tzinfo=timezone.utc))

    def test_recent_batch_skips_weekend_and_exchange_holiday(self):
        days = recent_completed_regular_days(3, now=datetime(2026, 10, 4, 10, tzinfo=timezone.utc))
        self.assertEqual(days, (date(2026, 10, 1), date(2026, 9, 30), date(2026, 9, 29)))

    def test_sync_counts_existing_official_day_as_duplicate(self):
        bar = parse_official_csv(HEADER + ROW, DAY, official_url(DAY), AT)
        with TemporaryDirectory() as folder, \
                patch("banknifty.official_daily.recent_completed_regular_days", return_value=(DAY,)), \
                patch("banknifty.official_daily.fetch_official_day", return_value=bar):
            database = Path(folder) / "market.sqlite3"
            self.assertEqual(sync_recent_official_days(database, 1)["inserted"], 1)
            self.assertEqual(sync_recent_official_days(database, 1)["duplicates"], 1)

    def test_older_batch_picks_archive_gap_before_older_days_and_reports_failures(self):
        second_day = date(2026, 10, 1)
        other = parse_official_csv(HEADER + ROW.replace(b"30-09-2026", b"01-10-2026"),
                                   second_day, official_url(second_day), AT)
        first_day = date(2026, 9, 29)
        first = parse_official_csv(HEADER + ROW.replace(b"30-09-2026", b"29-09-2026"),
                                   first_day, official_url(first_day), AT)
        middle = parse_official_csv(HEADER + ROW, DAY, official_url(DAY), AT)
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            save_official_day(database, first)
            save_official_day(database, other)
            self.assertEqual(older_or_missing_regular_days(database, 2),
                             (DAY, date(2026, 9, 28)))
            def fake_fetch(day, **_):
                if day == DAY:
                    return middle
                raise OSError("offline")
            with patch("banknifty.official_daily.fetch_official_day", side_effect=fake_fetch), \
                    patch("banknifty.official_daily.time.sleep"):
                outcome = sync_older_official_days(database, 2)
            self.assertEqual(outcome["inserted"], 1)
            self.assertEqual(outcome["failed"], [("2026-09-28", "OSError")])
            self.assertEqual(older_or_missing_regular_days(database, 1), (date(2026, 9, 28),))


if __name__ == "__main__":
    unittest.main()

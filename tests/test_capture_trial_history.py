"""Capture review records preserve failures and reject inconsistent direct writes."""
from datetime import datetime, timedelta, timezone
from contextlib import closing
import csv
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from banknifty.capture_trial_history import (build_capture_trial_summary,
    export_capture_trial_csv, format_capture_trial_summary,
    load_capture_trial_summaries, save_capture_trial_summary)


START = datetime(2026, 10, 5, 3, 45, tzinfo=timezone.utc)


class CaptureTrialHistoryTests(unittest.TestCase):
    def test_completed_and_failed_attempts_remain_distinct(self):
        passing = build_capture_trial_summary(
            START, START + timedelta(minutes=5),
            {"status": "completed", "inserted": 100, "duplicates": 2, "rejected": 0},
            {"saved_updates": 100, "observed_minutes": 5, "accepted_minutes": 3,
             "too_few_samples": 1, "missing_minute_edge": 1, "internal_gap": 0,
             "exchange_time_ahead_updates": 0, "median_receipt_lag_seconds": 0.5,
             "max_receipt_lag_seconds": 2.0,
             "first_exchange_ist": "2026-10-05T09:15:00+05:30",
             "last_exchange_ist": "2026-10-05T09:19:59+05:30"}, None, accepted=True)
        failed = build_capture_trial_summary(
            START + timedelta(minutes=10), START + timedelta(minutes=11),
            {"status": "no_fresh_updates", "inserted": 0}, None, None, accepted=False)
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            save_capture_trial_summary(database, passing)
            save_capture_trial_summary(database, failed)
            rows = load_capture_trial_summaries(database)
            self.assertEqual([row["accepted_minimal_gate"] for row in rows], [True, False])
            self.assertIn("3/5 observed minutes accepted", format_capture_trial_summary(rows[0]))
            self.assertIn("needs review", format_capture_trial_summary(rows[-1]))
            self.assertIn("minute-quality review unavailable", format_capture_trial_summary(rows[-1]))
            self.assertNotIn("forecast accuracy: 100", str(rows))
            destination = Path(folder) / "capture-reviews.csv"
            self.assertEqual(export_capture_trial_csv(database, destination), 2)
            with destination.open(encoding="utf-8-sig", newline="") as stream:
                exported = list(csv.DictReader(stream))
            self.assertEqual(exported[0]["minimal_software_gate_passed"], "yes")
            self.assertEqual(exported[0]["accepted_index_minutes"], "3")
            self.assertEqual(exported[1]["minimal_software_gate_passed"], "no")
            self.assertEqual(exported[1]["accepted_index_minutes"], "")
            self.assertEqual(exported[1]["capture_status"], "no_fresh_updates")
            with self.assertRaisesRegex(ValueError, "overwrite"):
                export_capture_trial_csv(database, database)

    def test_full_minute_review_is_saved_and_legacy_review_stays_readable(self):
        trial = {"saved_updates": 12, "observed_minutes": 1,
                 "accepted_minutes": 1, "full_minutes": 2,
                 "full_observed_minutes": 1, "full_accepted_minutes": 1}
        current = build_capture_trial_summary(
            START + timedelta(seconds=23), START + timedelta(minutes=3, seconds=23),
            {"status": "completed", "inserted": 12}, trial, None, accepted=True)
        legacy = build_capture_trial_summary(
            START, START + timedelta(minutes=1),
            {"status": "completed", "inserted": 12},
            {"saved_updates": 12, "observed_minutes": 1, "accepted_minutes": 1},
            None, accepted=True)
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            save_capture_trial_summary(database, current)
            save_capture_trial_summary(database, legacy)
            rows = load_capture_trial_summaries(database)
            self.assertEqual(rows[0]["index_trial"]["full_minutes"], 2)
            self.assertNotIn("full_minutes", rows[1]["index_trial"])
            self.assertIn("1/2 complete recording-window minutes",
                          format_capture_trial_summary(rows[0]))
            destination = Path(folder) / "reviews.csv"
            export_capture_trial_csv(database, destination)
            with destination.open(encoding="utf-8-sig", newline="") as stream:
                exported = list(csv.DictReader(stream))
            self.assertEqual(exported[0]["accepted_complete_window_minutes"], "1")
            self.assertEqual(exported[1]["accepted_complete_window_minutes"], "")
            self.assertEqual(exported[1]["index_finished_at_utc"], "")
        with self.assertRaisesRegex(ValueError, "Inconsistent full-minute"):
            build_capture_trial_summary(
                START, START + timedelta(minutes=1),
                {"status": "completed", "inserted": 12},
                {**trial, "full_accepted_minutes": 2}, None, accepted=True)

    def test_new_index_end_is_validated_and_preserved_separately_from_quote_cleanup(self):
        index_end = START + timedelta(minutes=1)
        review_end = index_end + timedelta(seconds=20)
        trial = {"saved_updates": 12, "observed_minutes": 1,
                 "accepted_minutes": 1, "full_minutes": 1,
                 "full_observed_minutes": 1, "full_accepted_minutes": 1}
        summary = build_capture_trial_summary(
            START, review_end, {"status": "completed", "inserted": 12},
            trial, None, accepted=True, index_finished_at=index_end)
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            save_capture_trial_summary(database, summary)
            self.assertEqual(load_capture_trial_summaries(database)[0]
                             ["index_finished_at_utc"], index_end.isoformat())
            export = Path(folder) / "reviews.csv"
            export_capture_trial_csv(database, export)
            with export.open(encoding="utf-8-sig", newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["index_finished_at_utc"], index_end.isoformat())
            self.assertEqual(row["finished_at_utc"], review_end.isoformat())
        with self.assertRaisesRegex(ValueError, "Index recording end"):
            build_capture_trial_summary(START, review_end,
                {"status": "completed", "inserted": 12}, trial, None,
                accepted=True, index_finished_at=review_end + timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "complete-minute"):
            build_capture_trial_summary(START, review_end,
                {"status": "completed", "inserted": 12},
                {**trial, "full_accepted_minutes": 0}, None,
                accepted=True, index_finished_at=index_end)

    def test_invalid_success_and_secret_fields_never_create_archive(self):
        with self.assertRaises(ValueError):
            build_capture_trial_summary(START, START + timedelta(minutes=1),
                                        {"status": "completed", "inserted": 1},
                                        None, None, accepted=True)
        for result, trial in (
            ({"status": "capture_exception", "inserted": 12},
             {"saved_updates": 12, "observed_minutes": 1, "accepted_minutes": 1}),
            ({"status": "completed", "inserted": 11},
             {"saved_updates": 12, "observed_minutes": 1, "accepted_minutes": 1}),
            ({"status": "completed", "inserted": 12},
             {"saved_updates": 11, "observed_minutes": 1, "accepted_minutes": 1}),
            ({"status": "completed", "inserted": 12},
             {"saved_updates": 12, "accepted_minutes": 1}),
        ):
            with self.subTest(result=result, trial=trial), self.assertRaisesRegex(ValueError, "inconsistent"):
                build_capture_trial_summary(START, START + timedelta(minutes=1),
                                            result, trial, None, accepted=True)
        with self.assertRaisesRegex(ValueError, "bank quote"):
            build_capture_trial_summary(
                START, START + timedelta(minutes=1),
                {"status": "completed", "inserted": 12},
                {"saved_updates": 12, "observed_minutes": 1, "accepted_minutes": 1},
                {"status": "activity_error", "saved": 14, "failures": 1,
                 "summary": {"complete": False}},
                include_activity=True, accepted=True)
        summary = build_capture_trial_summary(
            START, START + timedelta(minutes=1),
            {"status": "no_fresh_updates", "inserted": 0}, None, None)
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            with self.assertRaisesRegex(ValueError, "No capture reviews"):
                export_capture_trial_csv(database, Path(folder) / "empty.csv")
            self.assertFalse((Path(folder) / "empty.csv").exists())
            with self.assertRaises(ValueError):
                save_capture_trial_summary(database, {**summary, "access_token": "secret"})
            self.assertFalse(database.exists())

    def test_hash_mismatch_blocks_review(self):
        summary = build_capture_trial_summary(
            START, START + timedelta(minutes=1),
            {"status": "no_fresh_updates", "inserted": 0}, None, None)
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            save_capture_trial_summary(database, summary)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE events SET payload='{}' WHERE event_type='capture_trial_summary'")
                connection.commit()
            with self.assertRaisesRegex(ValueError, "integrity"):
                load_capture_trial_summaries(database)

    def test_preparation_failure_does_not_display_unmeasured_zero(self):
        summary = build_capture_trial_summary(
            START, START + timedelta(seconds=2),
            {"status": "preparation_failed"}, None, None)
        shown = format_capture_trial_summary(summary)
        self.assertIn("new-update count unavailable", shown)
        self.assertIn("minute-quality review unavailable", shown)
        self.assertNotIn("0 new updates", shown)

    def test_archive_error_and_busy_attempt_remain_saved_and_exportable(self):
        summary = build_capture_trial_summary(
            START, START + timedelta(seconds=4),
            {"status": "archive_error", "inserted": 0, "duplicates": 0,
             "rejected": 0, "archive_busy_retries": 1,
             "equity_archive_busy_retries": 0,
             "depth_archive_busy_retries": 0}, None, None, accepted=False)
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            save_capture_trial_summary(database, summary)
            saved = load_capture_trial_summaries(database)[0]
            self.assertEqual(saved["capture_status"], "archive_error")
            self.assertIn("1 index archive-lock retries", format_capture_trial_summary(saved))
            destination = Path(folder) / "review.csv"
            export_capture_trial_csv(database, destination)
            with destination.open(encoding="utf-8-sig", newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["index_archive_busy_retries"], "1")
            self.assertEqual(row["capture_status"], "archive_error")
        with self.assertRaisesRegex(ValueError, "Invalid archive retry counter"):
            build_capture_trial_summary(START, START + timedelta(seconds=1),
                {"status": "archive_error", "archive_busy_retries": True}, None, None)

    def test_failed_optional_bank_quote_archive_has_a_durable_trial_status(self):
        summary = build_capture_trial_summary(
            START, START + timedelta(minutes=1),
            {"status": "completed", "inserted": 12},
            {"saved_updates": 12, "observed_minutes": 1, "accepted_minutes": 1},
            {"status": "archive_error", "saved": 0, "sweeps": 0, "failures": 0},
            include_activity=True, accepted=False)
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            save_capture_trial_summary(database, summary)
            saved = load_capture_trial_summaries(database)[0]
            self.assertEqual(saved["bank_quote_trial"]["status"], "archive_error")
            destination = Path(folder) / "review.csv"
            export_capture_trial_csv(database, destination)
            with destination.open(encoding="utf-8-sig", newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["bank_quote_status"], "archive_error")


if __name__ == "__main__":
    unittest.main()

"""One capture's diagnostics must exclude saved updates from earlier trials."""

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.feed import IndexTick
from banknifty.groww import IST
from banknifty.tick_history import save_index_tick
from banknifty.trial_report import (format_index_trial_note, summarize_index_trial,
                                    summarize_tick_timestamps)
from connect_groww import capture_completion_ok


class TrialReportTests(unittest.TestCase):
    def test_receipt_window_excludes_prior_run_and_explains_partial_minute(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            prior = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
            save_index_tick(database, IndexTick("NSE-BANKNIFTY", prior,
                                                prior + timedelta(seconds=1), 55000))
            current = prior + timedelta(minutes=1)
            for offset in range(0, 60, 5):
                at = current + timedelta(seconds=offset)
                save_index_tick(database, IndexTick("NSE-BANKNIFTY", at,
                                                    at + timedelta(seconds=2), 55001 + offset))
            report = summarize_index_trial(database, current, current + timedelta(minutes=1))
            self.assertEqual(report["saved_updates"], 12)
            self.assertEqual((report["observed_minutes"], report["accepted_minutes"]), (1, 1))
            self.assertEqual(report["max_receipt_lag_seconds"], 2)
            self.assertEqual(report["first_exchange_ist"], current.isoformat())
            self.assertIn("12 unique updates", format_index_trial_note(report))
            self.assertIn("First and last trial minutes may be partial", format_index_trial_note(report))

    def test_sparse_trial_reports_no_fabricated_coverage(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            start = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
            self.assertIsNone(summarize_index_trial(database, start, start + timedelta(minutes=1)))
            self.assertIn("no new index updates", format_index_trial_note(None))
            for offset in (15, 30):
                at = start + timedelta(seconds=offset)
                save_index_tick(database, IndexTick("NSE-BANKNIFTY", at,
                                                    at + timedelta(seconds=1), 55000 + offset))
            report = summarize_index_trial(database, start, start + timedelta(minutes=1))
            self.assertEqual((report["observed_minutes"], report["accepted_minutes"]),
                             (1, 0))
            self.assertEqual(report["too_few_samples"], 1)
            with self.assertRaisesRegex(ValueError, "precedes"):
                summarize_index_trial(database, start + timedelta(minutes=1), start)

    def test_late_backlog_does_not_pass_current_trial_minute_gate(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            stale_minute = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
            # These delays are below the feed parser's two-minute age limit.
            capture_start = stale_minute + timedelta(minutes=1, seconds=30)
            for offset in range(0, 60, 5):
                exchange_at = stale_minute + timedelta(seconds=offset)
                received_at = capture_start + timedelta(seconds=offset)
                save_index_tick(database, IndexTick(
                    "NSE-BANKNIFTY", exchange_at, received_at, 55000 + offset))
            report = summarize_index_trial(database, capture_start,
                                           capture_start + timedelta(minutes=1))
            self.assertEqual(report["saved_updates"], 12)
            self.assertEqual(report["timestamp_diagnostics"]["p95_receipt_age_seconds"], 90)
            self.assertEqual(report["timestamp_diagnostics"]["receipt_age_over_5s_updates"], 12)
            self.assertEqual(report["timestamp_diagnostics"]["receipt_age_over_15s_updates"], 12)
            self.assertEqual(report["observed_minutes"], 0)
            self.assertEqual(report["accepted_minutes"], 0)
            self.assertFalse(capture_completion_ok(
                {"status": "completed", "inserted": 12}, None,
                trial_report=report))
            self.assertIn("only exchange updates within this recording window",
                          format_index_trial_note(report))

    def test_partial_edges_and_missing_full_minute_are_visible(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            start = datetime(2026, 10, 5, 9, 15, 5, tzinfo=IST)
            complete = datetime(2026, 10, 5, 9, 16, tzinfo=IST)
            edge = start.replace(second=0)
            for offset in (*range(5, 60, 5), 59):
                at = edge + timedelta(seconds=offset)
                save_index_tick(database, IndexTick("NSE-BANKNIFTY", at,
                                                    at + timedelta(seconds=1), 54900 + offset))
            for offset in range(0, 60, 5):
                at = complete + timedelta(seconds=offset)
                save_index_tick(database, IndexTick("NSE-BANKNIFTY", at,
                                                    at + timedelta(seconds=1), 55000 + offset))
            report = summarize_index_trial(database, start, start + timedelta(minutes=3))
            self.assertEqual((report["full_minutes"], report["full_observed_minutes"],
                              report["full_accepted_minutes"]), (2, 1, 1))
            self.assertEqual(report["accepted_minutes"], 2)
            self.assertIn("complete recording-window minutes 1/2 accepted",
                          format_index_trial_note(report))

    def test_good_samples_in_only_a_partial_minute_do_not_pass_capture_gate(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            minute = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
            for offset in (*range(5, 60, 5), 59):
                at = minute + timedelta(seconds=offset)
                save_index_tick(database, IndexTick("NSE-BANKNIFTY", at, at,
                                                    55000 + offset))
            report = summarize_index_trial(database, minute + timedelta(seconds=5),
                                           minute + timedelta(seconds=59, milliseconds=500))
            self.assertEqual(report["saved_updates"], 12)
            self.assertEqual(report["accepted_minutes"], 1)
            self.assertEqual(report["full_accepted_minutes"], 0)
            self.assertFalse(capture_completion_ok(
                {"status": "completed", "inserted": 12}, None, trial_report=report))


    def test_timestamp_age_boundaries_and_future_offsets_are_explicit(self):
        start = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
        received = start + timedelta(seconds=20)
        ages = (-5.001, -5, 0, 5, 15, 15.001)
        ticks = [IndexTick("NSE-BANKNIFTY", received - timedelta(seconds=age),
                           received, 55000) for age in ages]
        report = summarize_tick_timestamps(ticks, start, start + timedelta(minutes=1))
        self.assertEqual(report["p95_receipt_age_seconds"], 15.001)
        self.assertEqual(report["receipt_age_over_5s_updates"], 2)
        self.assertEqual(report["receipt_age_over_15s_updates"], 1)
        self.assertEqual(report["future_exchange_timestamp_updates"], 2)
        self.assertEqual(report["exchange_more_than_5s_ahead_updates"], 1)

    def test_timestamp_p95_uses_nearest_rank_not_maximum(self):
        start = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
        received = start + timedelta(seconds=30)
        ticks = [IndexTick("NSE-BANKNIFTY", received - timedelta(seconds=age),
                           received, 55000) for age in range(20)]
        report = summarize_tick_timestamps(ticks, start, start + timedelta(minutes=1))
        self.assertEqual(report["p95_receipt_age_seconds"], 18)

    def test_receipt_gaps_include_edges_and_ignore_unchanged_prices(self):
        start = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
        cases = (
            ((20, 25, 30, 35), 60, 20, 25, 25, 2, 2),
            ((5, 20, 35), 50, 5, 15, 15, 3, 0),
            (tuple(range(0, 61, 5)), 60, 0, 0, 5, 0, 0),
        )
        for offsets, duration, first_gap, last_gap, maximum, warnings, stale in cases:
            with self.subTest(offsets=offsets):
                ticks = [IndexTick("NSE-BANKNIFTY", start + timedelta(seconds=offset),
                                   start + timedelta(seconds=offset), 55000) for offset in offsets]
                report = summarize_tick_timestamps(ticks, start, start + timedelta(seconds=duration))
                self.assertEqual(report["start_silence_seconds"], first_gap)
                self.assertEqual(report["end_silence_seconds"], last_gap)
                self.assertEqual(report["max_receipt_gap_seconds"], maximum)
                self.assertEqual(report["warning_gaps_over_5s"], warnings)
                self.assertEqual(report["stale_gaps_over_15s"], stale)
                self.assertEqual(report["receipt_age_over_5s_updates"], 0)

    def test_archive_sequence_regressions_are_reported_without_changing_exchange_order(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            start = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
            for exchange_offset, receipt_offset in ((1, 10), (3, 8), (2, 12)):
                save_index_tick(database, IndexTick("NSE-BANKNIFTY",
                    start + timedelta(seconds=exchange_offset),
                    start + timedelta(seconds=receipt_offset), 55000))
            before = database.read_bytes()
            report = summarize_index_trial(database, start, start + timedelta(seconds=30))
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(report["first_exchange_ist"], (start + timedelta(seconds=1)).isoformat())
            self.assertEqual(report["last_exchange_ist"], (start + timedelta(seconds=3)).isoformat())
            diagnostics = report["timestamp_diagnostics"]
            self.assertEqual(diagnostics["receipt_clock_regressions"], 1)
            self.assertEqual(diagnostics["out_of_order_exchange_updates"], 1)
            self.assertTrue(any("no monotonic" in item for item in diagnostics["limitations"]))
            self.assertTrue(any("Duplicate" in item for item in diagnostics["limitations"]))

    def test_empty_timestamp_window_is_one_full_window_gap(self):
        start = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
        outside = IndexTick("NSE-BANKNIFTY", start - timedelta(seconds=1),
                            start - timedelta(seconds=1), 55000)
        for ticks in ([], [outside]):
            with self.subTest(ticks=ticks):
                report = summarize_tick_timestamps(ticks, start, start + timedelta(seconds=16))
                self.assertEqual(report["saved_updates_in_receipt_window"], 0)
                self.assertIsNone(report["p95_receipt_age_seconds"])
                self.assertEqual(report["start_silence_seconds"], 16)
                self.assertEqual(report["end_silence_seconds"], 16)
                self.assertEqual(report["max_receipt_gap_seconds"], 16)
                self.assertEqual(report["warning_gaps_over_5s"], 1)
                self.assertEqual(report["stale_gaps_over_15s"], 1)

if __name__ == "__main__":
    unittest.main()

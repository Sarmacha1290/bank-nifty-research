"""Observed feed bars must fail closed on sparse data and session gaps."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.feed import IndexTick
from banknifty.feed_quality import SAMPLED_SOURCE, inspect_sampled_minutes, missing_minutes_between_observations
from banknifty.feed_technical import assess_recorded_feed
from banknifty.groww import IST
from banknifty.tick_history import export_index_ticks_csv, load_index_ticks, save_index_tick


START = datetime(2026, 10, 5, 9, 15, tzinfo=IST)


def minute_updates(start, *, offsets=range(0, 60, 5)):
    return [IndexTick("NSE-BANKNIFTY", start + timedelta(seconds=offset),
                      start + timedelta(seconds=offset + 1), 55000 + offset / 10)
            for offset in offsets]


class FeedQualityTests(unittest.TestCase):
    def test_quality_rejects_sparse_and_edge_gapped_minutes(self):
        ticks = minute_updates(START) + minute_updates(START + timedelta(minutes=1), offsets=(0, 55))
        ticks += minute_updates(START + timedelta(minutes=2), offsets=range(15, 60, 3))
        ticks += minute_updates(START + timedelta(minutes=3),
                                offsets=(0, 1, 2, 3, 4, 5, 6, 7, 8, 30, 31, 32, 33, 55, 56, 57, 58, 59))
        quality, bars = inspect_sampled_minutes(ticks)
        self.assertEqual([row.status for row in quality],
                         ["accepted", "too_few_samples", "missing_minute_edge", "internal_gap"])
        self.assertEqual((bars[0].open, bars[0].high, bars[0].low, bars[0].close),
                         (55000, 55005.5, 55000, 55005.5))
        self.assertEqual(bars[0].source, SAMPLED_SOURCE)
        self.assertIsNone(bars[0].volume)
        holiday = datetime(2026, 10, 2, 9, 15, tzinfo=IST)
        self.assertEqual(inspect_sampled_minutes(minute_updates(holiday)), ([], []))

    def test_read_back_and_no_fabricated_missing_minute(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "history.sqlite3"
            for tick in minute_updates(START) + minute_updates(START + timedelta(minutes=2)):
                save_index_tick(database, tick)
            loaded = load_index_ticks(database, start=START, end=START + timedelta(minutes=3))
            quality, bars = inspect_sampled_minutes(loaded)
            self.assertEqual(len(loaded), 24)
            self.assertEqual(len(bars), 2)
            self.assertEqual([row.start.minute for row in quality], [15, 17])
            self.assertEqual(missing_minutes_between_observations(quality), 1)
            destination = Path(folder) / "updates.csv"
            self.assertEqual(export_index_ticks_csv(database, destination), 24)
            exported = destination.read_text(encoding="utf-8-sig")
            self.assertIn("exchange_at_utc,received_at_utc", exported)
            self.assertIn("NSE-BANKNIFTY", exported)
            with self.assertRaisesRegex(ValueError, "overwrite"):
                export_index_ticks_csv(database, database)
            with self.assertRaisesRegex(ValueError, "61 accepted"):
                assess_recorded_feed(loaded, generated_at=START + timedelta(minutes=3))

    def test_technical_rules_require_continuous_recent_minutes(self):
        updates = [tick for minute in range(61)
                   for tick in minute_updates(START + timedelta(minutes=minute))]
        generated = START + timedelta(minutes=61, seconds=20)
        quality, assessments = assess_recorded_feed(updates, generated_at=generated)
        self.assertEqual(len(quality), 61)
        self.assertEqual([item.horizon_minutes for item in assessments], [15, 30, 60])
        self.assertTrue(all(item.source == SAMPLED_SOURCE for item in assessments))
        with self.assertRaisesRegex(ValueError, "older than 120"):
            assess_recorded_feed(updates, generated_at=generated + timedelta(minutes=3))
        missing = [tick for tick in updates if tick.exchange_at.astimezone(IST).minute != 30]
        with self.assertRaisesRegex(ValueError, "61 accepted"):
            assess_recorded_feed(missing, generated_at=generated)

    def test_horizons_crossing_regular_close_are_unavailable(self):
        opening = datetime(2026, 10, 5, 14, 5, tzinfo=IST)
        updates = [tick for minute in range(61)
                   for tick in minute_updates(opening + timedelta(minutes=minute))]
        _, assessments = assess_recorded_feed(
            updates, generated_at=opening + timedelta(minutes=61, seconds=20))
        self.assertEqual([item.bias == "unavailable" for item in assessments], [False, True, True])
        self.assertIsNone(assessments[2].score)


if __name__ == "__main__":
    unittest.main()

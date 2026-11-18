"""Transient writer contention may recover; other failures must remain failures."""

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import sqlite3

from banknifty.archive_retry import save_with_busy_retry
from banknifty.groww import IST
from banknifty.tick_history import count_index_ticks, save_index_tick
from collect_groww import collect


NOW = datetime(2026, 10, 5, 10, 0, tzinfo=IST)
INSTRUMENT = {"exchange": "NSE", "segment": "CASH", "exchange_token": "BANKNIFTY",
              "groww_symbol": "NSE-BANKNIFTY"}


class Feed:
    def subscribe_index_value(self, instruments):
        self.subscribed = True

    def get_index_value(self):
        return {"NSE": {"CASH": {"BANKNIFTY": {
            "tsInMillis": int(NOW.timestamp() * 1000), "value": 55000.0}}}}

    def unsubscribe_index_value(self, instruments):
        self.unsubscribed = True


class ArchiveRetryTests(TestCase):
    def test_only_busy_or_locked_error_gets_one_retry(self):
        pauses = []
        calls = []

        def eventually_writes():
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return "saved"

        self.assertEqual(save_with_busy_retry(eventually_writes,
                         pause=pauses.append), ("saved", 1))
        self.assertEqual(pauses, [0.2])
        self.assertEqual(len(calls), 2)

        for error in (sqlite3.OperationalError("disk I/O error"), ValueError("conflict")):
            with self.subTest(error=error), self.assertRaises(type(error)):
                save_with_busy_retry(lambda: (_ for _ in ()).throw(error), pause=pauses.append)
        self.assertEqual(pauses, [0.2])

    def test_collector_recovers_one_lock_and_keeps_original_receipt(self):
        original = save_index_tick
        calls = []

        def briefly_locked(database, tick):
            calls.append(tick)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(database, tick)

        with TemporaryDirectory() as folder, patch("collect_groww.save_index_tick", briefly_locked):
            database = Path(folder) / "market.sqlite3"
            elapsed = [0.0]
            result = collect(Feed(), INSTRUMENT, database, duration_seconds=1,
                             now=lambda: NOW + timedelta(seconds=elapsed[0]),
                             sleep=lambda amount: elapsed.__setitem__(0, elapsed[0] + amount),
                             monotonic=lambda: elapsed[0])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["archive_busy_retries"], 1)
            self.assertEqual(result["inserted"], 1)
            self.assertEqual(count_index_ticks(database), 1)
            self.assertIs(calls[0], calls[1])

    def test_persistent_lock_stops_capture_without_pretending_success(self):
        calls = []

        def locked(database, tick):
            calls.append(tick)
            raise sqlite3.OperationalError("database is locked")

        with TemporaryDirectory() as folder, patch("collect_groww.save_index_tick", locked):
            database = Path(folder) / "market.sqlite3"
            elapsed = [0.0]
            feed = Feed()
            result = collect(feed, INSTRUMENT, database, duration_seconds=1,
                             now=lambda: NOW + timedelta(seconds=elapsed[0]),
                             sleep=lambda amount: elapsed.__setitem__(0, elapsed[0] + amount),
                             monotonic=lambda: elapsed[0])
            self.assertEqual(result["status"], "archive_error")
            self.assertEqual(result["archive_busy_retries"], 1)
            self.assertEqual(result["inserted"], 0)
            self.assertEqual(len(calls), 2)
            self.assertTrue(feed.unsubscribed)
            self.assertEqual(count_index_ticks(database), 0)

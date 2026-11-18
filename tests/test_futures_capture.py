"""Repeated futures quotes remain read-only and keep stale trade times."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.futures import FutureInstrument
from banknifty.futures_capture import capture_future_quotes
from banknifty.futures_history import load_future_snapshots
from banknifty.groww import DataAccessError


START = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
INSTRUMENT = FutureInstrument("BANKNIFTY26OCTFUT", "NSE-BANKNIFTY-27Oct26-FUT",
                              "48699", date(2026, 10, 27))


class Clock:
    value = 0.0

    def now(self):
        return self.value

    def pause(self, seconds):
        self.value += seconds

    def utc(self):
        return START + timedelta(seconds=self.value)


class Client:
    def __init__(self, *, fail_at=None):
        self.calls = 0
        self.fail_at = fail_at

    def banknifty_future_quote(self, symbol):
        self.calls += 1
        if self.calls == self.fail_at:
            raise DataAccessError("Groww request limit reached")
        return {"status": "SUCCESS", "payload": {
            "last_price": 55000 + self.calls,
            "last_trade_time": int((START - timedelta(seconds=5)).timestamp() * 1000),
            "open_interest": 1000 + self.calls, "volume": 20 + self.calls}}


class FuturesCaptureTests(unittest.TestCase):
    def test_three_quotes_keep_receipt_and_last_trade_times_separate(self):
        clock, client = Clock(), Client()
        with TemporaryDirectory() as folder:
            result = capture_future_quotes(client, INSTRUMENT,
                Path(folder) / "market.sqlite3", duration_seconds=610,
                clock=clock.now, pause=clock.pause, now_utc=clock.utc)
            rows = load_future_snapshots(Path(folder) / "market.sqlite3")
        self.assertEqual((result["status"], result["saved"], client.calls),
                         ("completed", 3, 3))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["received_at_utc"],
                         (START + timedelta(seconds=300)).isoformat())
        self.assertEqual(len({row["last_trade_at_utc"] for row in rows}), 1)

    def test_provider_error_stops_and_preserves_first_quote(self):
        clock, client = Clock(), Client(fail_at=2)
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            result = capture_future_quotes(client, INSTRUMENT, database,
                duration_seconds=900, clock=clock.now, pause=clock.pause,
                now_utc=clock.utc)
            self.assertEqual(result["status"], "access_stopped")
            self.assertEqual(result["saved"], 1)
            self.assertEqual(len(load_future_snapshots(database)), 1)

    def test_manual_stop_ends_without_another_request(self):
        clock, client = Clock(), Client()
        with TemporaryDirectory() as folder:
            result = capture_future_quotes(client, INSTRUMENT,
                Path(folder) / "market.sqlite3", duration_seconds=900,
                stop_requested=lambda: clock.value >= 400,
                clock=clock.now, pause=clock.pause, now_utc=clock.utc)
        self.assertEqual((result["status"], result["saved"], client.calls),
                         ("stopped", 2, 2))


if __name__ == "__main__":
    unittest.main()

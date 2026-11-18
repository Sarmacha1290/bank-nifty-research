"""Repeated option collection is bounded, read-only and archives valid rows."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.groww import DataAccessError
from banknifty.option_capture import capture_option_chains
from banknifty.option_history import load_option_chain_snapshots


EXPIRY = date(2026, 10, 27)
START = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)


class FakeClock:
    value = 0.0

    def now(self):
        return self.value

    def pause(self, seconds):
        self.value += seconds

    def utc(self):
        return START + timedelta(seconds=self.value)


class FakeClient:
    def __init__(self, clock, *, fail_at=None, slow_seconds=0):
        self.clock = clock
        self.fail_at = fail_at
        self.slow_seconds = slow_seconds
        self.calls = 0

    def banknifty_option_chain(self, expiry):
        self.calls += 1
        if self.calls == self.fail_at:
            raise DataAccessError("Groww request limit reached")
        self.clock.value += self.slow_seconds
        return {"status": "SUCCESS", "payload": {"underlying_ltp": 54000,
            "strikes": {"54000": {
                "CE": {"trading_symbol": "BANKNIFTYEXP54000CE",
                       "open_interest": 100 + self.calls, "volume": 20, "ltp": 200},
                "PE": {"trading_symbol": "BANKNIFTYEXP54000PE",
                       "open_interest": 120, "volume": 30, "ltp": 180}}}}}


class OptionCaptureTests(unittest.TestCase):
    def test_three_five_minute_snapshots_are_saved_without_a_live_token(self):
        clock = FakeClock()
        client = FakeClient(clock)
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            result = capture_option_chains(client, EXPIRY, database,
                duration_seconds=610, interval_seconds=300,
                clock=clock.now, pause=clock.pause, now_utc=clock.utc)
            saved = load_option_chain_snapshots(database)
        self.assertEqual((result["status"], result["saved"], client.calls),
                         ("completed", 3, 3))
        self.assertEqual(len(saved), 3)
        self.assertEqual([row["received_at_utc"] for row in saved],
                         [(START + timedelta(seconds=offset)).isoformat()
                          for offset in (0, 300, 600)])

    def test_stop_request_preserves_partial_archive(self):
        clock = FakeClock()
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            result = capture_option_chains(FakeClient(clock), EXPIRY, database,
                duration_seconds=900, interval_seconds=300,
                stop_requested=lambda: clock.value >= 400,
                clock=clock.now, pause=clock.pause, now_utc=clock.utc)
            self.assertEqual(result["status"], "stopped")
            self.assertEqual(result["saved"], 2)
            self.assertEqual(len(load_option_chain_snapshots(database)), 2)

    def test_provider_failure_stops_without_retries_or_invented_rows(self):
        clock = FakeClock()
        client = FakeClient(clock, fail_at=2)
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            result = capture_option_chains(client, EXPIRY, database,
                duration_seconds=900, interval_seconds=300,
                clock=clock.now, pause=clock.pause, now_utc=clock.utc)
            self.assertEqual(result["status"], "access_stopped")
            self.assertEqual((client.calls, result["saved"]), (2, 1))
            self.assertEqual(len(load_option_chain_snapshots(database)), 1)

    def test_slow_request_does_not_trigger_catch_up_burst(self):
        clock = FakeClock()
        client = FakeClient(clock, slow_seconds=360)
        with TemporaryDirectory() as folder:
            result = capture_option_chains(client, EXPIRY, Path(folder) / "market.sqlite3",
                duration_seconds=900, interval_seconds=300,
                clock=clock.now, pause=clock.pause, now_utc=clock.utc)
        self.assertEqual((result["saved"], client.calls), (2, 2))


if __name__ == "__main__":
    unittest.main()

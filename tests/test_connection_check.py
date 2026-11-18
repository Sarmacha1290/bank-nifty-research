"""Credential-free tests for the read-only connection-check path."""

from contextlib import closing
import json
from pathlib import Path
import queue
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from connect_groww import ConnectionWindow, save_safe_feed_status


class ConnectionCheckTests(unittest.TestCase):
    def test_feed_status_contains_only_approved_stage(self):
        with TemporaryDirectory() as folder:
            save_safe_feed_status(folder, "failed", "secure live-feed setup")
            saved = json.loads((Path(folder) / "live-feed-status.json")
                               .read_text(encoding="utf-8"))
            self.assertEqual(saved["stage"], "secure live-feed setup")
            self.assertEqual(saved["outcome"], "failed")
            with self.assertRaises(ValueError):
                save_safe_feed_status(folder, "failed", "SECRET_TEST_TOKEN")
            self.assertNotIn("SECRET_TEST_TOKEN", (Path(folder) / "live-feed-status.json")
                             .read_text(encoding="utf-8"))

    def _window(self, folder):
        # check_access only needs these two attributes; no Tk window or token
        # is needed to test its status and archive behavior.
        return SimpleNamespace(data_dir=Path(folder), messages=queue.Queue())

    def test_unexpected_response_reports_stage_without_secret(self):
        with TemporaryDirectory() as folder:
            window = self._window(folder)
            with patch("connect_groww.GrowwDataClient") as client, \
                 patch("connect_groww.fetch_banknifty_instrument",
                       return_value={"groww_symbol": "NSE-BANKNIFTY"}):
                client.return_value.index_snapshot.side_effect = RuntimeError("SECRET_TEST_TOKEN")
                ConnectionWindow.check_access(window, "SECRET_TEST_TOKEN")
            succeeded, message = window.messages.get_nowait()
            self.assertFalse(succeeded)
            self.assertIn("Groww price response", message)
            self.assertNotIn("SECRET_TEST_TOKEN", message)
            status = json.loads((Path(folder) / "connection-status.json")
                                .read_text(encoding="utf-8"))
            self.assertEqual(status["reason"], "Connection check failed during Groww price response.")
            self.assertNotIn("SECRET_TEST_TOKEN", str(status))

    def test_success_saves_only_market_snapshot_and_status(self):
        snapshot = {"source": "Groww", "symbol": "NSE-BANKNIFTY",
                    "received_at": "2026-10-05T04:00:00+00:00", "value": 55000.0,
                    "exchange_timestamp": None, "freshness": "unverified"}
        with TemporaryDirectory() as folder:
            window = self._window(folder)
            with patch("connect_groww.GrowwDataClient") as client, \
                 patch("connect_groww.fetch_banknifty_instrument",
                       return_value={"groww_symbol": "NSE-BANKNIFTY"}):
                client.return_value.index_snapshot.return_value = snapshot
                ConnectionWindow.check_access(window, "SECRET_TEST_TOKEN")
            succeeded, message = window.messages.get_nowait()
            self.assertTrue(succeeded, message)
            status = json.loads((Path(folder) / "connection-status.json")
                                .read_text(encoding="utf-8"))
            self.assertTrue(status["connected"])
            self.assertNotIn("SECRET_TEST_TOKEN", str(status))
            with closing(sqlite3.connect(Path(folder) / "market-data.sqlite3")) as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type='connection_snapshot'")
                    .fetchone()[0], 1)

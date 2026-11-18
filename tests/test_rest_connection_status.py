"""Failure paths must retain truthful counts and release the local connection UI."""
from contextlib import closing
import json
from pathlib import Path
import queue
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from connect_groww import ConnectionWindow
from banknifty.groww import DataAccessError
from banknifty.rest_sampler import save_rest_observation, summarize_rest_observations
from banknifty.rest_status import new_rest_status, load_rest_status, save_rest_status, rest_status_note
from banknifty.training_readiness import assess_training_readiness


SECRET = "SECRET_TEST_TOKEN_DO_NOT_SAVE"
SNAPSHOT = {"source": "Groww", "symbol": "NSE-BANKNIFTY", "value": 55000.,
            "received_at": "2026-10-19T04:00:00+00:00", "exchange_timestamp": None,
            "freshness": "unverified"}


class RestConnectionStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.window = SimpleNamespace(data_dir=self.folder, messages=queue.Queue(), stop_event=Event())

    def terminal(self):
        messages = []
        while not self.window.messages.empty():
            messages.append(self.window.messages.get_nowait())
        terminal = [item for item in messages if item[0] != "progress"]
        self.assertEqual(len(terminal), 1)
        self.assertNotIn(SECRET, str(messages))
        for path in self.folder.glob("*.json"):
            self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))
        db = self.folder / "market-data.sqlite3"
        if db.is_file():
            with closing(sqlite3.connect(db)) as con:
                if con.execute("SELECT name FROM sqlite_master WHERE name='events'").fetchone():
                    self.assertNotIn(SECRET, str(con.execute("SELECT payload FROM events").fetchall()))
        return terminal[0]

    def saved_sample(self, client, instrument, database, **kwargs):
        stamp = save_rest_observation(database, SNAPSHOT)
        kwargs["on_sample"](1, SNAPSHOT["value"], stamp)
        return {"status": "completed", "saved": 1}

    def test_catalogue_failure_replaces_old_success_and_saves_stage(self):
        old = new_rest_status(5)
        old.update(outcome="completed", stage="finished", saved=10)
        save_rest_status(self.folder, old)
        with patch("connect_groww.fetch_banknifty_instrument", side_effect=DataAccessError(SECRET)), \
             patch("connect_groww.GrowwDataClient") as client:
            ConnectionWindow.read_rest(self.window, SECRET, 5)
        client.assert_not_called()
        ok, message = self.terminal()
        status = load_rest_status(self.folder)
        self.assertFalse(ok)
        self.assertNotEqual(status["attempt_id"], old["attempt_id"])
        self.assertEqual((status["outcome"], status["stage"], status["saved"]),
                         ("preparation_failed", "instrument catalogue", 0))
        self.assertTrue(status["history_saved"])
        with closing(sqlite3.connect(self.folder / "market-data.sqlite3")) as con:
            payload = json.loads(con.execute("SELECT payload FROM events").fetchone()[0])
        self.assertTrue(payload["history_saved"])
        self.assertIn("diagnostic saved", message)
        self.assertEqual(summarize_rest_observations(self.folder / "market-data.sqlite3")["count"], 0)

    def test_client_creation_failure_has_no_price_request(self):
        with patch("connect_groww.fetch_banknifty_instrument", return_value={}), \
             patch("connect_groww.GrowwDataClient", side_effect=ValueError(SECRET)), \
             patch("connect_groww.sample_rest_prices") as sampler:
            ConnectionWindow.read_rest(self.window, SECRET, 1)
        sampler.assert_not_called()
        self.assertFalse(self.terminal()[0])
        self.assertEqual(load_rest_status(self.folder)["stage"], "client creation")

    def test_stop_during_catalogue_prevents_client_creation(self):
        def resolve():
            self.window.stop_event.set()
            return {}
        with patch("connect_groww.fetch_banknifty_instrument", side_effect=resolve), \
             patch("connect_groww.GrowwDataClient") as client:
            ConnectionWindow.read_rest(self.window, SECRET, 1)
        client.assert_not_called()
        self.terminal()
        self.assertEqual(load_rest_status(self.folder)["outcome"], "stopped")

    def test_unexpected_failure_after_commit_keeps_confirmed_count(self):
        def sample(*args, **kwargs):
            self.saved_sample(*args, **kwargs)
            raise RuntimeError(SECRET)
        with patch("connect_groww.fetch_banknifty_instrument", return_value={}), \
             patch("connect_groww.GrowwDataClient") as client, \
             patch("connect_groww.sample_rest_prices", side_effect=sample):
            ConnectionWindow.read_rest(self.window, SECRET, 1)
        client.return_value.close.assert_called_once()
        ok, message = self.terminal()
        self.assertFalse(ok)
        self.assertIn("1 receipt-time observations confirmed saved", message)
        status = load_rest_status(self.folder)
        self.assertEqual((status["outcome"], status["saved"]), ("unexpected_error", 1))
        self.assertEqual(summarize_rest_observations(self.folder / "market-data.sqlite3")["count"], 1)
        self.assertFalse(assess_training_readiness(self.folder / "market-data.sqlite3")["intraday_model_ready"])

    def test_event_write_failure_does_not_relabel_completed_capture(self):
        with patch("connect_groww.fetch_banknifty_instrument", return_value={}), \
             patch("connect_groww.GrowwDataClient"), \
             patch("connect_groww.sample_rest_prices", side_effect=self.saved_sample), \
             patch("connect_groww.record_event", side_effect=sqlite3.OperationalError(SECRET)):
            ConnectionWindow.read_rest(self.window, SECRET, 1)
        ok, message = self.terminal()
        self.assertFalse(ok)  # Review did not fully persist, even though sampling completed.
        self.assertIn("REST sampling completed", message)
        self.assertIn("Attempt history unavailable", message)
        status = load_rest_status(self.folder)
        self.assertEqual((status["outcome"], status["saved"], status["history_saved"]), ("completed", 1, False))
        self.assertIn("Attempt history was not saved", rest_status_note(self.folder))

    def test_status_write_failure_still_sends_one_terminal_message(self):
        with patch("connect_groww.fetch_banknifty_instrument", return_value={}), \
             patch("connect_groww.GrowwDataClient"), \
             patch("connect_groww.sample_rest_prices", side_effect=self.saved_sample), \
             patch("connect_groww.save_rest_status", side_effect=PermissionError(SECRET)):
            ConnectionWindow.read_rest(self.window, SECRET, 1)
        ok, message = self.terminal()
        self.assertFalse(ok)
        self.assertIn("REST sampling completed", message)
        self.assertIn("Latest diagnostic unavailable; attempt history saved", message)

    def test_both_diagnostic_writes_fail_without_hanging_ui(self):
        with patch("connect_groww.fetch_banknifty_instrument", side_effect=DataAccessError(SECRET)), \
             patch("connect_groww.save_rest_status", side_effect=PermissionError(SECRET)), \
             patch("connect_groww.record_event", side_effect=PermissionError(SECRET)):
            ConnectionWindow.read_rest(self.window, SECRET, 1)
        self.assertIn("older saved status may be stale", self.terminal()[1])

    def test_cleanup_failure_keeps_capture_count_and_does_not_hang(self):
        with patch("connect_groww.fetch_banknifty_instrument", return_value={}), \
             patch("connect_groww.GrowwDataClient") as client, \
             patch("connect_groww.sample_rest_prices", side_effect=self.saved_sample):
            client.return_value.close.side_effect = RuntimeError(SECRET)
            ConnectionWindow.read_rest(self.window, SECRET, 1)
        ok, message = self.terminal()
        self.assertFalse(ok)
        self.assertIn("Client cleanup failed", message)
        status = load_rest_status(self.folder)
        self.assertFalse(status["cleanup_ok"])
        self.assertEqual(status["saved"], 1)
        self.assertIn("Client cleanup failed", rest_status_note(self.folder))

    def test_check_access_cannot_hang_when_error_status_is_unwritable(self):
        blocker = self.folder / "not-a-folder"
        blocker.write_text("fixture", encoding="utf-8")
        self.window.data_dir = blocker
        with patch("connect_groww.GrowwDataClient") as client:
            client.return_value.index_snapshot.side_effect = DataAccessError(SECRET)
            with patch("connect_groww.fetch_banknifty_instrument", return_value={}):
                ConnectionWindow.check_access(self.window, SECRET)
        ok, message = self.terminal()
        self.assertFalse(ok)
        self.assertIn("Local status could not be saved", message)
        client.return_value.close.assert_called_once()

    def test_successful_access_with_failed_event_is_local_failure(self):
        with patch("connect_groww.GrowwDataClient") as client, \
             patch("connect_groww.fetch_banknifty_instrument", return_value={}), \
             patch("connect_groww.record_event", side_effect=PermissionError(SECRET)):
            client.return_value.index_snapshot.return_value = SNAPSHOT
            ConnectionWindow.check_access(self.window, SECRET)
        self.assertIn("A price response arrived", self.terminal()[1])
        status = json.loads((self.folder / "connection-status.json").read_text())
        self.assertTrue(status["price_response_received"])

    def test_saved_running_diagnostic_is_not_a_current_connection_claim(self):
        save_rest_status(self.folder, new_rest_status(5))
        self.assertIn("completion unconfirmed", rest_status_note(self.folder))
        self.assertIn("not a current connection", rest_status_note(self.folder))

    def test_reader_rejects_untrusted_extra_fields_duplicate_keys_and_big_files(self):
        for raw in (json.dumps({**new_rest_status(5), "message": SECRET}),
                    '{"version":1,"version":1}', 'x' * 4097,
                    json.dumps({**new_rest_status(5), "outcome": [SECRET]})):
            with self.subTest(raw_length=len(raw)):
                (self.folder / "rest-sampling-status.json").write_text(raw, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_rest_status(self.folder)
                self.assertNotIn(SECRET, rest_status_note(self.folder))

    def test_writer_rejects_secrets_in_stages_and_negative_counts(self):
        for change in ({"stage": SECRET}, {"saved": -1}, {"saved": True}, {"requested_minutes": True}):
            with self.assertRaises(ValueError):
                save_rest_status(self.folder, {**new_rest_status(5), **change})
        self.assertFalse((self.folder / "rest-sampling-status.json").exists())

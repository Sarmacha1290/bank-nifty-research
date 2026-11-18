"""Combined desktop capture reports quote outcomes without a network call."""
from contextlib import ExitStack, nullcontext
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import queue
import sqlite3
import sys
import tkinter as tk
import time
import unittest
from unittest.mock import patch

from connect_groww import (ConnectionWindow, capture_completion_ok,
                           capture_quality_note, capture_reconciliation_note)
from banknifty.futures_history import load_future_snapshots
from banknifty.capture_trial_history import load_capture_trial_summaries
from banknifty.groww_feed_security import GrowwFeedSetupError


class CombinedConnectionTests(unittest.TestCase):
    def test_completion_requires_all_requested_streams_and_real_saved_index_data(self):
        result = {"status": "completed", "inserted": 12,
                  "equity_status": "completed", "depth_status": "completed"}
        trial = {"accepted_minutes": 1, "full_accepted_minutes": 1,
                 "saved_updates": 12}
        quote = {"status": "completed", "saved": 14, "failures": 0,
                 "summary": {"complete": True}}
        self.assertTrue(capture_completion_ok(result, quote, include_banks=True,
                                              include_depth=True, include_activity=True,
                                              trial_report=trial))
        self.assertFalse(capture_completion_ok(result, quote))
        self.assertFalse(capture_completion_ok(result, quote, trial_report={"accepted_minutes": 0}))
        self.assertFalse(capture_completion_ok({**result, "inserted": 0}, None, trial_report=trial))
        self.assertFalse(capture_completion_ok({**result, "inserted": 11}, None, trial_report=trial))
        self.assertFalse(capture_completion_ok(result, None,
                                               trial_report={**trial, "saved_updates": 11}))
        self.assertFalse(capture_completion_ok(result, None,
                                               trial_report={**trial, "full_accepted_minutes": 0}))
        self.assertFalse(capture_completion_ok({**result, "inserted": 13}, None,
                                               trial_report=trial))
        self.assertIn("mismatch", capture_reconciliation_note({**result, "inserted": 13}, trial))
        self.assertFalse(capture_completion_ok({**result, "equity_status": "subscribe_failed"},
                                               quote, trial_report=trial, include_banks=True))
        self.assertFalse(capture_completion_ok({**result, "depth_status": "partial_coverage"},
                                               quote, trial_report=trial, include_depth=True))
        self.assertFalse(capture_completion_ok(result, {**quote, "failures": 1},
                                               trial_report=trial, include_activity=True))
        self.assertFalse(capture_completion_ok(result, {**quote, "summary": {"complete": False}},
                                               trial_report=trial, include_activity=True))

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.window = ConnectionWindow(self.root, Path(self.temp.name))

    def test_first_trial_defaults_to_five_minutes_and_labels_full_day_separately(self):
        self.assertEqual(self.window.capture_minutes.get(), "5")
        self.assertFalse(self.window.include_banks.get())
        self.assertFalse(self.window.include_activity.get())
        self.assertFalse(self.window.include_depth.get())
        note = capture_quality_note({"accepted_minutes": 3, "observed_minutes": 5,
                                     "rejected_minutes": 2, "missing_whole_minutes": 370})
        self.assertIn("3 of 5 observed minutes accepted", note)
        self.assertIn("370 of 375 full-day slots", note)
        self.assertIn("short trials leave other slots unrecorded", note)

    def test_futures_button_blocks_closed_market_before_using_token(self):
        self.window.token.insert(0, "fixture-token")
        with patch("connect_groww.regular_market_status", return_value="closed"), \
             patch("connect_groww.threading.Thread") as worker:
            self.window.check_future()
        worker.assert_not_called()
        self.assertEqual(self.window.token.get(), "fixture-token")
        self.assertFalse(self.window.busy)

    def test_nifty_fallback_button_clears_token_and_never_opens_outside_market(self):
        self.window.token.insert(0, "fixture-token")
        with patch("connect_groww.regular_market_status", return_value="closed"), \
             patch("connect_groww.threading.Thread") as worker:
            self.window.check_nifty50()
        worker.assert_not_called()
        self.assertEqual(self.window.token.get(), "fixture-token")
        with patch("connect_groww.regular_market_status", return_value="regular_session"), \
             patch("connect_groww.threading.Thread") as worker:
            self.window.check_nifty50()
        self.assertEqual(self.window.token.get(), "")
        self.assertTrue(self.window.busy)
        self.assertEqual(worker.call_args.kwargs["target"], self.window.read_nifty50_probe)
        self.assertEqual(worker.call_args.kwargs["args"], ("fixture-token",))

    def test_nifty_fallback_probe_does_not_write_bank_archive(self):
        sdk = SimpleNamespace(GrowwAPI=lambda token: object())
        catalog = ("exchange,segment,instrument_type,trading_symbol,groww_symbol,exchange_token\n"
                   "NSE,CASH,IDX,NIFTY,NSE-NIFTY,NIFTY\n")
        with patch.dict(sys.modules, {"growwapi": sdk}), \
             patch("connect_groww.fetch_instrument_catalog_text", return_value=catalog), \
             patch("connect_groww.isolated_groww_feed",
                   side_effect=lambda api: nullcontext(object())), \
             patch("connect_groww.probe_nifty50_feed", return_value={
                 "status": "completed", "unique": 3, "repeats": 2, "rejected": 0}):
            self.window.read_nifty50_probe("SECRET_TEST_TOKEN")
        succeeded, message = self.window.messages.get_nowait()
        self.assertTrue(succeeded)
        self.assertIn("3 distinct", message)
        self.assertNotIn("SECRET_TEST_TOKEN", message)
        self.assertFalse((Path(self.temp.name) / "market-data.sqlite3").exists())

    def test_futures_button_starts_one_worker_and_clears_masked_field(self):
        self.window.token.insert(0, "fixture-token")
        with patch("connect_groww.regular_market_status", return_value="regular_session"), \
             patch("connect_groww.threading.Thread") as worker:
            self.window.check_future()
        self.assertEqual(self.window.token.get(), "")
        self.assertTrue(self.window.busy)
        self.assertEqual(str(self.window.future_button.cget("state")), "disabled")
        self.assertEqual(worker.call_args.kwargs["target"], self.window.read_future)
        self.assertEqual(worker.call_args.kwargs["args"], ("fixture-token",))

    def test_futures_worker_archives_one_quote_and_clears_client_token(self):
        now = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
        catalog = ("exchange,segment,underlying_symbol,instrument_type,trading_symbol,"
                   "groww_symbol,exchange_token,expiry_date\n"
                   "NSE,FNO,BANKNIFTY,FUT,BANKNIFTY26OCTFUT,"
                   "NSE-BANKNIFTY-27Oct26-FUT,48699,2026-10-27\n")
        quote = {"status": "SUCCESS", "payload": {
            "last_price": 55500.5, "volume": 120, "open_interest": 4500,
            "last_trade_time": int(now.timestamp() * 1000) - 5000}}
        clients = []
        class Client:
            def __init__(self, token):
                self.token = token
                clients.append(self)
            def banknifty_future_quote(self, symbol):
                self.assert_symbol = symbol
                return quote
            def close(self):
                self.token = ""
        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz) if tz is not None else now.astimezone().replace(tzinfo=None)
        with patch("connect_groww.datetime", FixedClock), \
             patch("connect_groww.GrowwDataClient", Client), \
             patch("connect_groww.fetch_instrument_catalog_text", return_value=catalog):
            self.window.read_future("fixture-token")
        succeeded, message = self.window.messages.get_nowait()
        self.assertTrue(succeeded)
        self.assertIn("single quote", message)
        self.assertEqual(clients[0].token, "")
        self.assertEqual(clients[0].assert_symbol, "BANKNIFTY26OCTFUT")
        saved = load_future_snapshots(Path(self.temp.name) / "market-data.sqlite3")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["source"], "Groww")

    def test_failed_futures_response_does_not_create_archive(self):
        catalog = ("exchange,segment,underlying_symbol,instrument_type,trading_symbol,"
                   "groww_symbol,exchange_token,expiry_date\n"
                   "NSE,FNO,BANKNIFTY,FUT,BANKNIFTY26OCTFUT,"
                   "NSE-BANKNIFTY-27Oct26-FUT,48699,2026-10-27\n")
        clients = []
        class Client:
            def __init__(self, token):
                self.token = token
                clients.append(self)
            def banknifty_future_quote(self, symbol):
                return {"status": "SUCCESS", "payload": {"last_price": float("nan")}}
            def close(self):
                self.token = ""
        with patch("connect_groww.GrowwDataClient", Client), \
             patch("connect_groww.fetch_instrument_catalog_text", return_value=catalog):
            self.window.read_future("fixture-token")
        succeeded, message = self.window.messages.get_nowait()
        self.assertFalse(succeeded)
        self.assertIn("stopped", message)
        self.assertEqual(clients[0].token, "")
        self.assertFalse((Path(self.temp.name) / "market-data.sqlite3").exists())

    def _run(self, activity_result, *, quote_delay_seconds=0, diagnostic_bounds=None):
        clients = []
        def bank_activity(client, equities, database, **kwargs):
            clients.append(client)
            self.assertEqual(len(equities), 14)
            self.assertEqual(client.token, "fixture-token")
            if quote_delay_seconds:
                time.sleep(quote_delay_seconds)
            if isinstance(activity_result, Exception):
                raise activity_result
            return activity_result
        index_result = {
            "status": "completed", "inserted": 12, "duplicates": 0,
            "rejected": 0, "paper_logged": 0, "paper_errors": 0,
        }
        sdk = SimpleNamespace(GrowwAPI=lambda token: object(), GrowwFeed=lambda api: object())
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"growwapi": sdk}))
            stack.enter_context(patch("connect_groww.isolated_groww_feed",
                                      side_effect=lambda api: nullcontext(object())))
            stack.enter_context(patch("connect_groww.load_reference_weights", return_value={
                "constituents": [{"symbol": f"B{i}"} for i in range(14)]}))
            stack.enter_context(patch("connect_groww.fetch_instrument_catalog_text", return_value="catalog"))
            stack.enter_context(patch("connect_groww.resolve_banknifty", return_value={"groww_symbol": "NSE-BANKNIFTY"}))
            stack.enter_context(patch("connect_groww.resolve_bank_equities", return_value=[
                {"trading_symbol": f"B{i}"} for i in range(14)]))
            stack.enter_context(patch("connect_groww.collect", return_value=index_result))
            stack.enter_context(patch("connect_groww.collect_bank_activity", side_effect=bank_activity))
            summarize = stack.enter_context(patch("connect_groww.summarize_index_trial", return_value={
                "saved_updates": 12, "first_exchange_ist": "2026-10-05T09:15:00+05:30",
                "last_exchange_ist": "2026-10-05T09:15:55+05:30",
                "median_receipt_lag_seconds": 1, "max_receipt_lag_seconds": 1,
                "exchange_time_ahead_updates": 0, "observed_minutes": 1,
                "accepted_minutes": 1, "too_few_samples": 0,
                "full_minutes": 1, "full_observed_minutes": 1,
                "full_accepted_minutes": 1,
                "missing_minute_edge": 0, "internal_gap": 0,
            }))
            stack.enter_context(patch("connect_groww.summarize_recorded_feed", return_value=[]))
            self.window.check_feed("fixture-token", 1, include_activity=True)
            if diagnostic_bounds is not None:
                diagnostic_bounds.append(summarize.call_args.args[1:3])
        reports = []
        while True:
            try:
                reports.append(self.window.messages.get_nowait())
            except queue.Empty:
                break
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].token, "")
        return reports[-1]

    def test_success_reports_both_collectors(self):
        succeeded, message = self._run({
            "status": "completed", "sweeps": 1, "saved": 14, "failures": 0,
            "summary": {"complete": True},
        })
        self.assertTrue(succeeded)
        saved = load_capture_trial_summaries(Path(self.temp.name) / "market-data.sqlite3")
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0]["accepted_minimal_gate"])
        self.assertTrue(saved[0]["requested_bank_quotes"])
        self.assertIn("12 new updates", message)
        self.assertIn("14 bank rows", message)
        self.assertIn("Observed minutes 1, accepted 1", message)
        self.assertIn("Recorder/archive index counts agree", message)
        self.assertIn("Verified local database copy saved", message)
        backups = list((Path(self.temp.name) / "capture-backups").glob("capture-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as restored:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(restored.execute(
                "SELECT COUNT(*) FROM events WHERE event_type = 'capture_trial_summary'").fetchone()[0], 1)

    def test_quote_failure_is_visible_without_erasing_index_result(self):
        succeeded, message = self._run(RuntimeError("simulated quote failure"))
        self.assertFalse(succeeded)
        self.assertIn("12 new updates", message)
        self.assertIn("activity_error", message)
        self.assertIn("unknown bank rows", message)
        self.assertNotIn("simulated quote failure", message)
        saved = load_capture_trial_summaries(Path(self.temp.name) / "market-data.sqlite3")
        self.assertEqual(len(saved), 1)
        self.assertFalse(saved[0]["accepted_minimal_gate"])
        self.assertEqual(saved[0]["bank_quote_trial"]["status"], "activity_error")
        self.assertIn("Verified local database copy saved", message)
        self.assertEqual(len(list((Path(self.temp.name) / "capture-backups").glob("capture-*.sqlite3"))), 1)

    def test_slow_quote_cleanup_does_not_extend_index_quality_window(self):
        bounds = []
        succeeded, message = self._run({
            "status": "completed", "sweeps": 1, "saved": 14, "failures": 0,
            "summary": {"complete": True},
        }, quote_delay_seconds=0.1, diagnostic_bounds=bounds)
        self.assertTrue(succeeded, message)
        self.assertEqual(len(bounds), 1)
        review = load_capture_trial_summaries(Path(self.temp.name) / "market-data.sqlite3")[0]
        overall_finished = datetime.fromisoformat(review["finished_at_utc"])
        self.assertGreater((overall_finished - bounds[0][1]).total_seconds(), 0.05)
        self.assertEqual(datetime.fromisoformat(review["index_finished_at_utc"]), bounds[0][1])

    def test_backup_failure_does_not_change_capture_quality_result(self):
        with patch("connect_groww.backup_after_capture", side_effect=OSError("disk full")):
            succeeded, message = self._run({
                "status": "completed", "sweeps": 1, "saved": 14, "failures": 0,
                "summary": {"complete": True},
            })
        self.assertTrue(succeeded)
        self.assertIn("Automatic local database copy failed", message)
        self.assertNotIn("disk full", message)
        self.assertEqual(len(load_capture_trial_summaries(Path(self.temp.name) / "market-data.sqlite3")), 1)

    def test_preparation_exception_saves_credential_free_failure(self):
        sdk = SimpleNamespace(GrowwAPI=lambda token: object())
        with patch.dict(sys.modules, {"growwapi": sdk}), \
             patch("connect_groww.fetch_instrument_catalog_text",
                   side_effect=RuntimeError("SECRET_TEST_TOKEN")):
            self.window.check_feed("SECRET_TEST_TOKEN", 1)
        succeeded, message = self.window.messages.get_nowait()
        self.assertFalse(succeeded)
        self.assertIn("Groww instrument catalogue", message)
        self.assertNotIn("SECRET_TEST_TOKEN", message)
        saved = load_capture_trial_summaries(Path(self.temp.name) / "market-data.sqlite3")
        self.assertEqual(saved[0]["capture_status"], "preparation_failed")
        self.assertNotIn("SECRET_TEST_TOKEN", str(saved))

    def test_socket_setup_stage_is_saved_without_upstream_exception(self):
        sdk = SimpleNamespace(GrowwAPI=lambda token: object())
        with patch.dict(sys.modules, {"growwapi": sdk}), \
             patch("connect_groww.fetch_instrument_catalog_text", return_value="catalog"), \
             patch("connect_groww.resolve_banknifty", return_value={"groww_symbol": "NSE-BANKNIFTY"}), \
             patch("connect_groww.isolated_groww_feed",
                   side_effect=GrowwFeedSetupError("before_socket_credentials")):
            self.window.check_feed("SECRET_TEST_TOKEN", 1)
        succeeded, message = self.window.messages.get_nowait()
        self.assertFalse(succeeded)
        self.assertIn("before socket credentials", message)
        self.assertNotIn("SECRET_TEST_TOKEN", message)
        saved = json.loads((Path(self.temp.name) / "live-feed-status.json").read_text())
        self.assertEqual(saved["stage"], "before_socket_credentials")
        self.assertNotIn("SECRET_TEST_TOKEN", str(saved))

    def test_collector_exception_saves_failed_capture_review(self):
        sdk = SimpleNamespace(GrowwAPI=lambda token: object())
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"growwapi": sdk}))
            stack.enter_context(patch("connect_groww.fetch_instrument_catalog_text",
                                      return_value="catalog"))
            stack.enter_context(patch("connect_groww.resolve_banknifty",
                                      return_value={"groww_symbol": "NSE-BANKNIFTY"}))
            stack.enter_context(patch("connect_groww.isolated_groww_feed",
                                      side_effect=lambda api: nullcontext(object())))
            stack.enter_context(patch("connect_groww.collect",
                                      side_effect=RuntimeError("SECRET_TEST_TOKEN")))
            self.window.check_feed("SECRET_TEST_TOKEN", 1)
        succeeded, message = self.window.messages.get_nowait()
        self.assertFalse(succeeded)
        self.assertNotIn("SECRET_TEST_TOKEN", message)
        saved = load_capture_trial_summaries(Path(self.temp.name) / "market-data.sqlite3")
        self.assertEqual(saved[0]["capture_status"], "capture_exception")
        self.assertNotIn("SECRET_TEST_TOKEN", str(saved))
        self.assertIn("Verified local database copy saved", message)
        self.assertEqual(len(list((Path(self.temp.name) / "capture-backups").glob("capture-*.sqlite3"))), 1)

    def test_cancelled_preparation_has_review_without_opening_sdk(self):
        from datetime import datetime, timezone
        opening = datetime(2026, 10, 5, 3, 45, tzinfo=timezone.utc)
        with patch("connect_groww.wait_until", return_value=False), \
             patch.dict(sys.modules, {"growwapi": None}):
            self.window.check_feed("fixture-token", 390, scheduled_open=opening)
        succeeded, message = self.window.messages.get_nowait()
        self.assertFalse(succeeded)
        self.assertIn("cancelled", message)
        saved = load_capture_trial_summaries(Path(self.temp.name) / "market-data.sqlite3")
        self.assertEqual(saved[0]["capture_status"], "preparation_cancelled")


if __name__ == "__main__":
    unittest.main()

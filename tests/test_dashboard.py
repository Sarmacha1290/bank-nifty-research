import csv
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from banknifty.history import list_datasets
from banknifty.institutional_flows import stage_file
from banknifty.capture_trial_history import (build_capture_trial_summary,
                                             save_capture_trial_summary)
from datetime import datetime, timedelta, timezone
from dashboard import Dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = Dashboard(self.root, self.temp.name)

    def test_sample_can_be_loaded_reopened_and_exported_without_network(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Unexpected network request")):
            self.assertFalse(self.app.database.exists())
            self.assertEqual(len(self.app.agent_table.get_children()), 14)
            self.app.load_demo()
            self.app.load_demo()
            self.root.update_idletasks()
            self.assertEqual(len(self.app.candles), 180)
            self.assertEqual(list_datasets(self.app.database)[0]["count"], 180)
            self.assertIn("SYNTHETIC", self.app.chart_caption.cget("text"))
            self.assertEqual(self.app.chart_mode.get(), "Candles")
            self.assertIn("Descriptive only", self.app.pattern_caption.cget("text"))
            self.app.chart_mode.set("Close line")
            self.app.draw_chart()
            self.app.candles = []
            self.app.refresh()
            self.assertEqual(len(self.app.candles), 180)
            target = Path(self.temp.name) / "example.csv"
            with patch("dashboard.filedialog.asksaveasfilename", return_value=str(target)):
                self.app.export_selected()
            with target.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 180)
            self.assertEqual({row["source"] for row in rows}, {"synthetic"})

    def test_connection_window_opens_without_automatically_using_groww(self):
        with patch("connect_groww.fetch_instrument_catalog_text", side_effect=AssertionError("Unexpected Groww call")):
            self.app.connect()
            self.assertTrue(self.app.connection_window.winfo_exists())
            original = self.app.connection_window
            self.app.connect()
            self.assertEqual(original, self.app.connection_window)

    def test_technical_agent_sample_is_explicitly_synthetic(self):
        self.app.run_technical_sample()
        shown = self.app.technical_sample_info.cget("text")
        self.assertIn("INVENTED SAMPLE", shown)
        self.assertIn("15 min:", shown)
        self.assertIn("30 min:", shown)
        self.assertIn("60 min:", shown)
        self.assertIn("confidence unavailable", shown)

    def test_indicator_library_is_inspectable_with_source_label(self):
        self.app.show_indicator_sample()
        windows = [child for child in self.root.winfo_children()
                   if isinstance(child, tk.Toplevel) and
                   child.title() == "Bank Nifty indicator calculations"]
        self.assertEqual(len(windows), 1)
        labels = [child for child in windows[0].winfo_children()[0].winfo_children()
                  if isinstance(child, ttk.Label)]
        self.assertTrue(any("INVENTED DATA" in label.cget("text") for label in labels))

    def test_training_data_check_discloses_zero_models(self):
        self.app.show_training_readiness()
        shown = self.app.master_info.cget("text")
        self.assertIn("Trained forecast models: 0", shown)
        self.assertIn("Official NSE daily rows: 0", shown)

    def test_institutional_view_shows_scope_and_unverified_status(self):
        raw = ('"CATEGORY \n","DATE \n","BUY VALUE \n(₹ Crores)",'
               '"SELL VALUE \n(₹ Crores)","NET VALUE \n(₹ Crores)"\n'
               '"DII","01-Oct-2026","130.00","110.00","20.00"\n'
               '"FII/FPI","01-Oct-2026","100.00","120.00","-20.00"\n')
        source = Path(self.temp.name) / "flow.csv"
        source.write_bytes(raw.encode("utf-8"))
        stage_file(self.app.database, source, scope="nse_cash",
                   observed_at=datetime.now(timezone.utc), file_format="nse_download")
        self.app.show_institutional_flows()
        shown = self.app.flows_info.cget("text")
        self.assertIn("NSE-only cash market", shown)
        self.assertIn("SOURCE AUTHENTICITY UNVERIFIED", shown)
        self.assertIn("excluded from Master", shown)

    def test_intraday_export_refuses_to_invent_rows_from_empty_archive(self):
        with patch("dashboard.filedialog.asksaveasfilename", side_effect=AssertionError("No dialog expected")):
            self.app.export_intraday_research()
        self.assertIn("no research CSV was created", self.app.notice.get())

    def test_expiry_review_reports_missing_real_history(self):
        self.app.review_expiry_day_movement()
        self.assertIn("No labelled, expiry-classified", self.app.notice.get())

    def test_bank_move_review_keeps_empty_feed_unavailable(self):
        self.app.show_bank_move_review()
        self.assertIn("unavailable", self.app.equity_info.cget("text"))
        with patch("dashboard.filedialog.asksaveasfilename",
                   side_effect=AssertionError("No dialog expected")):
            self.app.export_bank_move_review()
        self.assertIn("no bank-movement CSV was created", self.app.notice.get())

    def test_option_change_export_has_no_dialog_without_real_pair(self):
        self.app.show_option_changes()
        self.assertIn("need_two_saved_snapshots", self.app.option_info.cget("text"))
        with patch("dashboard.filedialog.asksaveasfilename",
                   side_effect=AssertionError("No dialog expected")):
            self.app.export_option_changes()
        self.assertIn("No recent same-expiry option pair", self.app.option_info.cget("text"))

    def test_future_change_review_has_no_invented_pair(self):
        self.app.show_future_changes()
        self.assertIn("need_two_saved_quotes", self.app.future_info.cget("text"))

    def test_visual_map_button_generates_current_local_file(self):
        with patch("dashboard.webbrowser.open_new_tab", return_value=True) as browser:
            self.app.open_project_map()
        target = Path(__file__).resolve().parents[1] / "project-map.html"
        self.assertTrue(target.is_file())
        browser.assert_called_once_with(target.as_uri())
        self.assertIn("Visual project map opened", self.app.notice.get())

    def test_latest_capture_review_is_visible_after_reopening(self):
        self.app.review_latest_capture()
        self.assertIn("No capture review", self.app.capture_review_info.cget("text"))
        started = datetime(2026, 10, 5, 3, 45, tzinfo=timezone.utc)
        summary = build_capture_trial_summary(
            started, started + timedelta(minutes=5),
            {"status": "no_fresh_updates", "inserted": 0}, None, None)
        save_capture_trial_summary(self.app.database, summary)
        self.app.review_latest_capture()
        shown = self.app.capture_review_info.cget("text")
        self.assertIn("needs review", shown)
        self.assertIn("not market-data authenticity or forecast accuracy", shown)
        target = Path(self.temp.name) / "capture-reviews.csv"
        with patch("dashboard.filedialog.asksaveasfilename", return_value=str(target)):
            self.app.export_capture_reviews()
        self.assertIn("Exported 1 capture review", self.app.notice.get())
        self.assertIn("no_fresh_updates", target.read_text(encoding="utf-8-sig"))

    def test_empty_capture_reviews_do_not_open_save_dialog(self):
        with patch("dashboard.filedialog.asksaveasfilename",
                   side_effect=AssertionError("No dialog expected")):
            self.app.export_capture_reviews()
        self.assertIn("no export was created", self.app.capture_review_info.cget("text"))


if __name__ == "__main__":
    unittest.main()

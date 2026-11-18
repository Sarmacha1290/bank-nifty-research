"""Saved option differences cannot become invented trade-side or fresh quotes."""

from dataclasses import asdict
import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from banknifty.agents.options_futures import analyze_option_chain
from banknifty.option_history import save_option_chain_analysis
from banknifty.option_snapshot_review import (compare_saved_option_snapshots,
                                               export_latest_option_changes_csv,
                                               review_latest_saved_option_pair)


WHEN = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)


def saved(when, *, call_oi=100, put_oi=120, second_strike=False):
    strikes = {"54000": {
        "CE": {"trading_symbol": "BANKNIFTYEXP54000CE", "open_interest": call_oi,
               "volume": 20, "ltp": 200},
        "PE": {"trading_symbol": "BANKNIFTYEXP54000PE", "open_interest": put_oi,
               "volume": 30, "ltp": 180}}}
    if second_strike:
        strikes["54100"] = {
            "CE": {"trading_symbol": "BANKNIFTYEXP54100CE", "open_interest": 50,
                   "volume": 10, "ltp": 150},
            "PE": {"trading_symbol": "BANKNIFTYEXP54100PE", "open_interest": 60,
                   "volume": 12, "ltp": 230}}
    analysis = analyze_option_chain({"status": "SUCCESS", "payload": {
        "underlying_ltp": 54000, "strikes": strikes}}, date(2026, 10, 27), when)
    result = asdict(analysis)
    result["expiry_date"] = analysis.expiry_date.isoformat()
    result["received_at_utc"] = analysis.received_at_utc.isoformat()
    result["freshness"] = "unverified_exchange_time_absent"
    return result


class OptionSnapshotReviewTests(unittest.TestCase):
    def test_same_contracts_show_reported_oi_change_without_trade_side(self):
        result = compare_saved_option_snapshots(saved(WHEN),
            saved(WHEN + timedelta(minutes=5), call_oi=130, put_oi=105))
        self.assertEqual(result["status"], "comparable_contract_universe")
        self.assertEqual(result["call_oi_change_reported"], 30)
        self.assertEqual(result["put_oi_change_reported"], -15)
        self.assertIn("not_trade_side", result["interpretation"])

    def test_changed_contract_universe_suppresses_whole_chain_totals(self):
        result = compare_saved_option_snapshots(saved(WHEN),
            saved(WHEN + timedelta(minutes=5), second_strike=True))
        self.assertEqual(result["status"], "partial_or_changed_universe")
        self.assertEqual(result["common_contracts"], 2)
        self.assertIsNone(result["call_oi_change_reported"])

    def test_stale_or_cross_day_pair_rejected(self):
        with self.assertRaisesRegex(ValueError, "30 minutes"):
            compare_saved_option_snapshots(saved(WHEN), saved(WHEN + timedelta(minutes=31)))
        with self.assertRaisesRegex(ValueError, "30 minutes"):
            compare_saved_option_snapshots(saved(WHEN), saved(WHEN + timedelta(days=1)))

    def test_no_saved_pair_has_no_reported_change(self):
        with patch("banknifty.option_snapshot_review.load_option_chain_snapshots", return_value=[]):
            result = review_latest_saved_option_pair("missing.sqlite3")
        self.assertEqual(result["status"], "need_two_saved_snapshots")
        self.assertIsNone(result["comparison"])

    def test_actual_archive_path_reviews_two_saved_snapshots(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            for when, call_oi in ((WHEN, 100), (WHEN + timedelta(minutes=5), 130)):
                analysis = analyze_option_chain({"status": "SUCCESS", "payload": {
                    "underlying_ltp": 54000, "strikes": {"54000": {
                        "CE": {"trading_symbol": "BANKNIFTYEXP54000CE",
                               "open_interest": call_oi, "volume": 20, "ltp": 200},
                        "PE": {"trading_symbol": "BANKNIFTYEXP54000PE",
                               "open_interest": 120, "volume": 30, "ltp": 180}}}}},
                    date(2026, 10, 27), when)
                save_option_chain_analysis(database, analysis)
            review = review_latest_saved_option_pair(database)
            self.assertEqual(review["status"], "receipt_time_pair_reviewed")
            self.assertEqual(review["comparison"]["call_oi_change_reported"], 30)

    def test_export_marks_new_contracts_and_suppresses_partial_totals(self):
        comparison = compare_saved_option_snapshots(saved(WHEN),
            saved(WHEN + timedelta(minutes=5), second_strike=True))
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            target = Path(folder) / "option-changes.csv"
            with patch("banknifty.option_snapshot_review.review_latest_saved_option_pair",
                       return_value={"comparison": comparison}):
                count = export_latest_option_changes_csv(database, target)
            self.assertEqual(count, 4)
            with target.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            new = [row for row in rows if row["availability"] == "current_only"]
            self.assertEqual(len(new), 2)
            self.assertTrue(all(row["oi_change_reported"] == "" for row in new))
            self.assertTrue(all(row["call_oi_change_reported"] == "" for row in rows))
            with self.assertRaisesRegex(ValueError, "cannot overwrite"):
                export_latest_option_changes_csv(database, database)

    def test_malformed_saved_symbol_cannot_be_exported_to_excel(self):
        bad = saved(WHEN)
        bad["contracts"][0]["trading_symbol"] = "=1+1"
        with self.assertRaisesRegex(ValueError, "Invalid or duplicate"):
            compare_saved_option_snapshots(bad, saved(WHEN + timedelta(minutes=5)))


if __name__ == "__main__":
    unittest.main()

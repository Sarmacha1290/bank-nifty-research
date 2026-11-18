"""Audit must expose bank gaps and exclude quotes unavailable at an anchor."""
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.alignment import audit_alignment, audit_latest_recorded_session
from banknifty.equity_feed import EquityTick
from banknifty.equity_history import load_equity_ticks, save_equity_ticks
from banknifty.feed import IndexTick
from banknifty.groww import IST
from banknifty.tick_history import latest_index_session_date, save_index_tick


BASE = datetime(2026, 10, 5, 10, 0, tzinfo=IST)
REFERENCE = Path(__file__).resolve().parent / "fixtures" / "synthetic-bank-weights.json"


def index(at=BASE):
    return IndexTick("NSE-BANKNIFTY", at, at + timedelta(seconds=3), 55000.0)


def bank(symbol, at, received=None):
    return EquityTick(symbol, at, received or at + timedelta(seconds=1), 100.0)


class AlignmentTests(unittest.TestCase):
    def test_same_minute_exchange_and_receipt_cutoffs(self):
        report = audit_alignment(
            [index()],
            [bank("NSE-A", BASE - timedelta(seconds=2), BASE + timedelta(seconds=1)),
             bank("NSE-B", BASE - timedelta(seconds=1), BASE + timedelta(seconds=4))],
            ("NSE-A", "NSE-B"))
        self.assertEqual(report["observed_index_minutes"], 1)
        self.assertEqual(report["all_banks_fresh_minutes"], 0)
        self.assertEqual(report["per_bank"]["NSE-A"]["fresh"], 1)
        self.assertEqual(report["per_bank"]["NSE-B"]["not_received_yet"], 1)

    def test_future_quote_not_used_and_stale_quote_visible(self):
        report = audit_alignment(
            [index()],
            [bank("NSE-A", BASE + timedelta(seconds=1)),
             bank("NSE-B", BASE - timedelta(seconds=20))],
            ("NSE-A", "NSE-B"))
        self.assertEqual(report["per_bank"]["NSE-A"]["no_prior_quote"], 1)
        self.assertEqual(report["per_bank"]["NSE-B"]["stale"], 1)

    def test_last_index_update_in_minute_is_anchor_and_all_banks_can_pass(self):
        late = BASE + timedelta(seconds=30)
        report = audit_alignment(
            [index(), index(late)],
            [bank("NSE-A", late - timedelta(seconds=1)),
             bank("NSE-B", late - timedelta(seconds=2))],
            ("NSE-A", "NSE-B"))
        self.assertEqual(report["observed_index_minutes"], 1)
        self.assertEqual(report["all_banks_fresh_minutes"], 1)

    def test_empty_archive_is_honest_and_does_not_create_feed_rows(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            report = audit_latest_recorded_session(database, REFERENCE)
            self.assertEqual(report["status"], "no_index_data")
            self.assertEqual(report["observed_index_minutes"], 0)
            self.assertEqual(latest_index_session_date(database), None)
            self.assertEqual(load_equity_ticks(database), [])
            self.assertFalse(database.exists())

    def test_recorded_session_reports_missing_banks_without_adding_prices(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            save_index_tick(database, index())
            save_equity_ticks(database, (bank("NSE-MOCKBANK01", BASE - timedelta(seconds=1)),))
            report = audit_latest_recorded_session(database, REFERENCE)
            self.assertEqual(report["session_date"], "2026-10-05")
            self.assertEqual(report["expected_regular_minutes"], 375)
            self.assertEqual(report["observed_index_minutes"], 1)
            self.assertEqual(report["all_banks_fresh_minutes"], 0)
            self.assertEqual(report["per_bank"]["NSE-MOCKBANK01"]["fresh"], 1)
            self.assertEqual(report["per_bank"]["NSE-MOCKBANK02"]["no_prior_quote"], 1)


if __name__ == "__main__":
    unittest.main()

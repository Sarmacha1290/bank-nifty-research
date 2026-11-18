"""Daily export keeps market coverage separate from model performance."""
import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.agents.options_futures import analyze_option_chain
from banknifty.depth_feed import DepthTick
from banknifty.depth_history import save_depth_ticks
from banknifty.equity_feed import EquityTick
from banknifty.equity_history import save_equity_ticks
from banknifty.feed import IndexTick
from banknifty.groww import IST
from banknifty.market_activity import BankQuote
from banknifty.market_activity_history import save_bank_quotes
from banknifty.option_history import save_option_chain_analysis
from banknifty.session_diagnostics import (build_session_diagnostics,
                                           export_session_diagnostics_csv)
from banknifty.tick_history import save_index_tick


BASE = datetime(2026, 10, 5, 10, 0, tzinfo=IST)
REFERENCE = Path(__file__).resolve().parent / "fixtures" / "synthetic-bank-weights.json"


class SessionDiagnosticsTests(unittest.TestCase):
    def test_empty_archive_has_no_daily_result(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            destination = Path(folder) / "summary.csv"
            self.assertEqual(build_session_diagnostics(database, REFERENCE), [])
            with self.assertRaisesRegex(ValueError, "No recorded"):
                export_session_diagnostics_csv(database, REFERENCE, destination)
            self.assertFalse(destination.exists())

    def test_one_observed_minute_remains_partial_and_exportable(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            save_index_tick(database, IndexTick("NSE-BANKNIFTY", BASE,
                                                BASE + timedelta(seconds=3), 55000.0))
            save_equity_ticks(database, (EquityTick("NSE-MOCKBANK01", BASE,
                                                    BASE + timedelta(seconds=1), 100.0),))
            chain = {"status": "SUCCESS", "payload": {"underlying_ltp": 55000.0,
                      "strikes": {"55000": {
                          "CE": {"trading_symbol": "BANKNIFTY26O2755000CE", "ltp": 100.0,
                                 "open_interest": 10, "volume": 5},
                          "PE": {"trading_symbol": "BANKNIFTY26O2755000PE", "ltp": 100.0,
                                 "open_interest": 20, "volume": 6}}}}}
            save_option_chain_analysis(database,
                analyze_option_chain(chain, date(2026, 10, 27), BASE))
            save_bank_quotes(database, "during", [
                BankQuote("NSE-MOCKBANK01", BASE, 100, 30, 20, 100.0)])
            save_bank_quotes(database, "after", [
                BankQuote("NSE-MOCKBANK01", BASE.replace(hour=15, minute=31), 101, 31, 20, 100.0)])
            save_depth_ticks(database, [
                DepthTick("NSE-MOCKBANK01", BASE, BASE + timedelta(seconds=1),
                          ((99.0, 10.0),), ((101.0, 9.0),)),
                DepthTick("NSE-UNRELATED", BASE, BASE + timedelta(seconds=1),
                          ((99.0, 10.0),), ((101.0, 9.0),)),
            ])
            rows = build_session_diagnostics(database, REFERENCE)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["observed_index_minutes"], 1)
            self.assertEqual(row["accepted_sampled_index_minutes"], 0)
            self.assertEqual(row["missing_whole_index_minutes"], 374)
            self.assertEqual(row["banks_with_updates"], 1)
            self.assertEqual(row["index_minutes_with_all_banks_available"], 0)
            self.assertEqual(row["option_chain_snapshots_received_that_day"], 1)
            self.assertEqual(row["bank_quote_rows_received_during_session"], 1)
            self.assertEqual(row["bank_quote_sweeps_completed_during_session"], 1)
            self.assertEqual(row["bank_depth_updates_exchanged_during_session"], 1)
            self.assertEqual(row["banks_with_depth_updates"], 1)
            self.assertIn("no_forecast", row["interpretation"])
            destination = Path(folder) / "summary.csv"
            self.assertEqual(export_session_diagnostics_csv(database, REFERENCE, destination), 1)
            with destination.open(encoding="utf-8-sig", newline="") as stream:
                exported = list(csv.DictReader(stream))
            self.assertEqual(exported[0]["session_date_ist"], "2026-10-05")
            self.assertEqual(exported[0]["bank_depth_updates_exchanged_during_session"], "1")
            with self.assertRaises(ValueError):
                export_session_diagnostics_csv(database, REFERENCE, database)


if __name__ == "__main__":
    unittest.main()

"""Futures tests use fixtures only; they do not claim a live broker quote."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from banknifty.futures import (describe_futures_context, describe_futures_rollover,
                               nearest_banknifty_future, parse_future_quote)
from banknifty.futures_history import (export_future_snapshots_csv, load_future_snapshots,
                                        save_future_snapshot)
from banknifty.groww import DataAccessError, GrowwDataClient


CATALOG = ("exchange,segment,underlying_symbol,instrument_type,trading_symbol,groww_symbol,exchange_token,expiry_date\n"
           "NSE,FNO,BANKNIFTY,FUT,BANKNIFTY26DECFUT,NSE-BANKNIFTY-29Dec26-FUT,58872,2026-12-29\n"
           "NSE,FNO,BANKNIFTY,FUT,BANKNIFTY26OCTFUT,NSE-BANKNIFTY-27Oct26-FUT,48699,2026-10-27\n")
NOW = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
QUOTE = {"status": "SUCCESS", "payload": {"last_price": 55500.5,
          "last_trade_time": int(NOW.timestamp() * 1000) - 5000, "volume": 120,
          "open_interest": 4500, "previous_open_interest": 4400, "oi_day_change": 100}}


class FutureTests(unittest.TestCase):
    def test_nearest_catalogue_selection_and_expiry(self):
        row = nearest_banknifty_future(CATALOG, date(2026, 10, 5))
        self.assertEqual((row.trading_symbol, row.exchange_token),
                         ("BANKNIFTY26OCTFUT", "48699"))
        self.assertEqual(nearest_banknifty_future(CATALOG, date(2026, 10, 28)).expiry_date,
                         date(2026, 12, 29))
        with self.assertRaises(DataAccessError):
            nearest_banknifty_future(CATALOG, date(2027, 1, 1))

    def test_catalogue_duplicate_and_unsafe_symbol_rejected(self):
        duplicate = CATALOG + CATALOG.splitlines()[2] + "\n"
        with self.assertRaises(DataAccessError):
            nearest_banknifty_future(duplicate, date(2026, 10, 5))
        unsafe = CATALOG.replace("BANKNIFTY26OCTFUT", "=1+1BANKNIFTY26OCTFUT")
        self.assertEqual(nearest_banknifty_future(unsafe, date(2026, 10, 5)).expiry_date,
                         date(2026, 12, 29))

    def test_quote_time_is_last_trade_not_snapshot_time(self):
        row = nearest_banknifty_future(CATALOG, date(2026, 10, 5))
        snap = parse_future_quote(QUOTE, row, NOW, source_kind="synthetic")
        self.assertEqual(snap.source, "synthetic")
        self.assertEqual(snap.freshness, "last_trade_time_only")
        self.assertEqual((NOW - snap.last_trade_at_utc).total_seconds(), 5)
        self.assertEqual(snap.open_interest, 4500)
        no_time = {"status": "SUCCESS", "payload": {"last_price": 55500.5}}
        self.assertEqual(parse_future_quote(no_time, row, NOW).freshness,
                         "exchange_time_absent")

    def test_basis_requires_aligned_trade_and_spot_times(self):
        instrument = nearest_banknifty_future(CATALOG, date(2026, 10, 5))
        current = parse_future_quote(QUOTE, instrument, NOW, source_kind="synthetic")
        aligned = describe_futures_context(
            current, spot_price=55000.5, spot_at=current.last_trade_at_utc)
        self.assertEqual(aligned["spot_futures_premium_points"], 500)
        self.assertAlmostEqual(aligned["basis_pct"], 100 * 500 / 55000.5)
        stale = describe_futures_context(
            current, spot_price=55000.5, spot_at=NOW + timedelta(minutes=1))
        self.assertIsNone(stale["basis_pct"])
        earlier_quote = {"status": "SUCCESS", "payload": {
            **QUOTE["payload"], "last_price": 55400,
            "last_trade_time": int((NOW - timedelta(minutes=1)).timestamp() * 1000),
            "open_interest": 4400}}
        previous = parse_future_quote(earlier_quote, instrument,
                                      NOW - timedelta(minutes=1), source_kind="synthetic")
        pattern = describe_futures_context(current, previous=previous)
        self.assertEqual(pattern["price_oi_pattern"], "long_buildup")

    def test_rollover_ratio_requires_two_timed_expiries(self):
        near_inst = nearest_banknifty_future(CATALOG, date(2026, 10, 5))
        far_inst = nearest_banknifty_future(CATALOG, date(2026, 10, 28))
        near = parse_future_quote(QUOTE, near_inst, NOW, source_kind="synthetic")
        far = parse_future_quote(QUOTE, far_inst, NOW, source_kind="synthetic")
        result = describe_futures_rollover(near, far)
        self.assertEqual(result["far_oi_share"], 0.5)
        with self.assertRaisesRegex(ValueError, "time-aligned"):
            describe_futures_rollover(near,
                replace(far, received_at_utc=far.received_at_utc + timedelta(minutes=1)))

    def test_bad_quote_values_and_future_time_rejected(self):
        row = nearest_banknifty_future(CATALOG, date(2026, 10, 5))
        for change in ({"last_price": float("nan")}, {"volume": -1},
                       {"open_interest": float("inf")},
                       {"last_trade_time": int(NOW.timestamp() * 1000) + 1000}):
            quote = {"status": "SUCCESS", "payload": QUOTE["payload"] | change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_future_quote(quote, row, NOW)

    def test_fno_quote_is_get_only_and_symbol_checked(self):
        client = GrowwDataClient("fixture")
        with patch.object(client, "_get", return_value=QUOTE) as request:
            self.assertEqual(client.banknifty_future_quote("BANKNIFTY26OCTFUT"), QUOTE)
            request.assert_called_once_with("/live-data/quote", {
                "exchange": "NSE", "segment": "FNO", "trading_symbol": "BANKNIFTY26OCTFUT"})
        with self.assertRaises(ValueError):
            client.banknifty_future_quote("=1+1")

    def test_archive_revalidates_and_exports_synthetic_fixture(self):
        row = nearest_banknifty_future(CATALOG, date(2026, 10, 5))
        snap = parse_future_quote(QUOTE, row, NOW, source_kind="synthetic")
        with TemporaryDirectory() as directory:
            db = Path(directory) / "market.sqlite3"
            with self.assertRaises(ValueError):
                save_future_snapshot(db, replace(snap, open_interest=-1))
            self.assertFalse(db.exists())
            save_future_snapshot(db, snap)
            loaded = load_future_snapshots(db)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["source"], "synthetic")
            destination = Path(directory) / "futures.csv"
            self.assertEqual(export_future_snapshots_csv(db, destination), 1)
            self.assertIn("BANKNIFTY26OCTFUT", destination.read_text(encoding="utf-8-sig"))
            with self.assertRaises(ValueError):
                export_future_snapshots_csv(db, db)


if __name__ == "__main__":
    unittest.main()

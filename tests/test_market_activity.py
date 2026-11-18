import tempfile
import unittest
from unittest.mock import patch
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import nan
from pathlib import Path

from banknifty.market_activity import (BankQuote, detect_activity_spikes, detect_aggregate_spikes,
                                       parse_bank_quote, summarize_bank_quotes)
from banknifty.market_activity_history import (count_bank_quotes, export_activity_alerts_csv,
    export_bank_quotes_csv, load_activity_alerts, load_bank_quote_sweeps, load_bank_quotes,
    save_activity_alerts, save_bank_quotes)
from banknifty.collect_activity import collect_bank_activity
from banknifty.groww import DataAccessError


UTC = timezone.utc
START = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)


def quote(symbol, minute, volume, buy=100, sell=100):
    return BankQuote("NSE-" + symbol, START + timedelta(seconds=15 * minute),
                     volume, buy, sell, 100.0)


class MarketActivityTests(unittest.TestCase):
    def test_quote_archive_rejects_invalid_direct_rows(self):
        valid = quote("A", 0, 10)
        invalid = (
            replace(valid, symbol="=FORMULA"),
            replace(valid, received_at=START.replace(tzinfo=None)),
            replace(valid, volume=-1),
            replace(valid, total_buy_quantity=nan),
            replace(valid, total_sell_quantity=-2),
            replace(valid, last_price=0),
        )
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "archive.sqlite3"
            for row in invalid:
                with self.subTest(row=row), self.assertRaises(ValueError):
                    save_bank_quotes(database, "safe-sweep", (row,))
                self.assertFalse(database.exists())
            with self.assertRaises(ValueError):
                save_bank_quotes(database, "=FORMULA", (valid,))
            self.assertFalse(database.exists())

    def test_quote_fields_are_not_executed_side_volume(self):
        row = parse_bank_quote({"status": "SUCCESS", "payload": {
            "volume": 1200, "total_buy_quantity": 300, "total_sell_quantity": 200,
            "last_price": 100.0}}, "BANK", START)
        self.assertEqual((row.volume, row.total_buy_quantity, row.total_sell_quantity),
                         (1200, 300.0, 200.0))
        self.assertIsNone(parse_bank_quote({"status": "SUCCESS", "payload": {}}, "BANK", START).volume)
        with self.assertRaises(ValueError):
            parse_bank_quote({"status": "SUCCESS", "payload": {"volume": -1}}, "BANK", START)

    def test_full_coverage_required_for_all_bank_sum(self):
        partial = summarize_bank_quotes([quote("A", 0, 10)], ["NSE-A", "NSE-B"])
        self.assertIsNone(partial["cumulative_traded_shares"])
        full = summarize_bank_quotes([quote("A", 0, 10, 300, 100),
                                      quote("B", 0, 20, 100, 100)], ["NSE-A", "NSE-B"])
        self.assertEqual(full["cumulative_traded_shares"], 30)
        self.assertAlmostEqual(full["order_book_imbalance"], 1 / 3)

    def test_spike_uses_only_prior_session_snapshots(self):
        rows = [quote("A", i, 100 + 10 * i, 100, 100) for i in range(6)]
        rows.append(quote("A", 6, 250, 450, 50))
        alerts = detect_activity_spikes(rows)
        self.assertEqual({row["kind"] for row in alerts},
                         {"traded_volume_surge", "outstanding_buy_orders_surge"})
        self.assertEqual(detect_activity_spikes(rows[:5]), [])

    def test_longer_quote_gap_does_not_mimic_a_volume_spike(self):
        rows = [quote("A", i, 100 + 10 * i) for i in range(6)]
        rows.append(quote("A", 7, 180))
        self.assertEqual(detect_activity_spikes(rows), [])

    def test_all_bank_spike_requires_complete_sweeps(self):
        sweeps = [[quote("A", i, 100 + 10 * i), quote("B", i, 100 + 10 * i)]
                  for i in range(6)]
        sweeps.append([quote("A", 6, 250), quote("B", 6, 250)])
        alerts = detect_aggregate_spikes(sweeps, ["NSE-A", "NSE-B"])
        self.assertEqual([item["kind"] for item in alerts], ["traded_volume_surge"])
        self.assertEqual(detect_aggregate_spikes(sweeps[:-1] + [[quote("A", 6, 250)]],
                                                 ["NSE-A", "NSE-B"]), [])

    def test_spread_out_complete_sweep_cannot_raise_aggregate_spike(self):
        sweeps = [[quote("A", i, 100 + 10 * i), quote("B", i, 100 + 10 * i)]
                  for i in range(6)]
        late_bank = replace(quote("B", 6, 250),
                            received_at=quote("B", 6, 250).received_at + timedelta(seconds=8))
        current = [quote("A", 6, 250), late_bank]
        summary = summarize_bank_quotes(current, ["NSE-A", "NSE-B"])
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["receipt_span_seconds"], 8)
        self.assertFalse(summary["coherent_for_aggregate_spike"])
        self.assertEqual(detect_aggregate_spikes(sweeps + [current],
                                                 ["NSE-A", "NSE-B"]), [])

    def test_archive_and_excel_export(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "archive.sqlite3"
            save_bank_quotes(database, "sweep-one", [quote("A", 0, 10)])
            self.assertEqual(count_bank_quotes(database), 1)
            self.assertEqual(load_bank_quotes(database)[0].volume, 10)
            self.assertEqual(len(load_bank_quote_sweeps(database)), 1)
            csv_path = Path(folder) / "activity.csv"
            self.assertEqual(export_bank_quotes_csv(database, csv_path), 1)
            self.assertIn("outstanding_buy_shares", csv_path.read_text(encoding="utf-8-sig"))
            with self.assertRaises(ValueError):
                save_bank_quotes(database, "sweep-one", [quote("A", 0, 20)])

    def test_alerts_are_durable_and_exportable(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "archive.sqlite3"
            alert = detect_activity_spikes([quote("A", i, 100 + 10 * i) for i in range(6)] +
                                           [quote("A", 6, 250)])[0]
            self.assertEqual(save_activity_alerts(database, [alert]), 1)
            self.assertEqual(save_activity_alerts(database, [alert]), 0)
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                save_activity_alerts(database, [{**alert, "current": alert["current"] * 2,
                    "multiple": alert["multiple"] * 2}])
            self.assertEqual(len(load_activity_alerts(database)), 1)
            self.assertEqual(export_activity_alerts_csv(database, Path(folder) / "alerts.csv"), 1)

    def test_alert_archive_rejects_invalid_or_spreadsheet_controlled_rows(self):
        valid = detect_activity_spikes([quote("A", i, 100 + 10 * i) for i in range(6)] +
                                       [quote("A", 6, 250)])[0]
        invalid = (
            {**valid, "symbol": "NSE-=FORMULA"},
            {**valid, "at": "2026-10-05T04:00:00"},
            {**valid, "meaning": "=HYPERLINK(\"bad\")"},
            {**valid, "current": nan},
            {**valid, "multiple": 999},
        )
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "alerts.sqlite3"
            for row in invalid:
                with self.subTest(row=row), self.assertRaises(ValueError):
                    save_activity_alerts(database, (row,))
                self.assertFalse(database.exists())

    def test_collector_saves_all_14_with_fake_clock(self):
        class Clock:
            value = 0.0
            def now(self): return self.value
            def pause(self, seconds): self.value += seconds
        class Client:
            def equity_quote(self, symbol):
                return {"status": "SUCCESS", "payload": {"volume": 100,
                        "total_buy_quantity": 20, "total_sell_quantity": 10, "last_price": 100}}
        clock = Clock()
        instruments = [{"trading_symbol": f"B{i}"} for i in range(14)]
        with tempfile.TemporaryDirectory() as folder:
            result = collect_bank_activity(Client(), instruments, Path(folder) / "archive.sqlite3",
                duration_seconds=16, clock=clock.now, pause=clock.pause)
            self.assertEqual(result["sweeps"], 2)
            self.assertEqual(result["saved"], 28)
            self.assertTrue(result["summary"]["complete"])

    def test_rate_limit_stops_safely(self):
        class Client:
            calls = 0
            def equity_quote(self, symbol):
                self.calls += 1
                raise DataAccessError("Groww's request limit was reached. Please try later.")
        class Clock:
            value = 0.0
            def now(self): return self.value
            def pause(self, seconds): self.value += seconds
        client, clock = Client(), Clock()
        with tempfile.TemporaryDirectory() as folder:
            result = collect_bank_activity(client, [{"trading_symbol": f"B{i}"} for i in range(14)],
                Path(folder) / "archive.sqlite3", duration_seconds=60,
                clock=clock.now, pause=clock.pause)
        self.assertEqual(result["status"], "access_stopped")
        self.assertEqual(client.calls, 1)

    def test_quote_archive_failure_stops_requests_without_claiming_saved_rows(self):
        class Client:
            calls = 0
            def equity_quote(self, symbol):
                self.calls += 1
                return {"status": "SUCCESS", "payload": {"volume": 100,
                        "total_buy_quantity": 20, "total_sell_quantity": 10,
                        "last_price": 100}}
        class Clock:
            value = 0.0
            def now(self): return self.value
            def pause(self, seconds): self.value += seconds
        client, clock = Client(), Clock()
        with tempfile.TemporaryDirectory() as folder, patch(
                "banknifty.collect_activity.save_bank_quotes",
                side_effect=sqlite3.OperationalError("database or disk is full")):
            database = Path(folder) / "archive.sqlite3"
            result = collect_bank_activity(client,
                [{"trading_symbol": f"B{i}"} for i in range(14)], database,
                duration_seconds=60, clock=clock.now, pause=clock.pause)
            self.assertEqual(result["status"], "archive_error")
            self.assertEqual((result["saved"], result["sweeps"]), (0, 0))
            self.assertEqual(client.calls, 14)
            self.assertEqual(count_bank_quotes(database), 0)


if __name__ == "__main__":
    unittest.main()

"""Aligned bank movement must never fill a missing or late endpoint."""
from datetime import datetime, timedelta
import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from banknifty.bank_move_review import (compare_aligned_bank_moves,
                                         export_latest_bank_moves_csv,
                                         review_latest_recorded_bank_moves,
                                         review_recorded_bank_moves)
from banknifty.equity_feed import EquityTick
from banknifty.feed import IndexTick
from banknifty.groww import IST


BASE_END = datetime(2026, 10, 5, 10, 1, tzinfo=IST)
TARGET_END = BASE_END + timedelta(minutes=15)
REFERENCE = Path(__file__).resolve().parent / "fixtures" / "synthetic-bank-weights.json"


def idx(end, price):
    exchange = end - timedelta(seconds=3)
    return IndexTick("NSE-BANKNIFTY", exchange, exchange + timedelta(seconds=1), price)


def bank(symbol, end, price, *, late=False, future=False):
    exchange = end - timedelta(seconds=4) if not future else end - timedelta(seconds=2)
    received = end + timedelta(seconds=6) if late else exchange + timedelta(seconds=1)
    return EquityTick(symbol, exchange, received, price)


class BankMoveReviewTests(unittest.TestCase):
    def test_complete_pairs_show_individual_moves_but_no_weight_claim(self):
        ticks = [bank("NSE-A", BASE_END, 100), bank("NSE-A", TARGET_END, 110),
                 bank("NSE-B", BASE_END, 100), bank("NSE-B", TARGET_END, 95)]
        report = compare_aligned_bank_moves(idx(BASE_END, 50000),
            idx(TARGET_END, 50500), BASE_END, TARGET_END, ticks, ("NSE-A", "NSE-B"))
        self.assertEqual(report["status"], "complete_unweighted_ex_post")
        self.assertAlmostEqual(report["unweighted_mean_bank_return_pct"], 2.5)
        self.assertEqual(report["advancing_banks"], 1)
        self.assertEqual(report["index_move_points"], 500)
        self.assertIn("not_weighted", report["interpretation"])

    def test_missing_or_late_bank_never_becomes_zero_move(self):
        ticks = [bank("NSE-A", BASE_END, 100), bank("NSE-A", TARGET_END, 110),
                 bank("NSE-B", BASE_END, 100), bank("NSE-B", TARGET_END, 95, late=True)]
        report = compare_aligned_bank_moves(idx(BASE_END, 50000),
            idx(TARGET_END, 50500), BASE_END, TARGET_END, ticks, ("NSE-A", "NSE-B"))
        self.assertEqual(report["status"], "incomplete_bank_endpoints")
        self.assertEqual(report["banks_with_aligned_pairs"], 1)
        self.assertTrue(report["missing_banks"][0]["missing_target"])
        self.assertIsNone(report["unweighted_mean_bank_return_pct"])

    def test_future_quote_and_wrong_horizon_are_rejected(self):
        ticks = [bank("NSE-A", BASE_END, 100, future=True),
                 bank("NSE-A", TARGET_END, 110)]
        result = compare_aligned_bank_moves(idx(BASE_END, 50000),
            idx(TARGET_END, 50500), BASE_END, TARGET_END, ticks, ("NSE-A",))
        self.assertTrue(result["missing_banks"][0]["missing_base"])
        with self.assertRaisesRegex(ValueError, "completed Groww index interval"):
            compare_aligned_bank_moves(idx(BASE_END, 50000),
                idx(TARGET_END, 50500), BASE_END, TARGET_END + timedelta(minutes=1),
                ticks, ("NSE-A",))

    def test_no_archive_does_not_create_data_or_results(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            result = review_latest_recorded_bank_moves(database, REFERENCE)
            self.assertEqual(result["status"], "no_index_data")
            self.assertFalse(database.exists())

    def test_complete_sampled_index_history_selects_exact_targets(self):
        opening = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
        index_ticks = []
        for minute in range(121):
            for second in range(0, 60, 5):
                exchange = opening + timedelta(minutes=minute, seconds=second)
                index_ticks.append(IndexTick("NSE-BANKNIFTY", exchange,
                                             exchange + timedelta(seconds=1), 50000 + minute))
        equity_ticks = []
        for minute in (60, 75, 90, 120):
            end = opening + timedelta(minutes=minute + 1)
            exchange = end - timedelta(seconds=6)
            for symbol, base in (("NSE-A", 100), ("NSE-B", 200)):
                equity_ticks.append(EquityTick(symbol, exchange,
                    exchange + timedelta(seconds=1), base + minute))
        review = review_recorded_bank_moves(index_ticks, equity_ticks,
                                            ("NSE-A", "NSE-B"))
        self.assertEqual(review["status"], "reviewed_index_endpoints")
        self.assertEqual([item["horizon_minutes"] for item in review["results"]], [15, 30, 60])
        self.assertTrue(all(item["status"] == "complete_unweighted_ex_post"
                            for item in review["results"]))
        self.assertEqual(review["results"][0]["index_move_points"], 15)

    def test_csv_preserves_missing_bank_as_blank_price_and_return(self):
        interval = compare_aligned_bank_moves(idx(BASE_END, 50000),
            idx(TARGET_END, 50500), BASE_END, TARGET_END,
            [bank("NSE-A", BASE_END, 100), bank("NSE-A", TARGET_END, 110),
             bank("NSE-B", BASE_END, 100), bank("NSE-B", TARGET_END, 95, late=True)],
            ("NSE-A", "NSE-B"))
        report = {"session_date_ist": "2026-10-05", "bank_list_as_of_date": "2026-09-30",
                  "results": [interval]}
        with TemporaryDirectory() as folder:
            target = Path(folder) / "bank-moves.csv"
            with patch("banknifty.bank_move_review.review_latest_recorded_bank_moves",
                       return_value=report):
                count = export_latest_bank_moves_csv(Path(folder) / "archive.sqlite3",
                                                     REFERENCE, target)
            self.assertEqual(count, 2)
            with target.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            missing = next(row for row in rows if row["symbol"] == "NSE-B")
            self.assertEqual(missing["availability"], "missing_target")
            self.assertEqual(missing["target_ltp"], "")
            self.assertEqual(missing["bank_change_pct"], "")
            self.assertEqual(missing["interval_status"], "incomplete_bank_endpoints")


if __name__ == "__main__":
    unittest.main()

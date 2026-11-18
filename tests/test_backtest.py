import csv
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from banknifty.groww import IST

from banknifty.backtest import (evaluate_recorded_ticks, export_recorded_comparison_csv,
                                wilson_reference_interval)
from banknifty.core import Candle
from banknifty.feed import IndexTick
from banknifty.feed_quality import MinuteQuality, SAMPLED_SOURCE




def day_candles(day, *, future_down=False, falling=False, omit=None, count=136):
    start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    result = []
    for minute in range(count):
        if minute == omit:
            continue
        price = 50000 + minute * (-2 if falling else 2)
        if future_down and minute >= 70:
            price -= 100
        begin = start + timedelta(minutes=minute)
        result.append(Candle("NSE-BANKNIFTY", "index", begin,
                             begin + timedelta(minutes=1), price, price, price,
                             price, SAMPLED_SOURCE))
    return result


def observed(candles, *, weak_end=None):
    quality = [MinuteQuality(c.start, c.end, 12, 0.0,
                             50.0 if c.end == weak_end else 55.0, 5.0, "accepted")
               for c in candles]
    return quality, candles


class BacktestTests(unittest.TestCase):
    def test_wilson_reference_interval_shows_small_sample_uncertainty(self):
        self.assertIsNone(wilson_reference_interval(0, 0))
        lower, upper = wilson_reference_interval(5, 10)
        self.assertAlmostEqual(lower, 0.2366, places=4)
        self.assertAlmostEqual(upper, 0.7634, places=4)
        self.assertAlmostEqual(wilson_reference_interval(1, 1)[0], 0.2065, places=4)
        self.assertEqual(wilson_reference_interval(1, 1)[1], 1.0)
        for invalid in ((-1, 2), (3, 2), (1.0, 2), (True, 2)):
            with self.assertRaises(ValueError):
                wilson_reference_interval(*invalid)

    def test_late_target_update_does_not_create_a_retrospective_label(self):
        ticks = []
        for day in (5, 6, 7, 8, 9):
            start = datetime(2026, 10, day, 9, 15, tzinfo=IST)
            for minute in range(76):
                for second in range(0, 60, 5):
                    at = start + timedelta(minutes=minute, seconds=second)
                    received = at + timedelta(seconds=1)
                    if day == 9 and minute == 75 and second == 55:
                        received = start + timedelta(minutes=76, seconds=6)
                    ticks.append(IndexTick("NSE-BANKNIFTY", at, received,
                                           50000 + minute * 2.0))
        report = evaluate_recorded_ticks(ticks)
        self.assertEqual(report["excluded_late_or_clock_invalid_ticks"], 1)
        holdout = report["partitions"]["holdout"]["horizons"]["15"]
        self.assertEqual(holdout["eligible_origins"], 1)
        self.assertEqual(holdout["near_end_labels"], 0)

    def test_needs_five_whole_sessions_before_reporting_split_metrics(self):
        candles = day_candles(date(2026, 10, 5))
        with patch("banknifty.backtest.inspect_sampled_minutes", return_value=observed(candles)):
            report = evaluate_recorded_ticks([])
        self.assertEqual(report["split_status"], "insufficient_sessions")
        self.assertEqual(report["partitions"], {})

    def test_holdout_keeps_later_session_out_of_development(self):
        dates = [date(2026, 10, day) for day in (5, 6, 7, 8, 9)]
        candles = [c for day in dates for c in day_candles(day, future_down=day == dates[-1])]
        with patch("banknifty.backtest.inspect_sampled_minutes", return_value=observed(candles)):
            report = evaluate_recorded_ticks([])
        self.assertEqual(report["split_status"], "ready")
        self.assertEqual(report["partitions"]["holdout"]["dates"], ["2026-10-09"])
        development = report["partitions"]["development"]["horizons"]["15"]
        holdout = report["partitions"]["holdout"]["horizons"]["15"]
        self.assertGreater(development["near_end_labels"], 0)
        self.assertGreater(holdout["near_end_labels"], 0)
        self.assertGreater(development["always_up_hit_rate"], holdout["always_up_hit_rate"])
        self.assertIsNone(holdout["technical_wilson95_lower"])
        self.assertLess(holdout["always_up_wilson95_lower"],
                        holdout["always_up_wilson95_upper"])

    def test_day_mean_keeps_one_busy_day_from_dominating(self):
        dates = [date(2026, 10, day) for day in (5, 6, 7, 8, 9)]
        candles = [c for i, day in enumerate(dates) for c in day_candles(
            day, falling=i == 0, count=375 if i == 0 else 136)]
        with patch("banknifty.backtest.inspect_sampled_minutes", return_value=observed(candles)):
            report = evaluate_recorded_ticks([])
        development = report["partitions"]["development"]["horizons"]["15"]
        self.assertEqual(development["always_up_days_with_calls"], 3)
        self.assertAlmostEqual(development["always_up_mean_day_hit_rate"], 2 / 3)
        self.assertLess(development["always_up_hit_rate"],
                        development["always_up_mean_day_hit_rate"])
        self.assertEqual(development["always_up_min_day_hit_rate"], 0)
        self.assertEqual(development["always_up_max_day_hit_rate"], 1)

    def test_matched_comparison_excludes_baseline_only_origins(self):
        dates = [date(2026, 10, day) for day in (5, 6, 7, 8, 9)]
        candles = [c for day in dates for c in day_candles(day)]
        def selective_assess(snapshot, horizon, *, generated_at):
            return SimpleNamespace(bias="bullish" if snapshot.as_of.date() == dates[0] else "neutral")
        with patch("banknifty.backtest.inspect_sampled_minutes", return_value=observed(candles)), \
             patch("banknifty.backtest.assess", side_effect=selective_assess):
            report = evaluate_recorded_ticks([])
        row = report["partitions"]["development"]["horizons"]["15"]
        self.assertLess(row["technical_vs_always_up_matched_calls"], row["always_up_calls"])
        self.assertEqual(row["technical_vs_always_up_matched_calls"], row["technical_calls"])
        self.assertEqual(row["technical_vs_always_up_days_with_matched_calls"], 1)
        self.assertEqual(row["technical_vs_always_up_matched_difference"], 0)
        self.assertEqual(row["technical_vs_momentum_matched_calls"], row["technical_calls"])

    def test_missing_exact_endpoint_reduces_coverage(self):
        dates = [date(2026, 10, day) for day in (5, 6, 7, 8, 9)]
        full = [c for day in dates for c in day_candles(day)]
        gapped = [c for day in dates for c in day_candles(day, omit=75 if day == dates[-1] else None)]
        with patch("banknifty.backtest.inspect_sampled_minutes", return_value=observed(full)):
            first = evaluate_recorded_ticks([])
        with patch("banknifty.backtest.inspect_sampled_minutes", return_value=observed(gapped)):
            second = evaluate_recorded_ticks([])
        a = first["partitions"]["holdout"]["horizons"]["15"]
        b = second["partitions"]["holdout"]["horizons"]["15"]
        self.assertLess(b["near_end_labels"], a["near_end_labels"])

    def test_weak_target_edge_reduces_label_coverage(self):
        dates = [date(2026, 10, day) for day in (5, 6, 7, 8, 9)]
        candles = [c for day in dates for c in day_candles(day)]
        target = datetime(2026, 10, 9, 10, 31, tzinfo=IST)
        with patch("banknifty.backtest.inspect_sampled_minutes", return_value=observed(candles)):
            clean = evaluate_recorded_ticks([])
        with patch("banknifty.backtest.inspect_sampled_minutes",
                   return_value=observed(candles, weak_end=target)):
            weak = evaluate_recorded_ticks([])
        a = clean["partitions"]["holdout"]["horizons"]["15"]
        b = weak["partitions"]["holdout"]["horizons"]["15"]
        self.assertEqual(b["eligible_origins"], a["eligible_origins"])
        self.assertEqual(b["near_end_labels"], a["near_end_labels"] - 1)

    def test_csv_report_names_provenance_and_denominators(self):
        dates = [date(2026, 10, day) for day in (5, 6, 7, 8, 9)]
        candles = [c for day in dates for c in day_candles(day)]
        with patch("banknifty.backtest.inspect_sampled_minutes", return_value=observed(candles)):
            report = evaluate_recorded_ticks([])
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "market.sqlite3"
            database.write_bytes(b"archive must remain untouched")
            destination = Path(temporary) / "report.csv"
            with patch("banknifty.backtest.evaluate_recorded_database", return_value=report):
                self.assertEqual(export_recorded_comparison_csv(database, destination), 9)
            with destination.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 9)
            self.assertEqual(rows[-1]["partition"], "holdout")
            self.assertEqual(rows[-1]["horizon_minutes"], "60")
            self.assertEqual(rows[-1]["source"], SAMPLED_SOURCE)
            self.assertEqual(rows[-1]["excluded_late_or_clock_invalid_ticks"], "0")
            self.assertIn("not official exchange OHLC", rows[-1]["data_status"])
            self.assertEqual(rows[-1]["formula_version"], "technical-rules-v0.2")
            self.assertIn("always_up_wilson95_lower", rows[-1])
            self.assertEqual(rows[-1]["always_up_days_with_calls"], "1")
            self.assertIn("always_up_mean_day_hit_rate", rows[-1])
            self.assertIn("technical_vs_always_up_matched_calls", rows[-1])
            self.assertIn("technical_vs_momentum_matched_difference", rows[-1])
            self.assertIn("independent calls", rows[-1]["interpretation_note"])
            self.assertEqual(database.read_bytes(), b"archive must remain untouched")
            with self.assertRaisesRegex(ValueError, "must not overwrite"):
                export_recorded_comparison_csv(database, database)

    def test_csv_not_created_without_sufficient_dates(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "report.csv"
            with patch("banknifty.backtest.evaluate_recorded_database",
                       return_value={"split_status": "insufficient_sessions"}):
                with self.assertRaisesRegex(ValueError, "five analysis-ready"):
                    export_recorded_comparison_csv(Path(temporary) / "data.sqlite3", destination)
            self.assertFalse(destination.exists())

    def test_market_close_origin_is_not_counted_as_eligible(self):
        dates = [date(2026, 10, day) for day in (5, 6, 7, 8, 9)]
        candles = [c for day in dates for c in day_candles(day, count=375)[14:]]
        with patch("banknifty.backtest.inspect_sampled_minutes", return_value=observed(candles)):
            report = evaluate_recorded_ticks([])
        for horizon in ("15", "30", "60"):
            row = report["partitions"]["development"]["horizons"][horizon]
            self.assertEqual(row["eligible_origins"], 15)
            self.assertEqual(row["near_end_labels"], 15)


if __name__ == "__main__":
    unittest.main()


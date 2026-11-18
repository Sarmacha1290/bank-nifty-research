"""Research labels must never enter features or hide missing minutes."""
import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from banknifty.feed import IndexTick
from banknifty.groww import IST
from banknifty.expiry_context import record_expiry_reference
from banknifty.event_context import parse_event_csv
from banknifty.training_dataset import (ADDITIONAL_INDICATORS, FEATURES,
                                        build_training_research_rows,
                                        export_training_research_csv)


def recorded_day(day, *, count=121, omit=None, future_jump=0, late_minute=None,
                 weak_edge_minute=None):
    open_at = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    rows = []
    for minute in range(count):
        if minute == omit:
            continue
        seconds = (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 49, 50) if minute == weak_edge_minute else range(0, 60, 5)
        for second in seconds:
            exchange_at = open_at + timedelta(minutes=minute, seconds=second)
            received_at = exchange_at + timedelta(seconds=1 if minute != late_minute else 90)
            price = 50000.0 + minute * 2 + (future_jump if minute >= 80 else 0)
            rows.append(IndexTick("NSE-BANKNIFTY", exchange_at, received_at, price))
    return rows


class TrainingDatasetTests(unittest.TestCase):
    def test_empty_or_wrong_source_never_creates_training_rows(self):
        self.assertEqual(build_training_research_rows([])["rows"], [])
        with self.assertRaises(ValueError):
            build_training_research_rows(["official daily OHLC is not an intraday tick"])
        with TemporaryDirectory() as folder:
            destination = Path(folder) / "rows.csv"
            with self.assertRaisesRegex(ValueError, "No continuous"):
                export_training_research_csv(Path(folder) / "missing.sqlite3", destination)
            self.assertFalse(destination.exists())

    def test_future_price_changes_label_but_not_past_features(self):
        day = date(2026, 10, 5)
        base = build_training_research_rows(recorded_day(day))
        changed = build_training_research_rows(recorded_day(day, future_jump=100))
        self.assertEqual(base["selected_origins"], 2)
        self.assertEqual(base["structural_audit"]["checked_rows"], 6)
        self.assertEqual(len(base["rows"]), 6)
        first_base, first_changed = base["rows"][1], changed["rows"][1]
        self.assertEqual(first_base["horizon_minutes"], 30)
        self.assertEqual(first_base["label_status"], "ready")
        self.assertEqual(first_base["partition"], "unassigned_too_few_dates")
        self.assertEqual(first_base["origin_close_age_seconds"], 5.0)
        self.assertEqual(first_base["target_close_age_seconds"], 5.0)
        self.assertTrue(first_base["target_at_utc"].endswith("+00:00"))
        self.assertEqual({name: first_base[name] for name in FEATURES},
                         {name: first_changed[name] for name in FEATURES})
        self.assertEqual({name: first_base[name] for name in ADDITIONAL_INDICATORS},
                         {name: first_changed[name] for name in ADDITIONAL_INDICATORS})
        self.assertIn("descriptive-indicators", first_base["indicator_formula_version"])
        self.assertNotEqual(first_base["target_return_pct"],
                            first_changed["target_return_pct"])
        self.assertEqual(changed["rows"][-1]["label_status"], "missing_data")
        self.assertIsNone(changed["rows"][-1]["target_return_pct"])

    def test_expiry_and_price_movement_are_point_in_time(self):
        day = date(2026, 10, 27)
        catalog = ("exchange,segment,underlying_symbol,instrument_type,expiry_date\n"
                   "NSE,FNO,BANKNIFTY,CE,2026-10-27\n"
                   "NSE,FNO,BANKNIFTY,PE,2026-10-27\n")
        with TemporaryDirectory() as folder:
            ref = record_expiry_reference(Path(folder) / "archive.sqlite3", catalog,
                datetime(2026, 10, 27, 9, 0, tzinfo=IST))
        report = build_training_research_rows(recorded_day(day), expiry_references=(ref,))
        first = report["rows"][0]
        self.assertTrue(first["is_expiry_day"])
        self.assertEqual(first["calendar_days_to_expiry"], 0)
        self.assertEqual(first["price_change_15m_points"], 30)
        self.assertEqual(first["movement_efficiency_15m_pct"], 100)
        future_ref = dict(ref, observed_at_utc=datetime(2026, 10, 27, 11, 0,
            tzinfo=IST).isoformat())
        future_report = build_training_research_rows(recorded_day(day),
            expiry_references=(future_ref,))
        self.assertIsNone(future_report["rows"][0]["is_expiry_day"])
        self.assertTrue(future_report["rows"][3]["is_expiry_day"])

    def test_manual_macro_event_never_enters_before_first_seen(self):
        raw = ("kind,subject,title,source_url,published_at,event_at\n"
               "policy_event,RBI,Test announcement,https://www.rbi.org.in/example,"
               "2026-10-04T08:00:00+05:30,2026-10-05T10:30:00+05:30\n").encode()
        event = parse_event_csv(raw, datetime(2026, 10, 5, 11, 0, tzinfo=IST))[0]
        report = build_training_research_rows(recorded_day(date(2026, 10, 5)),
                                              events=(event,))
        self.assertEqual(report["rows"][0]["event_context_status"], "no_event_source")
        self.assertIsNone(report["rows"][0]["known_recent_macro_events"])
        self.assertEqual(report["rows"][3]["known_recent_macro_events"], 1)
        self.assertFalse(report["rows"][3]["event_context_forecast_eligible"])

    def test_gap_and_late_receipts_reduce_available_examples(self):
        day = date(2026, 10, 5)
        gapped = build_training_research_rows(recorded_day(day, omit=75))
        self.assertEqual(gapped["selected_origins"], 1)
        self.assertEqual({row["label_status"] for row in gapped["rows"]}, {"missing_data"})
        late = build_training_research_rows(recorded_day(day, late_minute=0))
        self.assertEqual(late["excluded_late_or_clock_invalid_ticks"], 12)
        self.assertEqual(late["selected_origins"], 1)

    def test_weak_sampled_endpoint_never_becomes_a_ready_label(self):
        day = date(2026, 10, 5)
        report = build_training_research_rows(recorded_day(day, weak_edge_minute=75))
        self.assertEqual(report["selected_origins"], 2)
        self.assertEqual(report["weak_target_labels"], 1)
        self.assertEqual(report["rows"][0]["horizon_minutes"], 15)
        self.assertEqual(report["rows"][0]["target_close_age_seconds"], 10.0)
        self.assertEqual(report["rows"][0]["label_status"], "weak_target_edge")
        self.assertIsNone(report["rows"][0]["target_return_pct"])
        self.assertEqual(report["rows"][1]["label_status"], "ready")

    def test_weak_origin_is_skipped_without_consulting_future_labels(self):
        day = date(2026, 10, 5)
        report = build_training_research_rows(recorded_day(day, weak_edge_minute=60))
        self.assertEqual(report["excluded_weak_origins"], 1)
        self.assertEqual(report["selected_origins"], 1)
        self.assertEqual(report["rows"][0]["origin_close_age_seconds"], 5.0)
        self.assertEqual(datetime.fromisoformat(report["rows"][0]["origin_at_utc"]).astimezone(IST).minute, 17)

    def test_whole_date_split_and_excel_export(self):
        ticks = [tick for day in (5, 6, 7, 8, 9)
                 for tick in recorded_day(date(2026, 10, day))]
        report = build_training_research_rows(ticks)
        self.assertEqual(report["split_status"], "ready")
        partitions = {row["session_date_ist"]: row["partition"] for row in report["rows"]}
        self.assertEqual(partitions["2026-10-09"], "holdout")
        self.assertEqual(partitions["2026-10-05"], "development")
        with TemporaryDirectory() as folder:
            database = Path(folder) / "archive.sqlite3"
            database.write_bytes(b"archive must remain untouched")
            destination = Path(folder) / "research.csv"
            with patch("banknifty.training_dataset.load_index_ticks", return_value=ticks), \
                 patch("banknifty.training_dataset.load_expiry_references", return_value=[]), \
                 patch("banknifty.training_dataset.load_known_events", return_value=[]):
                exported = export_training_research_csv(database, destination)
                with self.assertRaisesRegex(ValueError, "must not overwrite"):
                    export_training_research_csv(database, database)
            self.assertEqual(database.read_bytes(), b"archive must remain untouched")
            self.assertEqual(len(exported["rows"]), len(report["rows"]))
            with destination.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), len(report["rows"]))
            self.assertEqual(rows[-1]["partition"], "holdout")
            self.assertIn("not_a_trained", rows[-1]["interpretation"])


if __name__ == "__main__":
    unittest.main()

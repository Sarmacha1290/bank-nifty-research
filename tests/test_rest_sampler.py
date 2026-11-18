"""The REST fallback must stay separate from exchange-timestamped feed data."""

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from banknifty.rest_sampler import (save_rest_observation, sample_rest_prices,
                                    summarize_rest_observations,
                                    audit_rest_receipt_integrity,
                                    summarize_rest_session_coverage,
                                    export_rest_observations_csv)
from banknifty.groww import DataAccessError
from banknifty.training_readiness import assess_training_readiness


def snapshot(value=55000.0):
    return {"source": "Groww", "symbol": "NSE-BANKNIFTY",
            "received_at": datetime.now(timezone.utc).isoformat(), "value": value,
            "exchange_timestamp": None, "freshness": "unverified"}


class RestSamplerTests(unittest.TestCase):
    def test_saves_receipt_only_rows(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            save_rest_observation(database, snapshot())
            with closing(sqlite3.connect(database)) as connection:
                row = connection.execute("SELECT value,time_basis FROM rest_price_observations").fetchone()
                self.assertEqual(row, (55000.0, "laptop_receipt_no_exchange_timestamp"))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name='events'").fetchone()[0], 0)
            readiness = assess_training_readiness(database)
            self.assertEqual(readiness["groww_index_ticks"], 0)
            self.assertEqual(readiness["trained_forecast_models"], 0)
            self.assertFalse(readiness["intraday_model_ready"])

    def test_rejects_claimed_exchange_timestamp(self):
        with TemporaryDirectory() as folder:
            bad = snapshot()
            bad["exchange_timestamp"] = "2026-10-05T04:00:00+00:00"
            with self.assertRaises(ValueError):
                save_rest_observation(Path(folder) / "market.sqlite3", bad)

    def test_bounded_sampling_records_two_observations(self):
        class Stop:
            def is_set(self):
                return False
            def wait(self, seconds):
                elapsed[0] += 30
        class Client:
            def index_snapshot(self, instrument):
                return snapshot()
        elapsed = [0]
        with TemporaryDirectory() as folder, patch(
                "banknifty.rest_sampler.regular_market_status", return_value="regular_session"):
            result = sample_rest_prices(Client(), {"groww_symbol": "NSE-BANKNIFTY"},
                Path(folder) / "market.sqlite3", minutes=1, stop_event=Stop(),
                monotonic=lambda: elapsed[0])
            self.assertEqual(result, {"status": "completed", "saved": 2})

    def test_access_failure_does_not_create_fake_prices(self):
        class Client:
            def index_snapshot(self, instrument):
                raise DataAccessError("Groww rejected access")
        class Stop:
            def is_set(self):
                return False
        with TemporaryDirectory() as folder, patch(
                "banknifty.rest_sampler.regular_market_status", return_value="regular_session"):
            database = Path(folder) / "market.sqlite3"
            result = sample_rest_prices(Client(), {"groww_symbol": "NSE-BANKNIFTY"},
                database, minutes=1, stop_event=Stop())
            self.assertEqual(result, {"status": "access_error", "saved": 0})
            self.assertFalse(database.exists())

    def test_late_response_is_discarded_without_losing_committed_samples(self):
        for boundary, expected_status in (("stop", "stopped"),
                                          ("deadline", "completed"),
                                          ("close", "market_closed")):
            with self.subTest(boundary=boundary), TemporaryDirectory() as folder:
                elapsed = [0.0]
                wall_clock = [datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)]
                stopped = [False]
                received = []
                calls = []

                class Stop:
                    def is_set(self):
                        return stopped[0]
                    def wait(self, seconds):
                        elapsed[0] += seconds

                class Client:
                    def index_snapshot(self, instrument):
                        calls.append(instrument)
                        if len(calls) == 2:
                            if boundary == "stop":
                                stopped[0] = True
                            elif boundary == "deadline":
                                elapsed[0] = 60.0
                            else:
                                wall_clock[0] = datetime(2026, 10, 5, 10, 0,
                                                        tzinfo=timezone.utc)
                        item = snapshot(55000 + len(calls))
                        item["received_at"] = wall_clock[0].isoformat()
                        return item

                database = Path(folder) / "market.sqlite3"
                result = sample_rest_prices(Client(), {"groww_symbol": "NSE-BANKNIFTY"},
                    database, minutes=1, stop_event=Stop(),
                    monotonic=lambda: elapsed[0], now=lambda: wall_clock[0],
                    on_sample=lambda *args: received.append(args))
                self.assertEqual(result, {"status": expected_status, "saved": 1})
                self.assertEqual(len(calls), 2)
                self.assertEqual(len(received), 1)
                review = summarize_rest_observations(database)
                self.assertEqual(review["count"], 1)
                self.assertEqual(review["observed_low"], 55001)

    def test_boundary_during_sqlite_lock_rolls_back_new_observation(self):
        cases = [(boundary, expected, lock_kind)
                 for boundary, expected in (("stop", "stopped"),
                                             ("deadline", "completed"),
                                             ("close", "market_closed"))
                 for lock_kind in ("reader", "writer")]
        for boundary, expected_status, lock_kind in cases:
            with self.subTest(boundary=boundary, lock=lock_kind), TemporaryDirectory() as folder:
                database = Path(folder) / "market.sqlite3"
                save_rest_observation(database, snapshot(54999))
                elapsed = [0.0]
                wall_clock = [datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)]
                stop = Event()
                write_started = Event()
                received = []
                real_connect = sqlite3.connect

                class WriteObservedConnection(sqlite3.Connection):
                    def execute(self, sql, *args, **kwargs):
                        if sql == "BEGIN EXCLUSIVE":
                            write_started.set()
                        return super().execute(sql, *args, **kwargs)

                class Client:
                    def index_snapshot(self, instrument):
                        return snapshot(55000)

                def connect(*args, **kwargs):
                    return real_connect(*args, factory=WriteObservedConnection, **kwargs)

                with closing(real_connect(database)) as writer:
                    if lock_kind == "writer":
                        writer.execute("BEGIN IMMEDIATE")
                    else:
                        writer.execute("BEGIN")
                        writer.execute("SELECT * FROM rest_price_observations").fetchall()
                    with patch("banknifty.rest_sampler.sqlite3.connect", side_effect=connect), \
                            ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(sample_rest_prices, Client(),
                            {"groww_symbol": "NSE-BANKNIFTY"}, database, minutes=1,
                            stop_event=stop, monotonic=lambda: elapsed[0],
                            now=lambda: wall_clock[0],
                            on_sample=lambda *args: received.append(args))
                        try:
                            self.assertTrue(write_started.wait(2), "Sampler never attempted its write")
                            if boundary == "stop":
                                stop.set()
                            elif boundary == "deadline":
                                elapsed[0] = 60.0
                            else:
                                wall_clock[0] = datetime(2026, 10, 5, 10, 0,
                                                        tzinfo=timezone.utc)
                        finally:
                            writer.rollback()
                        result = future.result(timeout=5)
                self.assertEqual(result, {"status": expected_status, "saved": 0})
                self.assertEqual(received, [])
                review = summarize_rest_observations(database)
                self.assertEqual(review["count"], 1)
                self.assertEqual(review["observed_high"], 54999)

    def test_stop_after_insert_rolls_back_before_commit(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            save_rest_observation(database, snapshot(54999))
            stop = Event()
            real_connect = sqlite3.connect

            class StopAfterInsertConnection(sqlite3.Connection):
                def execute(self, sql, *args, **kwargs):
                    result = super().execute(sql, *args, **kwargs)
                    if sql.lstrip().startswith("INSERT INTO rest_price_observations"):
                        stop.set()
                    return result

            class Client:
                def index_snapshot(self, instrument):
                    return snapshot(55000)

            def connect(*args, **kwargs):
                return real_connect(*args, factory=StopAfterInsertConnection, **kwargs)

            with patch("banknifty.rest_sampler.sqlite3.connect", side_effect=connect):
                result = sample_rest_prices(Client(), {"groww_symbol": "NSE-BANKNIFTY"},
                    database, minutes=1, stop_event=stop, monotonic=lambda: 0,
                    now=lambda: datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc))
            self.assertEqual(result, {"status": "stopped", "saved": 0})
            review = summarize_rest_observations(database)
            self.assertEqual(review["count"], 1)
            self.assertEqual(review["observed_high"], 54999)

    def test_rejects_nonfinite_or_boolean_capture_bounds_before_request(self):
        class Client:
            def index_snapshot(self, instrument):
                raise AssertionError("Invalid bounds must not start a request")

        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            invalid = [(True, 5), (1.0, 5), (float("nan"), 5), (1, True),
                       (1, float("nan")), (1, float("inf")), (1, "5"), (1, 4.9)]
            for minutes, interval in invalid:
                with self.subTest(minutes=minutes, interval=interval), self.assertRaises(ValueError):
                    sample_rest_prices(Client(), {"groww_symbol": "NSE-BANKNIFTY"}, database,
                        minutes=minutes, interval_seconds=interval, stop_event=Event())
            self.assertFalse(database.exists())

    def test_review_and_excel_export_keep_receipt_provenance(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            first, second = snapshot(55000), snapshot(55000)
            first["received_at"] = "2026-10-05T04:00:00+00:00"
            second["received_at"] = "2026-10-05T04:00:20+00:00"
            save_rest_observation(database, first)
            save_rest_observation(database, second)
            review = summarize_rest_observations(database)
            self.assertEqual(review["count"], 2)
            self.assertEqual(review["repeated_prices"], 1)
            self.assertEqual(review["gaps_over_15_seconds"], 1)
            self.assertEqual(review["largest_gap_seconds"], 20)
            destination = Path(folder) / "samples.csv"
            self.assertEqual(export_rest_observations_csv(database, destination), 2)
            csv_text = destination.read_text(encoding="utf-8-sig")
            self.assertIn("laptop_received_at_utc", csv_text)
            self.assertIn("unverified", csv_text)
            self.assertNotIn("exchange_at_utc", csv_text)
            with self.assertRaises(ValueError):
                export_rest_observations_csv(database, database)

    def test_receipt_audit_flags_clock_regression_and_large_jump(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            for stamp, value in (
                    ("2026-10-05T04:00:00+00:00", 55000),
                    ("2026-10-05T03:59:59+00:00", 55000),
                    ("2026-10-05T04:00:04+00:00", 60000),
                    ("2026-10-05T04:00:30+00:00", 60000)):
                item = snapshot(value)
                item["received_at"] = stamp
                save_rest_observation(database, item)
            audit = audit_rest_receipt_integrity(database)
            self.assertEqual(audit["clock_regressions"], 1)
            self.assertEqual(audit["large_move_review_flags"], 1)
            self.assertEqual(audit["within_day_gaps_over_15_seconds"], 1)
            self.assertEqual(audit["outside_regular_session"], 0)
            self.assertEqual(audit["exchange_freshness"], "unverified")

    def test_session_coverage_counts_quadrants_without_creating_candles(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            for second in (1, 16, 31, 46):
                item = snapshot()
                item["received_at"] = f"2026-10-05T04:00:{second:02d}+00:00"
                save_rest_observation(database, item)
            item = snapshot()
            item["received_at"] = "2026-10-05T04:01:00+00:00"
            save_rest_observation(database, item)
            coverage = summarize_rest_session_coverage(database)
            self.assertEqual(coverage, [{"date_ist": "2026-10-05",
                "regular_minutes": 375, "minutes_with_receipts": 2,
                "four_quadrant_minutes": 1, "minutes_without_receipts": 373,
                "receipt_samples": 5, "exchange_freshness": "unverified"}])
            readiness = assess_training_readiness(database)
            self.assertFalse(readiness["intraday_model_ready"])

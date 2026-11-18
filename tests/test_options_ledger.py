"""Offline acceptance tests; all execution reports and contracts are synthetic."""
import csv
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from banknifty.options_ledger import OptionsLedger, LedgerError, DuplicateEvent, IntegrityError, validate_ledger


T0 = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)


def candidate(*, signal_id="s1", quantity=20, option_type="CALL", credit=False):
    low = {"contract_id": "SYNTHETIC-LOW", "underlying": "SYNTHETIC", "exchange": "SYNTHETIC",
           "option_type": option_type, "strike": "1000", "lot_size": 10, "tick_size": "0.05",
           "expiry_at": "2026-02-01T10:00:00+00:00", "effective_from": "2026-01-01T00:00:00+00:00",
           "known_at": "2026-01-01T00:00:00+00:00", "listed": True,
           "provenance": {"source": "synthetic_fixture", "raw": "fixture-only"}}
    high = dict(low, contract_id="SYNTHETIC-HIGH", strike="1100")
    buy_low = (option_type == "CALL") != credit
    return {"signal_id": signal_id, "status": "candidate", "as_of": T0.isoformat(),
            "quantity": quantity, "strategy": "credit_spread" if credit else "debit_spread",
            "input_fingerprint": "synthetic-original-input", "label": "synthetic",
            "legs": [{"contract": low, "side": "BUY" if buy_low else "SELL", "price": "80", "ratio": 1},
                     {"contract": high, "side": "SELL" if buy_low else "BUY", "price": "40", "ratio": 1}]}


class OptionsLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "options.sqlite3"
        self.ledger = OptionsLedger(self.path)
        self.signal = candidate()
        self.ledger.append_signal("s1", self.signal, recorded_at=T0)

    def fill(self, event_id, contract="SYNTHETIC-LOW", side="BUY", quantity=20, price="80", second=1, fees="0"):
        return self.ledger.record_fill(event_id, "s1", contract, side, quantity, price,
            T0 + timedelta(seconds=second), T0 + timedelta(seconds=second), fees=fees)

    def test_signal_freezes_all_inputs_and_creates_no_fills(self):
        self.signal["legs"][0]["price"] = "9999"
        stored = self.ledger.list_signals()[0]
        self.assertEqual(stored["legs"][0]["price"], "80")
        stored["legs"][0]["price"] = "77"
        self.assertEqual(self.ledger.list_signals()[0]["legs"][0]["price"], "80")
        self.assertEqual(self.ledger.exposure("s1")["status"], "unfilled")
        self.assertEqual([event["kind"] for event in self.ledger.events()], ["signal"])

    def test_strict_ledger_validation_is_readonly_and_checks_all_events(self):
        before = self.path.read_bytes()
        with patch("banknifty.options_ledger.sqlite3.connect", wraps=sqlite3.connect) as connect:
            result = validate_ledger(self.path)
        self.assertTrue(result["valid"])
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["last_sha256"], self.ledger.events()[-1]["sha256"])
        self.assertTrue(all(call.kwargs.get("uri") and "mode=ro" in call.args[0] for call in connect.call_args_list))
        self.assertEqual(self.path.read_bytes(), before)

    def test_strict_validation_rejects_private_column_in_empty_ledger(self):
        path = Path(self.temp.name) / "empty.sqlite3"
        OptionsLedger(path)
        self.assertEqual(validate_ledger(path)["events"], 0)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("ALTER TABLE options_events ADD COLUMN unrelated_private TEXT DEFAULT 'DUMMY-PRIVATE-FIXTURE'")
        before = path.read_bytes()
        with self.assertRaisesRegex(IntegrityError, "fixed application schema"):
            validate_ledger(path)
        self.assertEqual(path.read_bytes(), before)

    def test_strict_validation_rejects_changed_trigger_and_extra_objects(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TRIGGER options_events_no_update")
            db.execute("CREATE TRIGGER options_events_no_update BEFORE UPDATE ON options_events BEGIN SELECT 1; END")
        with self.assertRaisesRegex(IntegrityError, "fixed application schema"):
            validate_ledger(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE VIEW unrelated_private AS SELECT 'DUMMY-PRIVATE-FIXTURE'")
        with self.assertRaisesRegex(IntegrityError, "unrelated schema objects"):
            validate_ledger(self.path)

    def test_strict_validation_bounds_before_reading_event_payloads(self):
        with patch("banknifty.options_ledger._LEDGER_VALIDATION_EVENTS", 0), patch.object(OptionsLedger, "_read") as read:
            with self.assertRaisesRegex(LedgerError, "event limit"):
                validate_ledger(self.path)
            read.assert_not_called()
        with patch("banknifty.options_ledger._LEDGER_VALIDATION_BYTES", 1):
            with self.assertRaisesRegex(LedgerError, "64 MiB"):
                validate_ledger(self.path)

    def test_identical_signal_click_is_idempotent_after_restart(self):
        restarted = OptionsLedger(self.path)
        first = restarted.append_signal("s1", candidate(), recorded_at=T0 + timedelta(seconds=1))
        self.assertEqual(first["sequence"], 1)
        self.assertEqual(len(restarted.events()), 1)
        changed = candidate()
        changed["input_fingerprint"] = "future-information"
        with self.assertRaises(DuplicateEvent):
            restarted.append_signal("s1", changed, recorded_at=T0 + timedelta(seconds=1))

    def test_signal_guard_runs_inside_transaction_and_rolls_back_rejection(self):
        class StopPublication(Exception):
            pass
        calls = []
        def guard():
            with closing(sqlite3.connect(self.path, timeout=0)) as contender:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    contender.execute("BEGIN IMMEDIATE")
            calls.append("checked")
            raise StopPublication()
        with self.assertRaises(StopPublication):
            self.ledger.append_signal("s2", candidate(signal_id="s2"), recorded_at=T0, pre_commit=guard)
        self.assertEqual(calls, ["checked"])
        self.assertEqual(len(OptionsLedger(self.path, read_only=True).events()), 1)
        self.assertEqual(self.ledger.append_signal("s2", candidate(signal_id="s2"), recorded_at=T0)["sequence"], 2)

    def test_signal_guard_runs_before_insert_and_commit_only_for_new_insert(self):
        calls = []
        guard = lambda: calls.append("checked")
        for _ in range(2):
            self.ledger.append_signal("s2", candidate(signal_id="s2"), recorded_at=T0, pre_commit=guard)
        self.assertEqual(calls, ["checked", "checked"])
        self.assertEqual(len(self.ledger.events()), 2)
        with self.assertRaisesRegex(LedgerError, "callable"):
            self.ledger.append_signal("s3", candidate(signal_id="s3"), recorded_at=T0, pre_commit=False)

    def test_guard_reserves_against_new_readers_until_commit(self):
        calls = []
        def guard():
            with closing(sqlite3.connect(self.path, timeout=0)) as reader:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                    reader.execute('SELECT * FROM options_events').fetchall()
            calls.append('exclusive')
        self.ledger.append_signal('s2', candidate(signal_id='s2'), recorded_at=T0, pre_commit=guard)
        self.assertEqual(calls, ['exclusive', 'exclusive'])
        self.assertEqual(len(self.ledger.events()), 2)

    def test_guard_rejection_after_insert_rolls_back_entire_event(self):
        class StopPublication(Exception):
            pass
        calls = []
        def guard():
            calls.append('checked')
            if len(calls) == 2:
                raise StopPublication()
        with self.assertRaises(StopPublication):
            self.ledger.append_signal('s2', candidate(signal_id='s2'), recorded_at=T0, pre_commit=guard)
        self.assertEqual(calls, ['checked', 'checked'])
        self.assertEqual(len(self.ledger.events()), 1)
        self.assertEqual(self.ledger.append_signal('s2', candidate(signal_id='s2'), recorded_at=T0)['sequence'], 2)

    def test_recording_clock_has_exclusive_lock_and_deduplicates_without_resampling(self):
        calls = []
        def clock():
            with closing(sqlite3.connect(self.path, timeout=0)) as reader:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                    reader.execute('SELECT * FROM options_events').fetchall()
            calls.append('clock')
            return T0 + timedelta(seconds=2)
        first = self.ledger.append_signal('s2', candidate(signal_id='s2'), recorded_at=T0, recorded_at_clock=clock)
        repeated = self.ledger.append_signal('s2', candidate(signal_id='s2'), recorded_at=T0, recorded_at_clock=clock)
        self.assertEqual(first, repeated)
        self.assertEqual(calls, ['clock'])
        self.assertEqual(first['recorded_at'], (T0 + timedelta(seconds=2)).isoformat(timespec='microseconds'))
        self.assertEqual(first['payload']['signal']['as_of'], T0.isoformat())

    def test_recording_clock_rejects_invalid_or_regressing_time_without_event(self):
        for value in (None, T0.isoformat(), T0.replace(tzinfo=None), T0 - timedelta(seconds=1)):
            with self.subTest(value=value), self.assertRaises(LedgerError):
                self.ledger.append_signal('s2', candidate(signal_id='s2'), recorded_at=T0,
                                          recorded_at_clock=lambda: value)
        with self.assertRaisesRegex(LedgerError, 'callable'):
            self.ledger.append_signal('s2', candidate(signal_id='s2'), recorded_at=T0, recorded_at_clock=False)
        self.assertEqual(len(self.ledger.events()), 1)

    def test_guarded_append_is_compatible_with_wal_reader_snapshot(self):
        with closing(sqlite3.connect(self.path)) as reader:
            self.assertEqual(reader.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            reader.execute('BEGIN')
            self.assertEqual(len(reader.execute('SELECT * FROM options_events').fetchall()), 1)
            calls = []
            self.ledger.append_signal('s2', candidate(signal_id='s2'), recorded_at=T0,
                                      pre_commit=lambda: calls.append('checked'))
            self.assertEqual(calls, ['checked', 'checked'])
            self.assertEqual(len(reader.execute('SELECT * FROM options_events').fetchall()), 1)
            reader.rollback()
            self.assertEqual(len(reader.execute('SELECT * FROM options_events').fetchall()), 2)

    def test_concurrent_repeated_clicks_commit_one_signal_and_one_fill(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            outcomes = list(workers.map(lambda _: self.ledger.append_signal("s1", candidate(), recorded_at=T0), range(4)))
        self.assertEqual({item["sha256"] for item in outcomes}, {self.ledger.events()[0]["sha256"]})
        def fill_once(index):
            try:
                self.fill("click-" + str(index), quantity=10)
                return "saved"
            except DuplicateEvent:
                return "duplicate"
        with ThreadPoolExecutor(max_workers=4) as workers:
            outcomes = list(workers.map(fill_once, range(4)))
        self.assertEqual(outcomes.count("saved"), 1)
        self.assertEqual(outcomes.count("duplicate"), 3)
        self.assertEqual(len(self.ledger.events()), 2)

    def test_short_first_has_no_spread_bound(self):
        self.fill("short", "SYNTHETIC-HIGH", "SELL", price="40")
        result = self.ledger.exposure("s1")
        self.assertEqual(result["status"], "unhedged_short")
        self.assertFalse(result["contractual_bound_valid"])
        self.assertIn("UNHEDGED", " ".join(result["warnings"]))

    def test_hedge_close_first_restores_unhedged_warning(self):
        self.fill("long")
        self.fill("short", "SYNTHETIC-HIGH", "SELL", price="40", second=2)
        self.assertTrue(self.ledger.exposure("s1")["contractual_bound_valid"])
        self.fill("close-hedge", side="SELL", price="70", second=3)
        result = self.ledger.exposure("s1")
        self.assertEqual(result["status"], "unhedged_short")
        self.assertFalse(result["contractual_bound_valid"])
        self.fill("close-short", "SYNTHETIC-HIGH", "BUY", price="30", second=4)
        self.assertEqual(self.ledger.exposure("s1")["status"], "closed")

    def test_closed_manual_execution_pnl_is_cash_flow_less_recorded_fees(self):
        self.fill("long", price="80", fees="5")
        self.fill("close", side="SELL", price="75", second=2, fees="7")
        report = self.ledger.exposure("s1")
        self.assertEqual(report["realized_pnl_basis"], "user_reported_closed_execution_not_broker_verified")
        self.assertEqual(Decimal(report["realized_gross_pnl"]), Decimal(-100))
        self.assertEqual(Decimal(report["realized_net_pnl"]), Decimal(-112))
        self.assertIsNone(report["actual_max_loss_with_recorded_fees"])
        self.assertEqual(report["signal_label"], "synthetic")

    def test_closed_unknown_fees_leave_net_pnl_unavailable(self):
        self.fill("long", price="80", fees=None)
        self.fill("close", side="SELL", price="75", second=2, fees="7")
        report = self.ledger.exposure("s1")
        self.assertEqual(Decimal(report["realized_gross_pnl"]), Decimal(-100))
        self.assertIsNone(report["realized_net_pnl"])

    def test_correction_changes_closed_pnl_without_rewriting_original_fill(self):
        self.fill("long", price="80", fees="5")
        original = self.fill("close", side="SELL", price="75", second=2, fees="7")
        self.ledger.correct_fill("price-correction", "close", dict(original["payload"], price="85"),
            recorded_at=T0 + timedelta(seconds=3), reason="Corrected manual execution price")
        restarted = OptionsLedger(self.path)
        report = restarted.exposure("s1")
        self.assertEqual(Decimal(report["realized_gross_pnl"]), Decimal(100))
        self.assertEqual(Decimal(report["realized_net_pnl"]), Decimal(88))
        self.assertEqual(restarted.events()[2], original)

    def test_no_realized_total_for_unfilled_open_or_partial_positions(self):
        self.assertIsNone(self.ledger.exposure("s1")["realized_gross_pnl"])
        self.fill("long", price="80", fees="5")
        self.assertIsNone(self.ledger.exposure("s1")["realized_net_pnl"])
        self.fill("half-close", side="SELL", quantity=10, price="75", second=2, fees="7")
        report = self.ledger.exposure("s1")
        self.assertIsNone(report["realized_gross_pnl"])
        self.assertIsNone(report["realized_net_pnl"])
        self.assertEqual(report["realized_pnl_basis"], "unavailable_until_fully_closed")

    def test_partial_matching_does_not_adopt_full_position_bound(self):
        self.fill("half-long", quantity=10)
        self.fill("half-short", "SYNTHETIC-HIGH", "SELL", quantity=10, price="40", second=2)
        report = self.ledger.exposure("s1")
        self.assertEqual(report["status"], "hedged_open")
        self.assertFalse(report["complete_planned_position"])
        self.assertFalse(report["contractual_bound_valid"])

    def test_surplus_long_units_do_not_report_covered_short_as_unhedged(self):
        self.fill("long", quantity=20)
        self.fill("short", "SYNTHETIC-HIGH", "SELL", quantity=10, price="40", second=2)
        report = self.ledger.exposure("s1")
        self.assertEqual(report["matched_units"], 10)
        self.assertEqual(report["unmatched_long_units"], 10)
        self.assertEqual(report["unhedged_short_units"], 0)
        self.assertNotEqual(report["status"], "unhedged_short")
        self.assertFalse(report["contractual_bound_valid"])

    def test_only_short_units_exceeding_hedge_are_unhedged(self):
        self.fill("long", quantity=10)
        self.fill("short", "SYNTHETIC-HIGH", "SELL", quantity=20, price="40", second=2)
        report = self.ledger.exposure("s1")
        self.assertEqual(report["matched_units"], 10)
        self.assertEqual(report["unmatched_long_units"], 0)
        self.assertEqual(report["unhedged_short_units"], 10)
        self.assertEqual(report["status"], "unhedged_short")
        self.assertFalse(report["contractual_bound_valid"])

    def test_unknown_fees_are_not_silently_zero(self):
        self.fill("long", fees=None)
        self.fill("short", "SYNTHETIC-HIGH", "SELL", price="40", second=2, fees=None)
        self.assertFalse(self.ledger.exposure("s1")["fees_known"])
        self.assertFalse(self.ledger.exposure("s1")["contractual_bound_valid"])

    def test_long_call_and_put_bounds_and_execution_labels(self):
        for right, expected_profit in (("CALL", None), ("PUT", Decimal(20495))):
            with self.subTest(right=right):
                signal = candidate(signal_id=right, option_type=right)
                signal["legs"] = [leg for leg in signal["legs"] if leg["side"] == "BUY"]
                self.ledger.append_signal(right, signal, recorded_at=T0)
                self.ledger.record_fill(right, right, signal["legs"][0]["contract"]["contract_id"], "BUY", 20, "75", T0, T0, fees="5")
                report = self.ledger.exposure(right)
                self.assertEqual(Decimal(report["actual_max_loss_with_recorded_fees"]), Decimal(1505))
                self.assertEqual(None if report["actual_max_profit_with_recorded_fees"] is None else Decimal(report["actual_max_profit_with_recorded_fees"]), expected_profit)
                self.assertEqual(report["execution_basis"], "user_reported_manual_fills_not_broker_verified")
                self.assertEqual(report["payoff_basis"], "contractual_expiry_scenario_not_realized_pnl")

    def test_actual_entry_prices_replace_planned_prices_in_bounds(self):
        self.fill("long", price="90", fees="5")
        self.fill("short", "SYNTHETIC-HIGH", "SELL", price="40", second=2, fees="5")
        report = self.ledger.exposure("s1")
        self.assertEqual(Decimal(report["actual_max_loss_with_recorded_fees"]), Decimal(1010))
        self.assertEqual(Decimal(report["actual_max_profit_with_recorded_fees"]), Decimal(990))
        self.assertFalse(report["planned_price_bound_applies"])

    def test_call_and_put_debit_credit_acceptance_bounds(self):
        for right in ("CALL", "PUT"):
            for credit in (False, True):
                with self.subTest(right=right, credit=credit):
                    sid = right + str(credit)
                    signal = candidate(signal_id=sid, option_type=right, credit=credit)
                    self.ledger.append_signal(sid, signal, recorded_at=T0)
                    for index, leg in enumerate(signal["legs"]):
                        price = ("50" if leg["side"] == "SELL" else "25") if credit else ("60" if leg["side"] == "BUY" else "20")
                        self.ledger.record_fill(sid + str(index), sid, leg["contract"]["contract_id"], leg["side"], 20, price, T0, T0, fees="5")
                    result = self.ledger.exposure(sid)
                    expected_loss, expected_profit = (1510, 490) if credit else (810, 1190)
                    self.assertEqual(Decimal(result["actual_max_loss_with_recorded_fees"]), expected_loss)
                    self.assertEqual(Decimal(result["actual_max_profit_with_recorded_fees"]), expected_profit)

    def test_duplicate_ids_duplicate_content_and_overclose_rejected(self):
        self.fill("long", quantity=10)
        for event_id, second, qty in [("long", 2, 10), ("other", 1, 10)]:
            with self.assertRaises(DuplicateEvent):
                self.fill(event_id, second=second, quantity=qty)
        with self.assertRaisesRegex(LedgerError, "over-close"):
            self.fill("bad-close", side="SELL", quantity=20, second=2)
        self.assertEqual(len(self.ledger.events()), 2)

    def test_same_fill_with_different_fees_is_still_duplicate(self):
        self.fill("long", quantity=10)
        with self.assertRaises(DuplicateEvent):
            self.fill("fee-bypass", quantity=10, fees="12")

    def test_external_reference_deduplicates_across_signals(self):
        self.ledger.record_fill("first", "s1", "SYNTHETIC-LOW", "BUY", 10, "80", T0, T0, "broker-report-reference")
        with self.assertRaises(DuplicateEvent):
            self.ledger.record_fill("second", "s1", "SYNTHETIC-LOW", "BUY", 10, "81", T0, T0, "broker-report-reference")

    def test_correction_preserves_original_and_restart_hash_chain(self):
        original = self.fill("long")
        replacement = dict(original["payload"], price="75", quantity=10)
        self.ledger.correct_fill("correction", "long", replacement, recorded_at=T0 + timedelta(seconds=2), reason="Corrected transcription")
        restarted = OptionsLedger(self.path)
        events = restarted.events()
        self.assertEqual(events[1], original)
        self.assertEqual(events[2]["kind"], "correction")
        self.assertEqual(restarted.exposure("s1")["positions"]["SYNTHETIC-LOW"], 10)
        restarted.correct_fill("void", "long", None, recorded_at=T0 + timedelta(seconds=3), reason="Duplicate broker entry")
        self.assertEqual(restarted.exposure("s1")["status"], "unfilled")

    def test_void_opening_with_later_close_rejected_atomically(self):
        self.fill("long")
        self.fill("close", side="SELL", second=2)
        with self.assertRaisesRegex(LedgerError, "over-close"):
            self.ledger.correct_fill("void", "long", None, recorded_at=T0 + timedelta(seconds=3), reason="Mistaken original")
        self.assertEqual(len(self.ledger.events()), 3)

    def test_correction_cannot_rewrite_signal_or_move_fill(self):
        original = self.fill("long")
        with self.assertRaisesRegex(LedgerError, "original manual"):
            self.ledger.correct_fill("bad", "signal:s1", None, recorded_at=T0 + timedelta(seconds=2), reason="Change idea")
        with self.assertRaisesRegex(LedgerError, "different frozen"):
            self.ledger.correct_fill("bad", "long", dict(original["payload"], signal_id="another"), recorded_at=T0 + timedelta(seconds=2), reason="Change idea")

    def test_no_trade_signal_accepts_no_fills(self):
        signal = {"signal_id": "none", "status": "no_trade", "legs": [], "as_of": T0, "label": "synthetic"}
        self.ledger.append_signal("none", signal, recorded_at=T0)
        with self.assertRaisesRegex(LedgerError, "no-trade"):
            self.ledger.record_fill("bad", "none", "SYNTHETIC-LOW", "BUY", 10, "10", T0, T0)

    def test_unknown_size_preserves_manual_exposure_without_invented_bound(self):
        signal = candidate(signal_id="unsized", quantity=None)
        self.ledger.append_signal("unsized", signal, recorded_at=T0)
        self.ledger.record_fill("actual", "unsized", "SYNTHETIC-HIGH", "SELL", 10, "10", T0, T0)
        self.assertEqual(self.ledger.exposure("unsized")["status"], "unhedged_short")
        self.assertFalse(self.ledger.exposure("unsized")["contractual_bound_valid"])

    def test_contract_metadata_is_dated_and_listed_without_future_rewrites(self):
        variants = [{"known_at": "2026-01-06T00:00:00+00:00"}, {"listed": False},
                    {"effective_from": "2026-01-06T00:00:00+00:00"},
                    {"effective_until": "2026-01-04T00:00:00+00:00"},
                    {"expiry_at": "2026-01-04T00:00:00+00:00"}, {"lot_size": 30}]
        for changes in variants:
            signal = candidate(signal_id="metadata")
            for leg in signal["legs"]:
                leg["contract"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(LedgerError):
                self.ledger.append_signal("metadata", signal, recorded_at=T0)
        self.fill("original-lot", quantity=10)
        self.assertEqual(self.ledger.list_signals()[0]["legs"][0]["contract"]["lot_size"], 10)

    def test_unsupported_naked_ratio_and_mismatched_expiry_signals_rejected(self):
        naked = candidate(signal_id="bad")
        naked["legs"] = [leg for leg in naked["legs"] if leg["side"] == "SELL"]
        ratio = candidate(signal_id="bad")
        ratio["legs"][0]["ratio"] = 2
        expiry = candidate(signal_id="bad")
        expiry["legs"][0]["contract"]["expiry_at"] = "2026-02-02T10:00:00+00:00"
        for signal in (naked, ratio, expiry):
            with self.assertRaises(LedgerError):
                self.ledger.append_signal("bad", signal, recorded_at=T0)

    def test_opening_cap_cannot_be_bypassed_by_closing_then_reopening(self):
        self.fill("open")
        self.fill("close", side="SELL", second=2)
        with self.assertRaisesRegex(LedgerError, "exceeds"):
            self.fill("reopen", second=3)

    def test_invalid_lots_nan_future_and_order_rejected(self):
        for overrides in ({"quantity": 3}, {"price": "NaN"}, {"quantity": True}, {"price": "-2"}):
            with self.subTest(overrides=overrides), self.assertRaises(LedgerError):
                self.fill("bad", **overrides)
        with self.assertRaises(LedgerError):
            self.ledger.record_fill("future", "s1", "SYNTHETIC-LOW", "BUY", 10, "1", T0 + timedelta(seconds=1), T0)
        with self.assertRaises(LedgerError):
            self.ledger.record_fill("old", "s1", "SYNTHETIC-LOW", "BUY", 10, "1", T0 - timedelta(seconds=1), T0)
        self.assertEqual(len(self.ledger.events()), 1)

    def test_database_update_delete_and_corruption_detected(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            for sql in ("DELETE FROM options_events", "UPDATE options_events SET payload='{}'"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)
            db.execute("DROP TRIGGER options_events_no_update")
            db.execute("UPDATE options_events SET payload='{}'")
        with self.assertRaises(IntegrityError):
            OptionsLedger(self.path)

    def test_separate_market_database_is_not_modified(self):
        target = Path(self.temp.name) / "market.sqlite3"
        with closing(sqlite3.connect(target)) as db, db:
            db.execute("CREATE TABLE candles (price REAL)")
        before = target.read_bytes()
        with self.assertRaisesRegex(LedgerError, "separate"):
            OptionsLedger(target)
        self.assertEqual(target.read_bytes(), before)

    def test_export_escapes_formula_cells_and_preserves_json(self):
        self.fill("=HYPERLINK(\"example\")")
        output = Path(self.temp.name) / "ledger.csv"
        self.assertEqual(self.ledger.export_csv(output), 2)
        with output.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(rows[1]["event_id"].startswith("'="))
        self.assertEqual(json.loads(rows[1]["payload_json"])["origin"], "user_reported_manual_fill")
        with self.assertRaisesRegex(LedgerError, "overwrite"):
            self.ledger.export_csv(self.path)

    def test_recursive_credential_keys_rejected_without_write(self):
        signal = candidate(signal_id="bad")
        signal["provenance"] = [{"access_token": "fixture-secret-not-a-real-token"}]
        with self.assertRaisesRegex(LedgerError, "credential"):
            self.ledger.append_signal("bad", signal, recorded_at=T0)
        self.assertEqual(len(self.ledger.events()), 1)


if __name__ == "__main__":
    unittest.main()

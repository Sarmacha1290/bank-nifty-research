from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from banknifty.options_workspace import (OptionsWorkspaceController, OptionsResearchPanel,
    OperationCancelled, WorkspaceBusy, WorkspaceError, format_signal, format_replay,
    sample_import_document, research_status, read_research_runs, read_analysis_results)

T = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)


def settings(**overrides):
    result = {"direction": "BULLISH", "strategy": "LONG_OPTION", "horizon_minutes": "15",
        "capital": "100000", "max_risk_fraction": ".01", "max_contracts": "5", "total_fees": "2"}
    return dict(result, **overrides)


class WorkspaceControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "options"
        self.now = T
        self.controller = OptionsWorkspaceController(self.path, clock=lambda: self.now)

    def test_startup_does_not_write_or_connect(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(self.controller.quality_summary()["label"], "NO DATA")

    def test_synthetic_demo_is_explicit_and_frozen_after_restart(self):
        market = self.controller.load_demo()
        self.assertIn("SYNTHETIC", market.label)
        self.assertEqual(len(market.quotes), 4470)
        restarted = OptionsWorkspaceController(self.path, clock=lambda: self.now)
        self.assertEqual(restarted.market, market)
        self.assertEqual(restarted.quality_summary()["accepted"], 6)

    def test_freshness_transitions_to_stale_without_receipt_refresh(self):
        self.controller.load_demo()
        self.assertEqual(self.controller.quality_summary()["accepted"], 6)
        self.now += timedelta(seconds=4, microseconds=1)
        status = self.controller.quality_summary()
        self.assertEqual(status["accepted"], 0)
        self.assertIn("STALE", status["label"])

    def test_receipt_only_never_becomes_live(self):
        market = self.controller.load_demo()
        self.controller.market = replace(market, quotes=tuple(replace(q, source_at=None) for q in market.quotes))
        result = self.controller.quality_summary()
        self.assertIn("RECEIPT-ONLY", result["label"])
        self.assertEqual(result["accepted"], 0)

    def test_repeated_evaluation_is_idempotent_and_creates_no_fills(self):
        self.controller.load_demo()
        first = self.controller.evaluate(settings())
        second = self.controller.evaluate(settings())
        self.assertEqual(first.signal_id, second.signal_id)
        self.assertEqual(first.status, "candidate")
        self.assertEqual(len(self.controller.ledger().list_signals()), 1)
        self.assertEqual(self.controller.ledger().exposure(first.signal_id)["status"], "unfilled")

    def test_blank_financial_inputs_do_not_invent_sizing(self):
        self.controller.load_demo()
        signal = self.controller.evaluate(settings(capital="", max_risk_fraction="", total_fees="", max_contracts=""))
        self.assertIsNone(signal.quantity)
        self.assertIn("UNAVAILABLE", format_signal(signal))

    def test_future_changes_do_not_change_earlier_signal(self):
        market = self.controller.load_demo()
        first = self.controller.evaluate(settings())
        future = tuple(replace(q, bid=Decimal("999"), ask=Decimal("1000")) if q.received_at > T else q for q in market.quotes)
        self.controller.market = replace(market, quotes=future)
        second = self.controller.evaluate(settings())
        self.assertEqual(first, second)

    def test_pause_is_persistent_and_keeps_exposure_available(self):
        self.controller.load_demo()
        signal = self.controller.evaluate(settings())
        self.controller.set_paused(True)
        restarted = OptionsWorkspaceController(self.path, clock=lambda: T)
        self.assertTrue(restarted.paused)
        with self.assertRaisesRegex(WorkspaceError, "paused"):
            restarted.evaluate(settings())
        self.assertEqual(restarted.ledger().exposure(signal.signal_id)["status"], "unfilled")
        restarted.set_paused(False)
        self.assertEqual(restarted.evaluate(settings()).signal_id, signal.signal_id)

    def test_import_roundtrip_is_unverified_and_restart_hash_checked(self):
        path = Path(self.temp.name) / "quotes.json"
        self.controller.export_template(path)
        market = self.controller.import_file(path)
        self.assertIn("UNVERIFIED", market.label)
        self.assertEqual(market.quotes[0].provenance.evidence_kind, "replay_quote")
        restarted = OptionsWorkspaceController(self.path, clock=lambda: T)
        self.assertEqual(restarted.market, market)
        saved = next((self.path / "imports").glob("*.json"))
        saved.write_text("{}", encoding="utf-8")
        rejected = OptionsWorkspaceController(self.path, clock=lambda: T)
        self.assertIsNone(rejected.market)
        self.assertTrue(rejected.paused)

    def test_malformed_import_preserves_current_market_and_state(self):
        market = self.controller.load_demo()
        before = (self.path / "workspace-state.json").read_bytes()
        path = Path(self.temp.name) / "bad.json"
        path.write_text('{"schema_version":1,"token":"do-not-import"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.controller.import_file(path)
        self.assertEqual(self.controller.market, market)
        self.assertEqual((self.path / "workspace-state.json").read_bytes(), before)

    def test_cancelled_import_does_not_replace_existing_state(self):
        market = self.controller.load_demo()
        path = Path(self.temp.name) / "quotes.json"
        self.controller.export_template(path)
        from banknifty.options_import import load_options_file
        def cancelled(*args, **kwargs):
            value = load_options_file(*args, **kwargs)
            self.assertTrue(self.controller.cancel())
            return value
        with patch("banknifty.options_import.load_options_file", side_effect=cancelled):
            with self.assertRaises(OperationCancelled):
                self.controller.import_file(path)
        self.assertEqual(self.controller.market, market)
        self.assertFalse(self.controller.busy)

    def test_busy_rejects_repeated_operations_then_recovers(self):
        entered, release = threading.Event(), threading.Event()
        worker = threading.Thread(target=lambda: self.controller._run("test", lambda cancel: (entered.set(), release.wait(3))))
        worker.start()
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaises(WorkspaceBusy):
                self.controller.load_demo()
        finally:
            release.set()
            worker.join(3)
        self.controller.load_demo()

    def test_replay_uses_explicit_costs_latency_and_never_actual_fills(self):
        self.controller.load_demo()
        signal = self.controller.evaluate(settings())
        result = self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0")
        self.assertEqual(result.status, "completed", result.reasons)
        self.assertEqual(result.actual_fills_created, 0)
        self.assertIn("HYPOTHETICAL", format_replay(result))
        self.assertEqual(self.controller.ledger().exposure(signal.signal_id)["status"], "unfilled")

    def test_replay_rejects_missing_costs_or_fractional_latency(self):
        self.controller.load_demo()
        self.controller.evaluate(settings())
        for arguments in (("5", "", "0"), (".5", "2", "0")):
            with self.assertRaises(WorkspaceError):
                self.controller.replay(manual_latency_seconds=arguments[0], fees=arguments[1], slippage_per_leg=arguments[2])

    def test_manual_entry_export_restart_and_correction(self):
        self.controller.load_demo()
        signal = self.controller.evaluate(settings())
        leg = signal.legs[0]
        args = dict(signal_id=signal.signal_id, contract_id=leg.contract_id, side="BUY", quantity="1",
                    price=str(leg.price), occurred_at=T.isoformat(), event_id="manual-one", fees="1")
        self.controller.manual_fill(**args)
        with self.assertRaises(ValueError):
            self.controller.manual_fill(**args)
        restarted = OptionsWorkspaceController(self.path, clock=lambda: T)
        self.assertEqual(restarted.ledger().exposure(signal.signal_id)["positions"][leg.contract_id], 1)
        target = Path(self.temp.name) / "report.json"
        restarted.export_report(target)
        report = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn("manual_events", report)
        self.assertIn("hypothetical_replay", report)
        restarted.void_manual_fill("manual-one", "Entered wrong units; retaining correction evidence")
        self.assertEqual(restarted.ledger().exposure(signal.signal_id)["status"], "unfilled")
        self.assertGreaterEqual(len(restarted.ledger().events()), 3)

    def test_neutral_hypothesis_is_a_saved_no_trade(self):
        self.controller.load_demo()
        result = self.controller.evaluate(settings(direction="NEUTRAL"))
        self.assertEqual(result.status, "no_trade")
        self.assertIsNone(result.quantity)

    def test_wrong_saved_state_shape_fails_closed_without_crashing(self):
        self.path.mkdir()
        for invalid in ([], "", None, {"settings": []}):
            (self.path / "workspace-state.json").write_text(json.dumps(invalid), encoding="utf-8")
            restored = OptionsWorkspaceController(self.path, clock=lambda: T)
            self.assertTrue(restored.paused)
            self.assertIsNone(restored.market)

    def test_corrupted_existing_archive_cannot_be_selected_again(self):
        path = Path(self.temp.name) / "quotes.json"
        self.controller.export_template(path)
        self.controller.import_file(path)
        archive = next((self.path / "imports").glob("*.json"))
        archive.write_text("{}", encoding="utf-8")
        market = self.controller.load_demo()
        with self.assertRaisesRegex(WorkspaceError, "integrity"):
            self.controller.import_file(path)
        self.assertEqual(self.controller.market, market)

    def test_cancel_after_dataset_commit_starts_is_not_falsely_accepted(self):
        original = self.controller._save_state
        def during_save(state):
            self.assertFalse(self.controller.cancel())
            return original(state)
        with patch.object(self.controller, "_save_state", side_effect=during_save):
            self.controller.load_demo()
        self.assertIsNotNone(self.controller.market)

    def test_exports_cannot_overwrite_internal_state_or_application_code(self):
        self.controller.load_demo()
        self.controller.evaluate(settings())
        state = self.path / "workspace-state.json"
        original = state.read_bytes()
        targets = ((self.controller.export_report, state), (self.controller.export_template, state),
            (self.controller.export_report, self.path / "options-ledger.sqlite3"),
            (self.controller.export_ledger, self.path / "imports" / "protected.csv"),
            (self.controller.export_template, Path(__file__).resolve().parents[1] / "project.json"))
        for exporter, path in targets:
            with self.assertRaises(WorkspaceError):
                exporter(path)
        self.assertEqual(state.read_bytes(), original)
        self.assertEqual(len(self.controller.ledger().list_signals()), 1)

    def test_cancel_during_ledger_preparation_prevents_signal_commit(self):
        self.controller.load_demo()
        ledger = self.controller.ledger()
        def cancel_before_return():
            self.assertTrue(self.controller.cancel())
            return ledger
        with patch.object(self.controller, "ledger", side_effect=cancel_before_return):
            with self.assertRaises(OperationCancelled):
                self.controller.evaluate(settings())
        self.assertEqual(ledger.list_signals(), [])

    def test_cancel_during_correction_preparation_preserves_manual_fill(self):
        self.controller.load_demo()
        signal = self.controller.evaluate(settings())
        leg = signal.legs[0]
        self.controller.manual_fill(signal_id=signal.signal_id, contract_id=leg.contract_id,
            side="BUY", quantity="1", price=str(leg.price), occurred_at=T, event_id="kept-fill")
        ledger = self.controller.ledger()
        def cancel_before_return():
            self.controller.cancel()
            return ledger
        with patch.object(self.controller, "ledger", side_effect=cancel_before_return):
            with self.assertRaises(OperationCancelled):
                self.controller.void_manual_fill("kept-fill", "Cancelled correction")
        self.assertEqual(ledger.exposure(signal.signal_id)["positions"][leg.contract_id], 1)

    def test_price_correction_preserves_dependent_close_and_original_event(self):
        self.controller.load_demo()
        signal = self.controller.evaluate(settings())
        leg = signal.legs[0]
        self.controller.manual_fill(signal_id=signal.signal_id, contract_id=leg.contract_id,
            side="BUY", quantity="1", price="100", occurred_at=T, event_id="entry", fees="1")
        self.now += timedelta(seconds=2)
        self.controller.manual_fill(signal_id=signal.signal_id, contract_id=leg.contract_id,
            side="SELL", quantity="1", price="110", occurred_at=self.now, event_id="exit", fees="1")
        self.now += timedelta(seconds=1)
        correction = self.controller.replace_manual_fill("entry", price="102", occurred_at=T,
            fees="1", reason="Correcting mistyped execution price")
        exposure = self.controller.ledger().exposure(signal.signal_id)
        self.assertEqual(exposure["status"], "closed")
        self.assertEqual(Decimal(exposure["realized_net_pnl"]), Decimal("6"))
        original = next(e for e in self.controller.ledger().events() if e["event_id"] == "entry")
        self.assertEqual(Decimal(original["payload"]["price"]), Decimal("100"))
        latest = self.controller.manual_fill_for_correction("entry")
        self.assertEqual(latest["price"], "102")
        self.assertEqual(latest["contract_id"], leg.contract_id)

    def test_html_export_and_internal_target_protection(self):
        self.controller.load_demo()
        self.controller.evaluate(settings())
        target = Path(self.temp.name) / "research.html"
        self.controller.export_html(target)
        content = target.read_text(encoding="utf-8")
        self.assertIn("SYNTHETIC", content)
        self.assertNotIn("<script", content.lower())
        with self.assertRaises(WorkspaceError):
            self.controller.export_html(self.path / "internal.html")


class ExtendedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "research"
        self.now = T
        self.controller = OptionsWorkspaceController(self.path, clock=lambda: self.now)

    def corpus(self, *, receipt_only=False, kind="synthetic"):
        from tests.test_options_pipeline import underlying_fixture, option_fixture
        from banknifty.market_archive import ImportManifest
        underlying = underlying_fixture(cutoff=T, kind=kind)
        if receipt_only:
            underlying = tuple(replace(row, source_at=None) for row in underlying)
        contracts, quotes = option_fixture(cutoff=T, kind=kind)
        archive = self.controller.archive(read_only=False)
        archive.append_batch(contracts=contracts, quotes=quotes, underlying=underlying,
            manifest=ImportManifest("test-corpus", T, b"synthetic-test-corpus", evidence_kind="synthetic"))
        return archive

    def test_reconstructed_live_records_remain_replay_in_workspace_and_saved_run(self):
        self.corpus(kind="live_verified")
        decision = self.controller.run_data_pipeline(settings(), as_of=T, archive_mode="reconstructed")
        self.assertEqual(decision.label, "replay")
        self.assertFalse(decision.signal.trading_allowed)
        run = read_research_runs(self.path)[0]["payload"]
        self.assertEqual(run["archive_mode"], "reconstructed")
        self.assertEqual(run["decision"]["label"], "replay")
        self.assertTrue(all(quote.provenance.evidence_kind == "replay_quote" for quote in self.controller.market.quotes))

    def test_observed_availability_is_passed_to_replay_and_persisted(self):
        market = self.controller.load_demo()
        self.controller.evaluate(settings())
        self.controller.market = replace(market, availability_times=tuple(
            (quote.fingerprint, T + timedelta(hours=1)) for quote in market.quotes))
        result = self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0")
        self.assertNotEqual(result.status, "completed")
        self.assertIsNone(result.net_pnl)
        saved = read_research_runs(self.path)[0]["payload"]
        self.assertTrue(saved["availability_times"])
        self.assertEqual(set(saved["availability_times"]), set(saved["quote_fingerprints"]))
        self.assertEqual(set(saved["availability_times"].values()), {(T + timedelta(hours=1)).isoformat()})

    def test_archive_conflicts_reach_pipeline_replay_and_immutable_audit(self):
        from banknifty.market_archive import ImportManifest
        from banknifty.options_replay import replay_signal
        archive = self.corpus()
        first = self.controller.run_data_pipeline(settings(), as_of=T, archive_mode="observed")
        self.assertEqual(first.status, "candidate")
        original = next(quote for quote in archive.query(as_of=T).quotes
                        if quote.contract_id == first.signal.legs[0].contract_id)
        conflict_at = T + timedelta(seconds=1)
        changed = replace(original, bid=original.bid - Decimal("1"), received_at=conflict_at)
        archive.append_batch(quotes=(changed,),
            manifest=ImportManifest("conflict-test", conflict_at, b"changed-bid", evidence_kind="synthetic"))
        self.now = conflict_at
        earlier = self.controller.run_data_pipeline(settings(), as_of=T, archive_mode="observed")
        self.assertEqual(earlier, first)
        self.assertEqual(len(self.controller.market.conflict_events), 1)
        facts = self.controller.market.conflict_events
        with patch("banknifty.options_replay.replay_signal", wraps=replay_signal) as replay:
            self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0")
        self.assertEqual(replay.call_args.kwargs["conflict_events"], facts)
        replay_run = next(run["payload"] for run in read_research_runs(self.path) if run["payload"]["kind"] == "replay")
        self.assertEqual(replay_run["conflict_events"], [fact.to_dict() for fact in facts])
        blocked = self.controller.run_data_pipeline(settings(), as_of=conflict_at, archive_mode="observed")
        self.assertEqual(blocked.status, "no_trade")
        pipeline_run = next(run["payload"] for run in read_research_runs(self.path)
                            if run["payload"]["kind"] == "pipeline" and run["payload"]["conflict_events"])
        self.assertEqual(pipeline_run["conflict_events"], [fact.to_dict() for fact in facts])

    def test_pre_conflict_schema_pipeline_run_remains_readable(self):
        from hashlib import sha256
        from banknifty.options_workspace import _dump
        self.corpus()
        self.controller.run_data_pipeline(settings(), as_of=T)
        run = read_research_runs(self.path)[0]
        prior_path = self.path / "research-runs" / (run["run_id"] + ".json")
        run["payload"].pop("conflict_events")
        for name in ("data_gate_passed", "conflict_events"):
            run["payload"]["decision"].pop(name)
        run["run_id"] = sha256(_dump(run["payload"]).encode()).hexdigest()
        body = {key: run[key] for key in ("version", "run_id", "recorded_at", "payload")}
        run["sha256"] = sha256(_dump(body).encode()).hexdigest()
        prior_path.unlink()
        legacy = self.path / "research-runs" / (run["run_id"] + ".json")
        legacy.write_text(_dump(run), encoding="utf-8")
        before = legacy.read_bytes()
        self.assertEqual(read_research_runs(self.path), [run])
        restored = OptionsWorkspaceController(self.path, clock=lambda: T)
        self.assertEqual(restored.saved_runs, [run])
        self.assertIsNone(restored.runs_error)
        self.assertEqual(legacy.read_bytes(), before)

    def test_changed_manual_inputs_clear_explanation_and_keep_archive_selection(self):
        self.controller._select_archive("synthetic")
        self.controller.load_demo()
        first = self.controller.evaluate(settings(direction="BULLISH"))
        explanation = self.controller.explain_latest().to_dict()
        second = self.controller.evaluate(settings(direction="BEARISH"))
        self.assertNotEqual(first.input_fingerprint, second.input_fingerprint)
        self.assertEqual(self.controller.settings["archive_kind"], "synthetic")
        self.assertIsNone(self.controller.saved_results["explanation"])
        self.assertIsNone(research_status(self.path, as_of=T)["explanation"])
        self.controller._save_results(explanation=explanation)
        status = research_status(self.path, as_of=T)
        self.assertIsNone(status["explanation"])
        self.assertTrue(any(item["code"] == "explanation_input_changed" for item in status["alerts"]))

    def test_new_dataset_clears_cached_current_analysis_but_keeps_immutable_replays(self):
        self.controller.load_demo()
        self.controller.evaluate(settings())
        self.controller.explain_latest()
        self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0")
        self.now += timedelta(minutes=1)
        self.controller.load_demo()
        self.assertIsNone(self.controller.latest_signal)
        self.assertIsNone(self.controller.saved_results["explanation"])
        self.assertIsNone(self.controller.saved_results["replay"])
        self.assertEqual(len(read_research_runs(self.path)), 1)

    def test_hash_valid_run_with_unsupported_or_secret_fields_is_rejected(self):
        from hashlib import sha256
        from banknifty.options_workspace import _dump
        self.controller.load_demo()
        self.controller.evaluate(settings())
        self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0")
        original = read_research_runs(self.path)[0]
        original_path = next((self.path / "research-runs").glob("*.json"))
        for change in (lambda p: p.update(unexpected="unsupported"),
                       lambda p: p["signal"].update(api_key="synthetic-rejected-placeholder"),
                       lambda p: p.pop("policy")):
            value = json.loads(json.dumps(original))
            change(value["payload"])
            value["run_id"] = sha256(_dump(value["payload"]).encode()).hexdigest()
            body = {key: value[key] for key in ("version", "run_id", "recorded_at", "payload")}
            value["sha256"] = sha256(_dump(body).encode()).hexdigest()
            path = original_path.parent / (value["run_id"] + ".json")
            path.write_text(_dump(value), encoding="utf-8")
            with self.assertRaises(WorkspaceError):
                read_research_runs(self.path)
            path.unlink()

    def test_evaluation_records_insufficient_evidence_and_restarts_without_ledger_mutation(self):
        self.corpus()
        experiment = {"name": "One synthetic session", "start_at": T - timedelta(minutes=10),
            "end_at": T + timedelta(minutes=1), "decision_interval_minutes": "1"}
        result = self.controller.evaluate_archive(settings(manual_latency="5", slippage_per_leg="0"), experiment)
        self.assertEqual(result.status, "insufficient_sessions")
        self.assertFalse(result.validated)
        self.assertEqual(result.actual_fills_created, 0)
        self.assertFalse((self.path / "options-ledger.sqlite3").exists())
        self.assertEqual(read_research_runs(self.path)[0]["payload"]["kind"], "evaluation")
        restored = OptionsWorkspaceController(self.path, clock=lambda: T)
        self.assertEqual(restored.saved_results["evaluation"]["status"], "insufficient_sessions")

    def test_evaluation_cancelled_before_commit_preserves_archive_and_no_report(self):
        archive = self.corpus()
        before = archive.inventory()
        from banknifty.options_evaluation import evaluate_archive
        def cancelled(*args, **kwargs):
            result = evaluate_archive(*args, **kwargs)
            self.controller.cancel()
            return result
        experiment = {"start_at": T - timedelta(minutes=10), "end_at": T + timedelta(minutes=1)}
        with patch("banknifty.options_evaluation.evaluate_archive", side_effect=cancelled):
            with self.assertRaises(OperationCancelled):
                self.controller.evaluate_archive(settings(manual_latency="5", slippage_per_leg="0"), experiment)
        self.assertEqual(archive.inventory(), before)
        self.assertEqual(read_research_runs(self.path), [])

    def test_read_only_empty_status_creates_no_folder_and_stays_disconnected(self):
        status = research_status(self.path, as_of=T)
        self.assertFalse(self.path.exists())
        self.assertFalse(status["live_connection_active"])
        self.assertFalse(status["automatic_order_capability"])
        self.assertEqual(status["research_runs_status"], "empty")
        self.assertFalse(status["provider_readiness"]["connection_attempted"])

    def test_replay_restart_restores_immutable_result_and_full_assumptions(self):
        self.controller.load_demo()
        self.controller.evaluate(settings())
        result = self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0.05")
        first = read_research_runs(self.path)
        self.assertEqual(len(first), 1)
        replay = first[0]["payload"]
        self.assertEqual(replay["policy"]["manual_latency_seconds"], 5)
        self.assertEqual(Decimal(replay["policy"]["slippage_per_leg"]), Decimal(".05"))
        self.assertTrue(replay["quote_fingerprints"])
        self.assertIn("source_at", replay["quotes"][0])
        self.assertIn("received_at", replay["quotes"][0])
        self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0.05")
        self.assertEqual(read_research_runs(self.path), first)
        restarted = OptionsWorkspaceController(self.path, clock=lambda: T + timedelta(hours=1))
        self.assertEqual(restarted.saved_runs, first)
        status = research_status(self.path, as_of=T + timedelta(hours=1))
        self.assertEqual(Decimal(status["latest_replay"]["net_pnl"]), result.net_pnl)
        self.assertTrue(status["latest_signal"]["horizon_elapsed"])
        self.assertTrue(any(a["code"] == "research_horizon_elapsed" for a in status["alerts"]))

    def test_corrupt_run_is_explicitly_unavailable_after_restart(self):
        self.controller.load_demo()
        self.controller.evaluate(settings())
        self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0")
        path = next((self.path / "research-runs").glob("*.json"))
        value = json.loads(path.read_text(encoding="utf-8"))
        value["payload"]["result"]["net_pnl"] = "999999"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            read_research_runs(self.path)
        restored = OptionsWorkspaceController(self.path, clock=lambda: T)
        self.assertTrue(restored.runs_error)
        self.assertEqual(restored.saved_results, {})
        status = research_status(self.path, as_of=T)
        self.assertEqual(status["research_runs_status"], "invalid")
        self.assertIsNone(status["latest_replay"])
        self.assertTrue(any(a["code"] == "research_runs_invalid" for a in status["alerts"]))

    def test_interrupted_second_replay_write_preserves_committed_history(self):
        self.controller.load_demo()
        self.controller.evaluate(settings())
        first_result = self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0.05")
        first = read_research_runs(self.path)
        original_open = Path.open
        writes = []

        class PartialWriter:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.handle.close()

            def write(self, data):
                self.handle.write(data[:100])
                self.handle.flush()
                raise OSError("simulated write interruption")

        def interrupted(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            if path.parent.name == "research-runs" and args and args[0] == "xb":
                writes.append(path)
                return PartialWriter(handle)
            return handle

        with patch.object(Path, "open", interrupted):
            with self.assertRaisesRegex(OSError, "simulated write interruption"):
                self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0.10")
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].suffix, ".pending")
        self.assertEqual(len(list((self.path / "research-runs").glob("*.json"))), 1)
        restarted = OptionsWorkspaceController(self.path, clock=lambda: T)
        self.assertEqual(restarted.saved_runs, first)
        self.assertIsNone(restarted.runs_error)
        status = research_status(self.path, as_of=T)
        self.assertEqual(status["research_runs_status"], "available")
        self.assertEqual(Decimal(status["latest_replay"]["net_pnl"]), first_result.net_pnl)

    def test_fsync_failure_never_publishes_and_stale_staging_is_ignored(self):
        self.controller.load_demo()
        self.controller.evaluate(settings())
        self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0.05")
        first = read_research_runs(self.path)
        with patch("banknifty.options_workspace.os.fsync", side_effect=OSError("simulated flush failure")):
            with self.assertRaisesRegex(OSError, "simulated flush failure"):
                self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0.10")
        abandoned = self.path / "research-runs" / ("a" * 64 + ".crashed.pending")
        abandoned.write_bytes(b'{"incomplete":')
        self.assertEqual(read_research_runs(self.path), first)
        self.assertEqual(OptionsWorkspaceController(self.path, clock=lambda: T).saved_runs, first)
        self.assertEqual(research_status(self.path, as_of=T)["research_runs_status"], "available")
        self.assertEqual(abandoned.read_bytes(), b'{"incomplete":')

    def test_atomic_publication_follows_fsync_and_does_not_replace_existing_run(self):
        self.controller.load_demo()
        self.controller.evaluate(settings())
        actual_fsync, actual_link = os.fsync, os.link
        calls = []

        def fsync(fd):
            actual_fsync(fd)
            calls.append("fsync")

        def concurrent_publish(source, target):
            self.assertEqual(calls, ["fsync"])
            staged = json.loads(Path(source).read_text(encoding="utf-8"))
            self.assertEqual(Path(target).stem, staged["run_id"])
            self.assertFalse(Path(target).exists())
            self.assertEqual(read_research_runs(self.path), [])
            # Another writer wins the no-overwrite publication race. Its
            # independently valid recording time must remain unchanged.
            from hashlib import sha256
            from banknifty.options_workspace import _dump
            staged["recorded_at"] = (T - timedelta(seconds=1)).isoformat()
            body = {key: staged[key] for key in ("version", "run_id", "recorded_at", "payload")}
            staged["sha256"] = sha256(_dump(body).encode()).hexdigest()
            Path(target).write_text(_dump(staged), encoding="utf-8")
            actual_link(source, target)

        with patch("banknifty.options_workspace.os.fsync", side_effect=fsync), \
                patch("banknifty.options_workspace.os.link", side_effect=concurrent_publish):
            self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0.05")
        runs = read_research_runs(self.path)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["recorded_at"], (T - timedelta(seconds=1)).isoformat())
        self.assertEqual(self.controller.saved_runs, runs)
        self.assertEqual(list((self.path / "research-runs").glob("*.pending")), [])

    def test_corrupt_second_final_keeps_valid_history_and_strict_backup_rejects(self):
        from banknifty.project_backup import _research_backup_files
        self.path = Path(self.temp.name) / "project" / "data" / "options-research"
        self.controller = OptionsWorkspaceController(self.path, clock=lambda: T)
        self.controller.load_demo()
        self.controller.evaluate(settings())
        result = self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0.05")
        first = read_research_runs(self.path)
        abandoned = self.path / "research-runs" / ("a" * 64 + ".crashed.pending")
        abandoned.write_bytes(b'{"incomplete":')
        scratch = Path(self.temp.name) / "backup-scratch"
        scratch.mkdir()
        files, summary = _research_backup_files(self.path.parent.parent, scratch)
        self.assertEqual(summary["research_runs"], 1)
        self.assertFalse(any(name.endswith(".pending") for name in files))
        corrupt = self.path / "research-runs" / ("b" * 64 + ".json")
        raw = b'{"payload":{"unrelated_private":"DUMMY-PRIVATE-FIXTURE"},'
        corrupt.write_bytes(raw)
        restored = OptionsWorkspaceController(self.path, clock=lambda: T)
        self.assertEqual(restored.saved_runs, first)
        self.assertIn("1 saved research record(s)", restored.runs_error)
        self.assertIn("valid historical runs remain available", restored.restore_message)
        status = research_status(self.path, as_of=T)
        self.assertEqual(status["research_runs_status"], "partial")
        self.assertEqual(Decimal(status["latest_replay"]["net_pnl"]), result.net_pnl)
        self.assertTrue(any(a["code"] == "research_runs_invalid" for a in status["alerts"]))
        self.assertNotIn("DUMMY-PRIVATE-FIXTURE", json.dumps(status))
        self.assertEqual(corrupt.read_bytes(), raw)
        report = Path(self.temp.name) / "recovered.json"
        restored.export_report(report)
        exported = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(exported["research_runs"], first)
        self.assertIn("failed validation", exported["research_runs_warning"])
        self.assertNotIn("DUMMY-PRIVATE-FIXTURE", report.read_text(encoding="utf-8"))
        with self.assertRaises(WorkspaceError):
            read_research_runs(self.path)
        with self.assertRaises(WorkspaceError):
            _research_backup_files(self.path.parent.parent, scratch)

    def test_snapshot_never_writes_existing_state_ledger_or_run_files(self):
        self.controller.load_demo()
        self.controller.evaluate(settings())
        self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0")
        def files():
            return {str(p.relative_to(self.path)): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.path.rglob("*") if p.is_file()}
        before = files()
        first = research_status(self.path, as_of=T + timedelta(minutes=20))
        second = research_status(self.path, as_of=T + timedelta(minutes=21))
        self.assertEqual(before, files())
        self.assertEqual({a["id"] for a in first["alerts"]}, {a["id"] for a in second["alerts"]})

    def test_data_driven_direction_ignores_manual_hypothesis_and_spot(self):
        self.corpus()
        result = self.controller.run_data_pipeline(settings(direction="BEARISH", underlying_price="1"), as_of=T)
        self.assertEqual(result.assessment.direction, "BULLISH")
        self.assertEqual(result.signal.direction, "BULLISH")
        self.assertEqual(result.label, "synthetic")
        self.assertEqual(result.signal.underlying_price, result.assessment.reference_price)
        repeated = self.controller.run_data_pipeline(settings(direction="BEARISH", underlying_price="1"), as_of=T)
        self.assertEqual(result.input_fingerprint, repeated.input_fingerprint)
        self.assertEqual(len(self.controller.ledger().list_signals()), 1)
        self.assertEqual(len(read_research_runs(self.path)), 1)

    def test_receipt_only_underlying_yields_no_signal_and_no_invented_fills(self):
        self.corpus(receipt_only=True)
        result = self.controller.run_data_pipeline(settings(), as_of=T)
        self.assertEqual(result.status, "no_trade")
        self.assertIsNone(result.signal)
        self.assertIn("receipt_only", result.label)
        self.assertFalse((self.path / "options-ledger.sqlite3").exists())

    def test_pipeline_cancellation_before_commit_does_not_save_signal(self):
        self.corpus()
        from banknifty.options_pipeline import run_pipeline
        def cancelled(*args, **kwargs):
            result = run_pipeline(*args, **kwargs)
            self.controller.cancel()
            return result
        with patch("banknifty.options_pipeline.run_pipeline", side_effect=cancelled):
            with self.assertRaises(OperationCancelled):
                self.controller.run_data_pipeline(settings(), as_of=T)
        self.assertFalse((self.path / "options-ledger.sqlite3").exists())
        self.assertEqual(read_research_runs(self.path), [])

    def test_archive_schema_template_import_and_readiness_are_unverified(self):
        path = Path(self.temp.name) / "schema.json"
        self.controller.export_archive_template(path)
        result = self.controller.import_archive(path)
        self.assertEqual(result["import"]["status"], "accepted")
        self.assertEqual(result["inventory"]["counts"]["underlying"], 120)
        decision = self.controller.run_data_pipeline(settings(), as_of=T)
        self.assertEqual(decision.label, "replay")
        self.assertFalse(self.controller.archive_diagnostics()["live_connection_active"])

    def test_invalid_corpus_does_not_replace_existing_archive_selection(self):
        self.controller._select_archive("synthetic")
        path = Path(self.temp.name) / "bad.json"
        path.write_text('{"schema_version":2,"api_key":"not-accepted"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.controller.import_archive(path)
        self.assertEqual(self.controller.settings["archive_kind"], "synthetic")

    def test_explanation_is_local_and_does_not_change_frozen_signal(self):
        self.controller.load_demo()
        signal = self.controller.evaluate(settings())
        with patch("http.client.HTTPSConnection", side_effect=AssertionError("No AI connection allowed")):
            explanation = self.controller.explain_latest()
        self.assertEqual(self.controller.latest_signal, signal)
        self.assertEqual(explanation.mode, "local")
        self.assertEqual(len(self.controller.ledger().list_signals()), 1)

    def test_html_retains_manual_prices_fees_and_correction_reason(self):
        self.controller.load_demo()
        signal = self.controller.evaluate(settings())
        leg = signal.legs[0]
        self.controller.manual_fill(signal_id=signal.signal_id, contract_id=leg.contract_id, side="BUY",
            quantity="1", price="123.45", occurred_at=T, event_id="audit-fill", fees="7.25")
        self.controller.replace_manual_fill("audit-fill", price="124.55", occurred_at=T, fees="8.75", reason="Corrected fee statement")
        path = Path(self.temp.name) / "audit.html"
        self.controller.export_html(path)
        text = path.read_text(encoding="utf-8")
        for expected in ("123.45", "124.55", "7.25", "8.75", "Corrected fee statement"):
            self.assertIn(expected, text)

    def test_bad_analysis_cache_is_not_treated_as_absent(self):
        self.path.mkdir()
        (self.path / "analysis-results.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            read_analysis_results(self.path)
        status = research_status(self.path, as_of=T)
        self.assertTrue(any(a["code"] == "analysis_summary_invalid" for a in status["alerts"]))

    def test_duplicate_cache_keys_reject_ignored_private_fixture_without_exposure(self):
        from hashlib import sha256
        from banknifty.options_workspace import _dump
        self.path.mkdir()
        marker = "DUMMY-PRIVATE-FIXTURE"
        for payload, raw_payload in (
            ({}, '"payload":{"unrelated_private":"' + marker + '"},"payload":{}'),
            ({"updated_at": T.isoformat()}, '"payload":{"updated_at":"' + marker + '","updated_at":"' + T.isoformat() + '"}'),
        ):
            raw = '{"version":1,' + raw_payload + ',"sha256":"' + sha256(_dump(payload).encode()).hexdigest() + '"}'
            with self.subTest(raw_payload=raw_payload):
                (self.path / "analysis-results.json").write_text(raw, encoding="utf-8")
                with self.assertRaises(WorkspaceError) as caught:
                    read_analysis_results(self.path)
                self.assertNotIn(marker, str(caught.exception))
                status = research_status(self.path, as_of=T)
                self.assertTrue(any(a["code"] == "analysis_summary_invalid" for a in status["alerts"]))
                self.assertNotIn(marker, json.dumps(status))
                self.assertIsNone(status["explanation"])

    def test_duplicate_research_run_keys_fail_closed_even_with_valid_final_hash(self):
        self.controller.load_demo()
        self.controller.evaluate(settings())
        self.controller.replay(manual_latency_seconds="5", fees="2", slippage_per_leg="0")
        path = next((self.path / "research-runs").glob("*.json"))
        raw = path.read_text(encoding="utf-8")
        marker = "DUMMY-PRIVATE-FIXTURE"
        path.write_text('{"payload":{"unrelated_private":"' + marker + '"},' + raw.lstrip()[1:], encoding="utf-8")
        with self.assertRaises(WorkspaceError) as caught:
            read_research_runs(self.path)
        self.assertNotIn(marker, str(caught.exception))
        status = research_status(self.path, as_of=T)
        self.assertEqual(status["research_runs_status"], "invalid")
        self.assertIsNone(status["latest_replay"])
        self.assertNotIn(marker, json.dumps(status))

    def test_saved_state_rejects_duplicate_nonfinite_and_deep_json(self):
        self.path.mkdir()
        base = '{"version":1,"kind":"empty","paused":false,"settings":%s}'
        values = ('{"private":"DUMMY-PRIVATE-FIXTURE","private":0}',
                  '{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}', '{"value":1e999}',
                  '{"value":' + '[' * 45 + '0' + ']' * 45 + '}',
                  '{"value":' + '[' * 1100 + '0' + ']' * 1100 + '}')
        for value in values:
            with self.subTest(value=value[:60]):
                (self.path / "workspace-state.json").write_text(base % value, encoding="utf-8")
                restored = OptionsWorkspaceController(self.path, clock=lambda: T)
                self.assertTrue(restored.paused)
                self.assertEqual(restored.settings, {})
                self.assertIn("failed validation", restored.restore_message)
                status = research_status(self.path, as_of=T)
                self.assertNotIn("DUMMY-PRIVATE-FIXTURE", json.dumps(status))
                self.assertTrue(any(a["code"] == "entries_paused" for a in status["alerts"]))

    def test_strict_json_rules_apply_to_cache_and_run_readers(self):
        from banknifty.options_workspace import _strict_json
        self.path.mkdir()
        (self.path / "research-runs").mkdir()
        malformed = ('{"payload":NaN}', '{"payload":Infinity}', '{"payload":-Infinity}',
                     '{"payload":1e999}', '[' * 45 + '0' + ']' * 45,
                     '[' * 1100 + '0' + ']' * 1100, '{"payload":')
        for raw in malformed:
            with self.subTest(raw=raw[:50]):
                with self.assertRaises(WorkspaceError):
                    _strict_json(raw)
                (self.path / "analysis-results.json").write_text(raw, encoding="utf-8")
                (self.path / "research-runs" / ("a" * 64 + ".json")).write_text(raw, encoding="utf-8")
                with self.assertRaises(WorkspaceError):
                    read_analysis_results(self.path)
                with self.assertRaises(WorkspaceError):
                    read_research_runs(self.path)


class OptionsPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.panel = OptionsResearchPanel(self.root, storage_dir=Path(self.temp.name) / "ui")
        self.panel.pack()
        self.panel.controller.clock = lambda: T

    def wait_for_work(self):
        until = time.monotonic() + 10
        while self.panel._worker is not None and time.monotonic() < until:
            self.root.update()
            time.sleep(.01)
        self.root.update()
        self.assertIsNone(self.panel._worker, self.panel.notice.get())

    def test_readiness_updates_only_main_thread_and_deduplicates_restart_alert(self):
        self.panel.controller.load_demo()
        self.panel.controller.evaluate(settings())
        self.panel.controller.clock = lambda: T + timedelta(hours=1)
        main_thread = threading.get_ident()
        updates = []
        original = self.panel._text
        def capture(widget, value):
            updates.append(threading.get_ident())
            return original(widget, value)
        with patch.object(self.panel, "_text", side_effect=capture):
            self.panel.diagnose_archive()
            self.panel.diagnose_archive()
            self.assertIn("already", self.panel.notice.get())
            self.wait_for_work()
            self.assertNotIn("No new warning", self.panel.notice.get())
            self.panel.diagnose_archive()
            self.wait_for_work()
        self.assertEqual(set(updates), {main_thread})
        self.assertIn("No new warning", self.panel.notice.get())
        self.assertIn("not_started", self.panel.analysis_output.get("1.0", "end"))

    def test_data_evaluation_cancel_keeps_event_loop_and_controls_available(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def evaluate(*args):
            calls.append(1)
            def action(cancel):
                entered.set()
                release.wait(3)
                self.panel.controller._check(cancel)
            return self.panel.controller._run("controlled-test-evaluation", action)
        with patch.object(self.panel.controller, "evaluate_archive", side_effect=evaluate):
            self.panel.evaluate_corpus()
            self.assertTrue(entered.wait(1))
            self.panel.evaluate_corpus()
            beat = []
            self.root.after(1, lambda: beat.append(True))
            end = time.monotonic() + .1
            while not beat and time.monotonic() < end:
                self.root.update()
            self.assertTrue(beat)
            self.panel.cancel()
            release.set()
            self.wait_for_work()
        self.assertEqual(calls, [1])
        self.assertIn("OperationCancelled", self.panel.notice.get())
        self.assertTrue(all(str(button.cget("state")) == "normal" for button in self.panel.action_buttons))
        self.assertEqual(read_research_runs(self.panel.controller.storage_dir), [])

    def test_demo_repeated_clicks_and_cancelled_import_keep_ui_responsive(self):
        self.panel.load_demo()
        self.panel.load_demo()
        self.assertIn("already", self.panel.notice.get())
        self.wait_for_work()
        self.assertIn("SYNTHETIC", self.panel.status.get())
        market = self.panel.controller.market
        with patch("banknifty.options_workspace.filedialog.askopenfilename", return_value=""):
            self.panel.import_data()
        self.assertEqual(self.panel.controller.market, market)
        self.assertIn("cancelled", self.panel.notice.get())

    def test_bad_import_recovers_controls_and_stale_status(self):
        self.panel.load_demo()
        self.wait_for_work()
        path = Path(self.temp.name) / "bad.json"
        path.write_text("not json", encoding="utf-8")
        with patch("banknifty.options_workspace.filedialog.askopenfilename", return_value=str(path)):
            self.panel.import_data()
        self.wait_for_work()
        self.assertIn("ValueError", self.panel.notice.get())
        self.assertTrue(all(str(button.cget("state")) == "normal" for button in self.panel.action_buttons))
        self.panel.controller.clock = lambda: T + timedelta(seconds=4, microseconds=1)
        self.panel.refresh_status(schedule=False)
        self.assertIn("STALE", self.panel.status.get())

    def test_ui_evaluate_pause_resume_and_export_cancellation(self):
        self.panel.load_demo()
        self.wait_for_work()
        for key, value in settings().items():
            self.panel.variables[key].set(value)
        self.panel.evaluate()
        self.wait_for_work()
        self.assertIn("UNVALIDATED", self.panel.output.get("1.0", "end"))
        self.panel.toggle_pause()
        self.wait_for_work()
        self.assertIn("PAUSED", self.panel.status.get())
        self.panel.toggle_pause()
        self.wait_for_work()
        self.assertFalse(self.panel.controller.paused)
        with patch("banknifty.options_workspace.filedialog.asksaveasfilename", return_value=""):
            self.panel.export_report()
        self.assertIn("cancelled", self.panel.notice.get())

    def test_manual_correction_dialog_prefills_and_appends_replacement(self):
        self.panel.controller.load_demo()
        signal = self.panel.controller.evaluate(settings())
        leg = signal.legs[0]
        self.panel.controller.manual_fill(signal_id=signal.signal_id, contract_id=leg.contract_id,
            side="BUY", quantity="1", price="100", occurred_at=T, event_id="dialog-entry", fees="1")
        self.panel.correction_id.set("dialog-entry")
        self.panel.edit_fill()
        self.wait_for_work()
        dialog = self.panel._correction_window
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(self.panel.correction_dialog_vars["price"].get(), "100")
        self.panel.edit_fill()
        self.assertIs(self.panel._correction_window, dialog)
        self.panel.correction_dialog_vars["price"].set("102")
        self.panel.correction_dialog_vars["reason"].set("Wrong price typed")
        self.panel.correction_dialog_save()
        self.wait_for_work()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(self.panel.controller.manual_fill_for_correction("dialog-entry")["price"], "102")


if __name__ == "__main__":
    unittest.main()

import contextlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest

from banknifty.market_archive import ArchiveConflict, ArchiveSnapshot, ImportManifest, MarketArchive
from banknifty.options_data import ContractMetadata, OptionQuote, RawProvenance
from banknifty.options_pipeline import UnderlyingObservation
from banknifty.options_strategy import ResearchPolicy
from banknifty.options_replay import ReplayPolicy
from banknifty.options_evaluation import (FrozenExperimentConfig, EvaluationRow, evaluate_archive,
    evaluation_folds, fit_calibration, calibration_metrics, summarize_rows, freeze_experiment, main)


D = Decimal
T = datetime(2026, 1, 5, 4, 0, tzinfo=timezone.utc)


def raw(at, kind="synthetic"):
    return RawProvenance("evaluation-fixture", b"invented-offline-fixture", at, kind)


def corpus(days=6, *, kind="synthetic"):
    known = T-timedelta(days=1)
    contracts = tuple(ContractMetadata(name, "SYNTH-BANKNIFTY", "SYNTHETIC", right, D(strike),
        T+timedelta(days=30), 1, D("0.05"), known, None, known, True, raw(known, kind))
        for name, right, strike in (("FIX-CALL50100", "CALL", "50100"), ("FIX-PUT50000", "PUT", "50000")))
    observations, quotes = [], []
    for day in range(days):
        start = T+timedelta(days=day)
        for step in range(120):
            at = start+timedelta(seconds=step*5)
            observations.append(UnderlyingObservation("SYNTH-BANKNIFTY", D("50000")+D(step)/10,
                at, at, raw(at, kind), step))
        for seconds in range(-5, 100, 5):
            at = start+timedelta(minutes=10, seconds=seconds)
            bid = D("100") if seconds < 15 else D("105") if day % 2 == 0 else D("95")
            for contract in contracts:
                quotes.append(OptionQuote(contract.contract_id, at, at, bid, bid+1, raw(at, kind),
                    bid_size=100, ask_size=100, sequence=seconds+5))
    return contracts, tuple(quotes), tuple(observations)


class MemoryArchive:
    read_only = True

    def __init__(self, data, *, imported_at=None, conflicts=()):
        self.contracts, self.quotes, self.underlying = data
        self.imported_at = imported_at
        self.conflicts = tuple(conflicts)
        self.calls = []

    def query(self, as_of, *, mode="observed", start_at=None):
        self.calls.append((as_of, mode, start_at))
        def visible(item):
            receipt = getattr(item, "received_at", getattr(item, "known_at", None))
            source = getattr(item, "source_at", None)
            return (receipt <= as_of and item.provenance.captured_at <= as_of
                    and (source is None or source <= as_of)
                    and (mode == "reconstructed" or self.imported_at is None or self.imported_at <= as_of)
                    and (isinstance(item, ContractMetadata) or start_at is None or receipt >= start_at))
        groups = tuple(tuple(item for item in group if visible(item)) for group in (self.contracts, self.quotes, self.underlying))
        all_rows = tuple(item for group in groups for item in group)
        availability = tuple(sorted((item.fingerprint, self.imported_at or getattr(item, "received_at", getattr(item, "known_at", None))) for item in all_rows))
        return ArchiveSnapshot(*groups, ("fixture-manifest",), "SYNTHETIC FIXTURE / REPLAY", as_of, mode,
                               tuple(sorted(item.fingerprint for item in all_rows)), availability=availability,
                               conflicts=tuple(event for event in self.conflicts if event.available_at <= as_of))


def config(days=6, **changes):
    return replace(FrozenExperimentConfig("offline-fixture", T+timedelta(minutes=10),
        T+timedelta(days=days-1, minutes=11), research_policy=ResearchPolicy(horizon_minutes=1),
        replay_policy=ReplayPolicy(fees_total=D("0.20"), slippage_per_leg=D("0.05"), quantity=1),
        decision_interval_minutes=60, min_train_sessions=2, validation_sessions=1, holdout_sessions=1,
        min_calibration_samples=4, min_bin_samples=2, min_calibration_days=2, min_class_samples=1), **changes)


def calibration_rows(count=20, *, kind="live_verified", one_day=False):
    result = []
    for index in range(count):
        at = T+timedelta(days=0 if one_day else index, minutes=index if one_day else 0)
        positive = index % 2 == 0
        result.append(EvaluationRow(at, at.date(), "pipeline", "candidate", kind, str(index),
            str(index), D("0.4"), "completed", D("1") if positive else D("-1"), positive,
            label_available_at=at+timedelta(minutes=1)))
    return tuple(result)


class EvaluationRunnerTests(unittest.TestCase):
    def test_published_archive_conflicts_are_forwarded_to_pipeline_gate(self):
        data = corpus()
        conflicts = []
        for day in range(6):
            cutoff = T+timedelta(days=day, minutes=10)
            book = next(q for q in data[1] if q.received_at == cutoff and q.contract_id == "FIX-CALL50100")
            conflicts.append(ArchiveConflict("option", book.contract_id, book.provenance.source,
                book.provenance.evidence_kind, "1"*64, book.source_at, book.received_at,
                book.sequence, cutoff, "2"*64, "3"*64, "4"*64))
        intervals = tuple((T+timedelta(days=d, minutes=10), T+timedelta(days=d, minutes=10)) for d in range(6))
        report = evaluate_archive(MemoryArchive(data, conflicts=conflicts),
            config(archive_mode="observed", expected_session_intervals=intervals))
        rows = tuple(row for row in report.rows if row.model == "pipeline")
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(not row.data_gate_passed and "published_archive_identity_conflict" in row.reasons for row in rows))
        self.assertEqual(report.summary["models"]["pipeline"]["data_unavailable_decisions"], 6)
        self.assertIsNone(report.summary["models"]["pipeline"]["complete_period_net_pnl"])

    def test_conflict_published_during_replay_remains_an_unscored_outcome(self):
        data = corpus()
        conflicts = []
        for day in range(6):
            cutoff = T+timedelta(days=day, minutes=10)
            book = next(q for q in data[1] if q.received_at == cutoff and q.contract_id == "FIX-CALL50100")
            conflicts.append(ArchiveConflict("option", book.contract_id, book.provenance.source,
                book.provenance.evidence_kind, "1"*64, book.source_at, book.received_at,
                book.sequence, cutoff+timedelta(seconds=10), "2"*64, "3"*64, "4"*64))
        intervals = tuple((T+timedelta(days=d, minutes=10), T+timedelta(days=d, minutes=10)) for d in range(6))
        report = evaluate_archive(MemoryArchive(data, conflicts=conflicts),
            config(archive_mode="observed", expected_session_intervals=intervals))
        rows = tuple(row for row in report.rows if row.model == "pipeline")
        self.assertTrue(all(row.status == "candidate" and row.data_gate_passed for row in rows))
        self.assertTrue(all(row.replay_status != "completed" and row.net_pnl is None for row in rows))
        summary = report.summary["models"]["pipeline"]
        self.assertEqual(summary["missing_outcomes"], 6)
        self.assertEqual(summary["scored"], 0)
        self.assertIsNone(summary["complete_period_net_pnl"])

    def test_rejected_neutral_inputs_are_unavailable_not_complete_zero_pnl(self):
        contracts, quotes, observations = corpus()
        neutral = tuple(replace(row, value=D("50000")) for row in observations)
        mixed = tuple(replace(row, provenance=replace(row.provenance, evidence_kind="live_verified")) for row in neutral)
        stale = tuple(replace(row, source_at=row.received_at-timedelta(seconds=6)) for row in quotes)
        intervals = tuple((T+timedelta(days=day, minutes=10), T+timedelta(days=day, minutes=10)) for day in range(6))
        for books, underlying in ((quotes, mixed), ((), neutral), (stale, neutral)):
            with self.subTest(books=len(books), mixed=underlying is mixed):
                report = evaluate_archive(MemoryArchive((contracts, books, underlying)),
                    config(archive_mode="observed", expected_session_intervals=intervals))
                rows = tuple(row for row in report.rows if row.model == "pipeline")
                self.assertEqual(len(rows), 6)
                self.assertTrue(all(not row.data_gate_passed and row.net_pnl is None for row in rows))
                summary = report.summary["models"]["pipeline"]
                self.assertEqual(summary["data_unavailable_decisions"], 6)
                self.assertEqual(summary["complete_session_count"], 0)
                self.assertIsNone(summary["complete_period_net_pnl"])
                self.assertTrue(all(day["complete_day_net_pnl"] is None for day in summary["per_day"]))

    def test_gated_neutral_abstention_remains_complete_without_a_trade(self):
        contracts, quotes, observations = corpus()
        neutral = tuple(replace(row, value=D("50000")) for row in observations)
        intervals = tuple((T+timedelta(days=day, minutes=10), T+timedelta(days=day, minutes=10)) for day in range(6))
        report = evaluate_archive(MemoryArchive((contracts, quotes, neutral)),
            config(archive_mode="observed", expected_session_intervals=intervals))
        rows = tuple(row for row in report.rows if row.model == "pipeline")
        self.assertTrue(all(row.status == "no_trade" and row.data_gate_passed for row in rows))
        self.assertTrue(all(row.net_pnl is None and row.positive_net_outcome is None for row in rows))
        summary = report.summary["models"]["pipeline"]
        self.assertEqual(summary["data_unavailable_decisions"], 0)
        self.assertEqual(summary["complete_session_count"], 6)
        self.assertEqual(summary["complete_period_net_pnl"], "0")
        self.assertEqual(summary["scored"], 0)

    def test_observed_spans_do_not_claim_requested_period_or_session_coverage(self):
        result = evaluate_archive(MemoryArchive(corpus()), config())
        self.assertEqual(result.summary["schedule_coverage"]["mode"], "observed_spans_only")
        self.assertIsNone(result.summary["schedule_coverage"]["expected_decision_count"])
        summary = result.summary["models"]["pipeline"]
        self.assertIsNone(summary["complete_period_net_pnl"])
        self.assertTrue(all(day["complete_day_net_pnl"] is None for day in summary["per_day"]))

    def test_explicit_expected_windows_count_missing_days_and_edges(self):
        contracts, quotes, underlying = corpus()
        missing_day = (T+timedelta(days=2)).date()
        underlying = tuple(u for u in underlying if u.received_at.date() != missing_day)
        intervals = tuple((T+timedelta(days=d, minutes=10), T+timedelta(days=d, minutes=11)) for d in range(6))
        cfg = config(expected_session_intervals=intervals, decision_interval_minutes=1)
        self.assertEqual(FrozenExperimentConfig.from_dict(cfg.to_dict()), cfg)
        result = evaluate_archive(MemoryArchive((contracts, quotes, underlying)), cfg)
        coverage = result.summary["schedule_coverage"]
        self.assertEqual(coverage["expected_decision_count"], 12)
        self.assertEqual(coverage["evaluated_decision_count"], 12)
        summary = result.summary["models"]["pipeline"]
        self.assertEqual(summary["data_unavailable_decisions"], 7)
        self.assertIsNone(summary["complete_period_net_pnl"])
        missing = tuple(r for r in result.rows if r.model == "pipeline" and r.session_date == missing_day)
        self.assertEqual(len(missing), 2)
        self.assertTrue(all(not r.data_gate_passed for r in missing))

    def test_verified_origin_with_synthetic_five_second_outcomes_never_calibrates(self):
        contracts, quotes, underlying = corpus(kind="live_verified")
        changed = tuple(replace(q, provenance=replace(q.provenance, evidence_kind="synthetic"))
                        if q.received_at.minute == 10 and q.received_at.second > 0 else q for q in quotes)
        result = evaluate_archive(MemoryArchive((contracts, changed, underlying)), config(archive_mode="observed"))
        self.assertEqual(result.summary["models"]["pipeline"]["scored"], 0)
        self.assertTrue(all(r.evidence_kind == "synthetic" and r.net_pnl is None for r in result.rows))
        self.assertTrue(all(f["calibration_model"]["status"] == "unavailable" for f in result.folds))
        self.assertFalse(result.validated)

    def test_synthetic_monitoring_only_and_changed_provider_are_ineligible(self):
        contracts, quotes, underlying = corpus(kind="live_verified")
        for kind in ("synthetic", "mixed_source"):
            changed = tuple(replace(q, provenance=replace(q.provenance,
                evidence_kind="synthetic" if kind == "synthetic" else "live_verified",
                source="different-feed" if kind == "mixed_source" else q.provenance.source))
                if q.received_at.minute == 10 and q.received_at.second == 30 else q for q in quotes)
            result = evaluate_archive(MemoryArchive((contracts, changed, underlying)), config(archive_mode="observed"))
            self.assertEqual(result.summary["models"]["pipeline"]["scored"], 0)
            self.assertEqual(result.summary["evidence_kinds"], [kind])
            self.assertTrue(all(f["calibration_model"]["status"] == "unavailable" for f in result.folds))

    def test_end_to_end_synthetic_report_is_explicit_and_never_calibrated(self):
        archive = MemoryArchive(corpus())
        before = (archive.contracts, archive.quotes, archive.underlying)
        result = evaluate_archive(archive, config())
        self.assertEqual(result.status, "completed_research")
        self.assertEqual(len(result.rows), 18)
        self.assertEqual(result.summary["models"]["pipeline"]["scored"], 6)
        self.assertEqual(result.summary["models"]["pipeline"]["missing_outcomes"], 0)
        self.assertEqual(result.summary["evidence_kinds"], ["synthetic"])
        self.assertEqual(len(result.folds), 3)
        self.assertTrue(result.folds[-1]["holdout"])
        self.assertFalse(result.validated)
        self.assertEqual(result.actual_fills_created, 0)
        self.assertEqual(result.config.expected_archive_fingerprint, result.archive_fingerprint)
        self.assertTrue(all(fold["calibration_model"]["status"] == "unavailable" for fold in result.folds))
        self.assertEqual(before, (archive.contracts, archive.quotes, archive.underlying))

    def test_config_roundtrip_frozen_hash_and_execution_embargo(self):
        original = config()
        restored = FrozenExperimentConfig.from_dict(original.to_dict())
        self.assertEqual(restored, original)
        self.assertEqual(restored.fingerprint, original.fingerprint)
        self.assertEqual(original.effective_embargo_seconds, 95)
        with self.assertRaises(ValueError):
            replace(original, embargo_seconds=60)
        with self.assertRaises(ValueError):
            FrozenExperimentConfig.from_dict(dict(original.to_dict(), unknown="extra"))

    def test_past_signals_unchanged_when_all_future_prices_change(self):
        data = corpus()
        cutoff = T+timedelta(days=5)
        changed = (data[0], tuple(replace(q, bid=q.bid*2, ask=q.ask*2) if q.received_at >= cutoff else q for q in data[1]),
                   tuple(replace(u, value=D("100000")-u.value) if u.received_at >= cutoff else u for u in data[2]))
        first = evaluate_archive(MemoryArchive(data), config())
        second = evaluate_archive(MemoryArchive(changed), config())
        self.assertEqual(tuple(r for r in first.rows if r.decision_at < cutoff),
                         tuple(r for r in second.rows if r.decision_at < cutoff))
        self.assertEqual(first.folds[-1]["calibration_model"], second.folds[-1]["calibration_model"])
        with self.assertRaisesRegex(ValueError, "fingerprint changed"):
            evaluate_archive(MemoryArchive(changed), first.config)

    def test_missing_quote_outcomes_are_unavailable_not_losses_or_zeroes(self):
        contracts, quotes, underlying = corpus()
        day = (T+timedelta(days=4)).date()
        quotes = tuple(q for q in quotes if q.received_at.date() != day or q.received_at < T+timedelta(days=4, minutes=10, seconds=15))
        result = evaluate_archive(MemoryArchive((contracts, quotes, underlying)), config())
        summary = result.summary["models"]["pipeline"]
        self.assertEqual(summary["candidates"], 6)
        self.assertEqual(summary["scored"], 5)
        self.assertEqual(summary["missing_outcomes"], 1)
        self.assertIsNone(summary["complete_period_net_pnl"])
        missing = next(row for row in result.rows if row.model == "pipeline" and row.session_date == day)
        self.assertIsNone(missing.net_pnl)
        self.assertIsNone(missing.positive_net_outcome)

    def test_underlying_only_or_receipt_only_cannot_supply_option_outcomes(self):
        contracts, quotes, underlying = corpus()
        for data in (((), (), underlying), (contracts, quotes, tuple(replace(u, source_at=None) for u in underlying))):
            result = evaluate_archive(MemoryArchive(data), config())
            self.assertEqual(result.summary["models"]["pipeline"]["scored"], 0)
            self.assertEqual(result.actual_fills_created, 0)
            self.assertFalse(any(row.positive_net_outcome is not None for row in result.rows))

    def test_observed_mode_does_not_backdate_a_later_import(self):
        archive = MemoryArchive(corpus(), imported_at=T+timedelta(days=10))
        result = evaluate_archive(archive, config(archive_mode="observed"))
        self.assertEqual(result.status, "unavailable_no_underlying")
        self.assertEqual(result.rows, ())

    def test_insufficient_sessions_and_missing_execution_inputs_remain_visible(self):
        result = evaluate_archive(MemoryArchive(corpus(2)), config(2, replay_policy=ReplayPolicy()))
        self.assertEqual(result.status, "insufficient_sessions")
        self.assertEqual(result.folds, ())
        self.assertEqual(result.summary["models"]["pipeline"]["missing_outcomes"], 2)

    def test_cancellation_and_max_decisions_do_not_modify_archive(self):
        archive = MemoryArchive(corpus())
        result = evaluate_archive(archive, config(), cancelled=lambda: True)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.rows, ())
        self.assertEqual(result.actual_fills_created, 0)
        with self.assertRaisesRegex(ValueError, "max_decisions"):
            evaluate_archive(archive, config(max_decisions=1))

    def test_heldout_dates_never_fit_and_actual_label_availability_is_purged(self):
        rows = calibration_rows(8)
        setup = config(8, min_train_sessions=2)
        heldout = evaluation_folds(rows, setup)[-1]
        self.assertNotIn(rows[-1].session_date.isoformat(), heldout["train_dates"])
        self.assertFalse(heldout["test_labels_used_for_fitting"])
        self.assertFalse(heldout["validation_used_for_parameter_selection"])
        late = replace(rows[0], label_available_at=rows[-1].decision_at+timedelta(seconds=1))
        altered = evaluation_folds((late, *rows[1:]), setup)[-1]
        self.assertEqual(altered["training_purged_decisions"], 1)

    def test_real_sqlite_archive_read_only_cli_and_report_roundtrip(self):
        data = corpus(1)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root/"market.sqlite3"
            archive = MarketArchive(path)
            archive.append_batch(contracts=data[0], quotes=data[1], underlying=data[2],
                manifest=ImportManifest("evaluation-fixture", T+timedelta(days=2), b'{"fixture":"invented"}', evidence_kind="synthetic"))
            before = path.read_bytes()
            cfg = config(1)
            frozen = freeze_experiment(path, cfg)
            self.assertIsNotNone(frozen.expected_archive_fingerprint)
            config_path, report_path, frozen_path = root/"config.json", root/"report.json", root/"frozen.json"
            config_path.write_text(json.dumps(cfg.to_dict()), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["--archive", str(path), "--config", str(config_path), "--output", str(report_path),
                             "--frozen-config", str(frozen_path)])
            self.assertEqual(code, 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "insufficient_sessions")
            self.assertEqual(report["actual_fills_created"], 0)
            self.assertEqual(FrozenExperimentConfig.from_dict(json.loads(frozen_path.read_text())).fingerprint,
                             report["experiment_fingerprint"])
            self.assertEqual(path.read_bytes(), before)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
                main(["--archive", str(path), "--config", str(config_path), "--output", str(report_path)])
            self.assertEqual(failure.exception.code, 2)


class CalibrationTests(unittest.TestCase):
    def fit(self, rows, **changes):
        return fit_calibration(rows, **dict({"min_samples": 20, "min_bin_samples": 10,
            "min_days": 10, "min_class_samples": 5}, **changes))

    def test_known_empirical_bin_and_brier_are_data_backed_but_unvalidated(self):
        model = self.fit(calibration_rows())
        self.assertEqual(model.status, "experimental_verified_observations")
        self.assertEqual(model.predict(D("0.4")), D("0.5"))
        self.assertFalse(model.validated)
        later = tuple(replace(r, decision_at=r.decision_at+timedelta(days=30), session_date=(r.decision_at+timedelta(days=30)).date()) for r in calibration_rows())
        metrics = calibration_metrics(model, later)
        self.assertEqual(D(metrics["brier_score"]), D("0.25"))
        self.assertEqual(metrics["predicted_and_labelled"], 20)
        self.assertFalse(metrics["validated"])

    def test_training_scores_are_explicitly_in_sample_and_never_pass_as_future(self):
        rows = calibration_rows()
        model = self.fit(rows)
        training = calibration_metrics(model, rows, sample_role="in_sample")
        self.assertEqual(training["sample_role"], "in_sample")
        self.assertEqual(training["predicted_and_labelled"], 20)
        future = calibration_metrics(model, rows)
        self.assertEqual(future["predicted_and_labelled"], 0)
        self.assertIsNone(future["brier_score"])
        report = evaluate_archive(MemoryArchive(corpus()), config())
        self.assertEqual(report.folds[0]["training"]["calibration"]["sample_role"], "in_sample")
        self.assertEqual(report.folds[0]["test"]["calibration"]["sample_role"], "out_of_sample")

    def test_synthetic_never_fits_even_with_explicit_unverified_optin(self):
        model = self.fit(calibration_rows(kind="synthetic"), allow_unverified=True)
        self.assertEqual(model.status, "unavailable")
        self.assertIsNone(model.predict(D("0.4")))
        self.assertIsNone(model.base_rate)

    def test_unverified_corpus_requires_explicit_optin_and_keeps_label(self):
        rows = calibration_rows(kind="replay_quote")
        self.assertEqual(self.fit(rows).status, "unavailable")
        model = self.fit(rows, allow_unverified=True)
        self.assertEqual(model.status, "experimental_unverified")
        self.assertEqual(model.predict(D("0.4")), D("0.5"))
        self.assertFalse(model.validated)

    def test_small_n_single_day_single_class_and_sparse_bins_are_unknown(self):
        for rows in (calibration_rows(3), calibration_rows(one_day=True),
                     tuple(replace(r, net_pnl=D("1"), positive_net_outcome=True) for r in calibration_rows()),
                     tuple(replace(r, strength=D(index % 4)/4) for index, r in enumerate(calibration_rows()))):
            model = self.fit(rows)
            self.assertEqual(model.status, "unavailable")
            self.assertIsNone(model.predict(D("0.4")))

    def test_duplicate_decisions_do_not_inflate_sample_size_and_conflicts_fail(self):
        row = calibration_rows()[0]
        model = self.fit((row,)*100)
        self.assertEqual(model.samples, 1)
        self.assertEqual(model.status, "unavailable")
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.fit((row, replace(row, net_pnl=D("-1"), positive_net_outcome=False)))

    def test_mismatched_labels_or_evidence_cannot_receive_calibration_credit(self):
        rows = calibration_rows()
        with self.assertRaisesRegex(ValueError, "label must match"):
            self.fit((replace(rows[0], positive_net_outcome=False), *rows[1:]))
        model = self.fit(rows)
        metrics = calibration_metrics(model, calibration_rows(kind="synthetic"))
        self.assertEqual(metrics["predicted_and_labelled"], 0)
        self.assertIsNone(metrics["brier_score"])


if __name__ == "__main__":
    unittest.main()

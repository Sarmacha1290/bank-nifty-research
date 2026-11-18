"""Closed-bar pipeline checks over deterministic synthetic source records."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

from banknifty.options_data import ContractMetadata, OptionQuote, QualityPolicy, RawProvenance
from banknifty.options_pipeline import (
    UnderlyingObservation, PipelinePolicy, PipelineSession, assess_direction,
    build_closed_bars, closed_boundary, generate_baseline, run_pipeline,
)
from banknifty.options_strategy import ResearchPolicy, generate_signal, point_in_time_inputs
from banknifty.market_archive import ArchiveConflict

D = Decimal
NOW = datetime(2026, 10, 15, 5, 0, tzinfo=timezone.utc)
SYMBOL = "SYNTH-BANKNIFTY"


def underlying_fixture(direction="BULLISH", *, cutoff=NOW, kind="synthetic"):
    result = []
    for minute in range(10):
        start = cutoff - timedelta(minutes=10 - minute)
        base = D("55000") + D(minute * 20) if direction == "BULLISH" else D("55300") - D(minute * 20) if direction == "BEARISH" else D("55100")
        for second in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 59):
            stamp = start + timedelta(seconds=second)
            value = base
            result.append(UnderlyingObservation(SYMBOL, value, stamp, stamp,
                          RawProvenance("synthetic-index", f"{stamp.isoformat()}:{value}".encode(), stamp, kind), len(result)))
    return tuple(result)


def option_fixture(*, cutoff=NOW, kind="synthetic"):
    known = cutoff - timedelta(days=1)
    contracts, quotes = [], []
    for option_type in ("CALL", "PUT"):
        for strike in range(54900, 55500, 100):
            contract = ContractMetadata(f"SYNTH-{option_type}-{strike}", SYMBOL, "SYNTHETIC", option_type, D(strike),
                cutoff + timedelta(days=7), 25, D("0.05"), known, None, known, True,
                RawProvenance("synthetic-catalogue", f"{option_type}:{strike}".encode(), known, kind))
            ask = D(100) + D((strike - 54900) // 10) * (-1 if option_type == "CALL" else 1)
            quote = OptionQuote(contract.contract_id, cutoff - timedelta(seconds=1), cutoff, ask-D(1), ask,
                RawProvenance("synthetic-options", f"{option_type}:{strike}:{ask}".encode(), cutoff, kind),
                bid_size=1000, ask_size=1000, open_interest=0, oi_source_at=cutoff-timedelta(seconds=1))
            contracts.append(contract)
            quotes.append(quote)
    return tuple(contracts), tuple(quotes)


def run_fixture(direction="BULLISH", *, rows=None, as_of=NOW, **kwargs):
    contracts, quotes = option_fixture()
    return run_pipeline(underlying_fixture(direction) if rows is None else rows, contracts, quotes,
                        as_of=as_of, strategy="DEBIT_SPREAD", **kwargs)


class ObservationTests(unittest.TestCase):
    def test_receipt_only_is_preserved_and_not_relabelled(self):
        rows = underlying_fixture()
        unknown = tuple(replace(row, source_at=None) for row in rows)
        result = run_fixture(rows=unknown)
        self.assertEqual(result.status, "no_trade")
        self.assertEqual(result.label, "receipt_only")
        self.assertIsNone(result.signal)
        self.assertEqual(result.observed_inputs[0].source_at, None)
        self.assertIn("underlying_unknown_source_freshness", result.reasons)

    def test_invalid_values_timestamps_sequences_reject_at_interface(self):
        row = underlying_fixture()[0]
        for changes in ({"value": D("0")}, {"value": D("NaN")}, {"received_at": NOW.replace(tzinfo=None)},
                        {"source_at": NOW.replace(tzinfo=None)}, {"sequence": -1}, {"sequence": True}, {"symbol": "a\nb"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(row, **changes)
        with self.assertRaises(FrozenInstanceError):
            row.value = D("1")

    def test_raw_provenance_is_included_without_invented_volume_or_oi(self):
        row = underlying_fixture()[0]
        payload = json.loads(json.dumps(row.to_dict()))
        self.assertEqual(payload["provenance"]["payload"]["data"], row.provenance.payload.hex())
        self.assertNotIn("volume", payload)
        self.assertNotIn("open_interest", payload)


class ClosedBarTests(unittest.TestCase):
    def test_exact_closed_intervals_and_ohlc(self):
        rows = list(underlying_fixture())
        rows[-3] = replace(rows[-3], value=D("55195"))
        rows[-2] = replace(rows[-2], value=D("55170"))
        bars, retained, failures = build_closed_bars(rows, as_of=NOW)
        self.assertEqual(failures, ())
        self.assertEqual(len(bars), 10)
        self.assertEqual(bars[-1].start_at, NOW-timedelta(minutes=1))
        self.assertEqual(bars[-1].end_at, NOW)
        self.assertEqual((bars[-1].open, bars[-1].high, bars[-1].low, bars[-1].close), (D(55180), D(55195), D(55170), D(55180)))
        self.assertEqual(bars[-1].sample_count, 12)
        self.assertEqual(bars[-1].kind, "sampled_one_minute_ohlc")
        self.assertEqual(retained, tuple(rows))

    def test_tick_at_boundary_belongs_to_next_bar_and_does_not_change_identity(self):
        rows = underlying_fixture()
        baseline = run_fixture(rows=rows)
        next_bar = replace(rows[-1], source_at=NOW, received_at=NOW, value=D("999999"), sequence=120,
                           provenance=RawProvenance("different-provider-next-bar", b"next", NOW, "live_verified"))
        actual = run_fixture(rows=(*rows, next_bar))
        self.assertEqual(actual, baseline)

    def test_future_source_receipt_or_provenance_is_excluded(self):
        rows = underlying_fixture()
        baseline = run_fixture(rows=rows)
        later = NOW + timedelta(seconds=1)
        possibilities = (replace(rows[-1], source_at=later, received_at=later),
                         replace(rows[-1], received_at=later),
                         replace(rows[-1], provenance=replace(rows[-1].provenance, captured_at=later)))
        for future in possibilities:
            self.assertEqual(run_fixture(rows=(*rows, future)), baseline)

    def test_gap_missing_edge_and_insufficient_samples_abstain(self):
        rows = underlying_fixture()
        samples = (tuple(row for index, row in enumerate(rows) if index != 36),
                   tuple(row for row in rows if not NOW-timedelta(minutes=4) <= row.source_at < NOW-timedelta(minutes=3)))
        for reduced in samples:
            result = run_fixture(rows=reduced)
            self.assertEqual(result.status, "no_trade")
            self.assertIsNone(result.assessment)
        sparse_policy = PipelinePolicy(minimum_samples_per_minute=2)
        sparse = tuple(row for index, row in enumerate(rows) if index % 12 in (0, 11))
        result = run_fixture(rows=sparse, pipeline_policy=sparse_policy)
        self.assertTrue(any("internal_sample_gap" in reason for reason in result.reasons))

    def test_late_receipts_and_decision_staleness_abstain(self):
        rows = list(underlying_fixture())
        rows[0] = replace(rows[0], received_at=rows[0].source_at+timedelta(seconds=6))
        result = run_fixture(rows=rows)
        self.assertIn("underlying_source_receipt_latency_rejected", result.reasons)
        delayed = run_fixture(as_of=NOW+timedelta(seconds=6))
        self.assertIn("decision_after_closed_boundary_window", delayed.reasons)
        self.assertIn("latest_underlying_source_stale", delayed.reasons)

    def test_duplicate_reconnect_observation_keeps_first_receipt(self):
        rows = underlying_fixture()
        repeated = replace(rows[-1], received_at=NOW, provenance=replace(rows[-1].provenance, captured_at=NOW))
        bars, retained, reasons = build_closed_bars((*rows, repeated), as_of=NOW)
        self.assertEqual(reasons, ())
        self.assertEqual(bars[-1].sample_count, 12)
        self.assertEqual(bars[-1].observations[-1].received_at, rows[-1].received_at)
        self.assertEqual(len(retained), 121)

    def test_conflicts_source_switch_and_sequence_regression_block(self):
        rows = list(underlying_fixture())
        changes = (replace(rows[-1], value=D("1")),
                   replace(rows[-1], provenance=replace(rows[-1].provenance, source="other")),
                   replace(rows[-1], sequence=1))
        for bad in changes:
            result = run_fixture(rows=(*rows[:-1], bad))
            self.assertEqual(result.status, "no_trade")
        conflict = run_fixture(rows=(*rows, replace(rows[-1], value=D("1"))))
        self.assertIn("underlying_conflicting_source_timestamp", conflict.reasons)

    def test_same_source_duplicate_advances_sequence_high_water(self):
        rows = underlying_fixture()
        repeated_at = rows[-1].received_at + timedelta(milliseconds=100)
        repeated = replace(rows[-1], sequence=200, received_at=repeated_at,
                           provenance=replace(rows[-1].provenance, captured_at=repeated_at))
        later_at = rows[-1].source_at + timedelta(milliseconds=500)
        regressed = replace(rows[-1], sequence=150, source_at=later_at, received_at=later_at,
                            provenance=replace(rows[-1].provenance, captured_at=later_at))
        result = run_fixture(rows=(*rows, repeated, regressed))
        self.assertEqual(result.status, "no_trade")
        self.assertIn("underlying_sequence_regressed_or_disappeared", result.reasons)
        permitted = run_fixture(rows=(*rows, repeated))
        self.assertEqual(permitted.status, "candidate")
        self.assertEqual(permitted.bars[-1].sample_count, 12)

    def test_permutations_have_identical_result_and_diagnostics(self):
        rows = underlying_fixture()
        self.assertEqual(run_fixture(rows=rows), run_fixture(rows=reversed(rows)))
        bad = replace(rows[-1], value=D("1"))
        self.assertEqual(run_fixture(rows=(*rows, bad)), run_fixture(rows=(bad, *reversed(rows))))

    def test_timezone_offsets_do_not_change_boundary_or_fingerprint(self):
        local = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
        self.assertEqual(closed_boundary(local), NOW)
        self.assertEqual(run_fixture(as_of=local).input_fingerprint, run_fixture().input_fingerprint)


class PipelineTests(unittest.TestCase):
    def test_direction_is_derived_for_bullish_bearish_and_neutral(self):
        for direction in ("BULLISH", "BEARISH", "NEUTRAL"):
            result = run_fixture(direction)
            self.assertEqual(result.assessment.direction, direction)
            self.assertEqual(result.status, "no_trade" if direction == "NEUTRAL" else "candidate")
            self.assertIsNone(result.assessment.confidence)
            self.assertGreaterEqual(result.assessment.strength, 0)
            self.assertLessEqual(result.assessment.strength, 1)
            self.assertEqual(result.signal.underlying_quality, "timestamped_closed_bar_structural_checks_passed")
            self.assertEqual(result.signal.underlying_price, result.bars[-1].close)
            self.assertFalse(result.trading_allowed)
            self.assertFalse(result.signal.trading_allowed)

    def test_disagreement_is_neutral(self):
        bars, _, _ = build_closed_bars(underlying_fixture(), as_of=NOW)
        closes = [D(100)]*6+[D(120),D(150),D(130),D(110)]
        changed = tuple(replace(bar, close=close) for bar, close in zip(bars, closes))
        assessment = assess_direction(changed)
        self.assertLess(assessment.momentum_return, 0)
        self.assertGreater(assessment.fast_mean, assessment.slow_mean)
        self.assertEqual(assessment.direction, "NEUTRAL")

    def test_options_remain_gated_and_exact_underlying_identity_required(self):
        contracts, quotes = option_fixture()
        mismatched = [replace(c, underlying="OTHER") for c in contracts]
        result = run_pipeline(underlying_fixture(), mismatched, quotes, as_of=NOW, strategy="LONG_OPTION")
        self.assertIn("underlying_and_option_catalogue_identity_mismatch", result.reasons)
        stale = [replace(q, source_at=NOW-timedelta(seconds=6)) for q in quotes]
        result = run_pipeline(underlying_fixture(), contracts, stale, as_of=NOW, strategy="LONG_OPTION")
        self.assertEqual(result.status, "no_trade")
        self.assertIsNotNone(result.assessment)

    def test_neutral_abstention_requires_complete_option_data_gates(self):
        contracts, quotes = option_fixture()
        rows = underlying_fixture("NEUTRAL")
        valid = run_pipeline(rows, contracts, quotes, as_of=NOW, strategy="DEBIT_SPREAD")
        self.assertEqual(valid.status, "no_trade")
        self.assertTrue(valid.data_gate_passed)
        self.assertEqual(valid.signal.direction, "NEUTRAL")
        variants = (
            (contracts, ()),
            ((), quotes),
            (contracts, tuple(replace(q, source_at=NOW-timedelta(seconds=6)) for q in quotes)),
            (tuple(replace(c, underlying="OTHER") for c in contracts), quotes),
            (contracts, (*quotes, replace(quotes[0], bid=quotes[0].bid-D(1)))),
            (contracts, tuple(replace(q, provenance=replace(q.provenance, evidence_kind="live_verified")) for q in quotes)),
        )
        for metadata, books in variants:
            with self.subTest(metadata=len(metadata), books=len(books)):
                failed = run_pipeline(rows, metadata, books, as_of=NOW, strategy="DEBIT_SPREAD")
                self.assertEqual(failed.assessment.direction, "NEUTRAL")
                self.assertFalse(failed.data_gate_passed)
                self.assertIsNone(failed.signal)
                self.assertIsNone(generate_baseline(failed, metadata, books, direction="BULLISH", strategy="DEBIT_SPREAD"))

    def test_published_conflicts_block_only_affected_visible_inputs(self):
        contracts, quotes = option_fixture()
        underlying = underlying_fixture()
        clean = run_pipeline(underlying, contracts, quotes, as_of=NOW, strategy="DEBIT_SPREAD")
        for kind, item in (("catalogue", contracts[0]), ("option", quotes[0]), ("underlying", underlying[0])):
            event = ArchiveConflict(kind, getattr(item, "contract_id", getattr(item, "symbol", None)),
                item.provenance.source, item.provenance.evidence_kind, "1"*64,
                getattr(item, "source_at", None), getattr(item, "received_at", getattr(item, "known_at", None)),
                getattr(item, "sequence", None), NOW, "2"*64, "3"*64, "4"*64,
                getattr(item, "effective_from", None), getattr(item, "known_at", None))
            with self.subTest(kind=kind):
                rejected = run_pipeline(underlying, contracts, quotes, as_of=NOW, strategy="DEBIT_SPREAD",
                                        conflict_events=(event,))
                self.assertFalse(rejected.data_gate_passed)
                self.assertIsNone(rejected.signal)
                self.assertIn("published_archive_identity_conflict", rejected.reasons)
                self.assertEqual(rejected.conflict_events, (event,))
                self.assertNotEqual(clean.input_fingerprint, rejected.input_fingerprint)
                for ignored in (replace(event, available_at=NOW+timedelta(microseconds=1)),
                                replace(event, symbol="UNRELATED")):
                    actual = run_pipeline(underlying, contracts, quotes, as_of=NOW, strategy="DEBIT_SPREAD",
                                          conflict_events=(ignored,))
                    self.assertEqual(actual, clean)
                self.assertEqual(run_pipeline(underlying, contracts, quotes, as_of=NOW, strategy="DEBIT_SPREAD",
                    conflict_events=(event, event)), rejected)

    def test_synthetic_replay_and_live_unverified_labels_remain_distinct(self):
        for kind, label in (("synthetic", "synthetic"), ("replay_quote", "replay"), ("live_unverified", "live_unverified"), ("live_verified", "live_data_verified_research_unvalidated")):
            contracts, quotes = option_fixture(kind=kind)
            result = run_pipeline(underlying_fixture(kind=kind), contracts, quotes, as_of=NOW, strategy="LONG_OPTION")
            self.assertEqual(result.label, label)
            self.assertEqual(result.status, "candidate")
            self.assertIsNone(result.confidence)
        contracts, quotes = option_fixture(kind="live_verified")
        mixed = run_pipeline(underlying_fixture(), contracts, quotes, as_of=NOW, strategy="LONG_OPTION")
        self.assertEqual(mixed.status, "no_trade")
        self.assertIn("pipeline_mixed_evidence_kinds", mixed.reasons)

    def test_historical_candle_and_required_live_evidence_block(self):
        historical = run_fixture(rows=underlying_fixture(kind="historical_candle"))
        self.assertEqual(historical.label, "candle_approximate")
        self.assertEqual(historical.status, "no_trade")
        strict = run_fixture(pipeline_policy=PipelinePolicy(require_live_verified=True))
        self.assertEqual(strict.status, "no_trade")
        strict_options = run_fixture(quality_policy=QualityPolicy(require_live_verified=True))
        self.assertEqual(strict_options.status, "no_trade")

    def test_future_underlying_and_option_mutations_cannot_change_earlier_decision(self):
        rows = underlying_fixture()
        contracts, quotes = option_fixture()
        expected = run_fixture()
        later = NOW+timedelta(hours=1)
        future_row = replace(rows[-1], value=D("1"), source_at=later, received_at=later,
                             provenance=replace(rows[-1].provenance, captured_at=later))
        future_contract = replace(contracts[0], lot_size=999, known_at=later, effective_from=later,
                                  provenance=replace(contracts[0].provenance, captured_at=later))
        future_quote = replace(quotes[0], bid=D("1"), ask=D("1"), source_at=later, received_at=later,
                               provenance=replace(quotes[0].provenance, captured_at=later))
        actual = run_pipeline((*rows, future_row), (*contracts, future_contract), (*quotes, future_quote), as_of=NOW, strategy="DEBIT_SPREAD")
        self.assertEqual(actual, expected)

    def test_signal_fingerprint_covers_derived_history_not_just_final_price(self):
        original = run_fixture()
        rows = list(underlying_fixture())
        rows[5] = replace(rows[5], value=D("55003"))
        revised = run_fixture(rows=rows)
        self.assertEqual(original.signal.underlying_price, revised.signal.underlying_price)
        self.assertNotEqual(original.signal.input_fingerprint, revised.signal.input_fingerprint)
        with self.assertRaises(FrozenInstanceError):
            original.bars[-1].close = D("1")

    def test_manual_hypothesis_mode_is_preserved(self):
        contracts, quotes = option_fixture()
        result = generate_signal(contracts, quotes, as_of=NOW, underlying_price=D("55180"), direction="BULLISH", strategy="DEBIT_SPREAD")
        self.assertEqual(result.underlying_quality, "caller_supplied_unverified")
        self.assertNotEqual(result.signal_id, run_fixture().signal.signal_id)
        self.assertEqual(point_in_time_inputs(contracts, quotes, NOW)[0], tuple(sorted(contracts,key=lambda c:c.contract_id)))

    def test_baseline_reuses_gates_and_never_rescues_bad_data(self):
        contracts, quotes = option_fixture()
        qualified = run_fixture("NEUTRAL")
        baseline = generate_baseline(qualified, contracts, quotes, direction="BULLISH", strategy="LONG_OPTION")
        self.assertEqual(baseline.status, "candidate")
        self.assertIn("baseline", baseline.reasons[-1])
        failed = run_fixture(rows=())
        self.assertIsNone(generate_baseline(failed, contracts, quotes, direction="BULLISH", strategy="LONG_OPTION"))

    def test_baseline_cannot_substitute_evidence_identity_or_policy(self):
        contracts, quotes = option_fixture(kind="live_verified")
        qualified = run_pipeline(underlying_fixture(kind="live_verified"), contracts, quotes, as_of=NOW, strategy="LONG_OPTION")
        synthetic_contracts, synthetic_quotes = option_fixture()
        with self.assertRaisesRegex(ValueError, "identical frozen"):
            generate_baseline(qualified, synthetic_contracts, synthetic_quotes, direction="BULLISH", strategy="LONG_OPTION")
        different = tuple(replace(contract, underlying="OTHER") for contract in contracts)
        with self.assertRaises(ValueError):
            generate_baseline(qualified, different, quotes, direction="BULLISH", strategy="LONG_OPTION")
        with self.assertRaises(ValueError):
            generate_baseline(qualified, contracts, quotes, direction="BULLISH", strategy="LONG_OPTION", research_policy=ResearchPolicy(horizon_minutes=15))


class SessionTests(unittest.TestCase):
    def test_exact_boundary_is_emitted_only_once_and_checkpoint_survives_restart(self):
        contracts, quotes = option_fixture()
        rows = underlying_fixture()
        session = PipelineSession()
        first = session.process(rows, contracts, quotes, as_of=NOW, strategy="DEBIT_SPREAD")
        self.assertTrue(first.emitted)
        self.assertEqual(first.decision.status, "candidate")
        changed = tuple(replace(row, value=row.value+D("1")) for row in rows)
        second = session.process(changed, contracts, quotes, as_of=NOW+timedelta(seconds=1), strategy="DEBIT_SPREAD")
        self.assertFalse(second.emitted)
        self.assertIsNone(second.decision)
        self.assertEqual(second.prior_fingerprint, first.decision.input_fingerprint)
        restarted = PipelineSession(session.checkpoint())
        self.assertFalse(restarted.process(rows, contracts, quotes, as_of=NOW, strategy="DEBIT_SPREAD").emitted)

    def test_abstention_is_frozen_and_late_data_cannot_rewrite_it(self):
        contracts, quotes = option_fixture()
        rows = underlying_fixture()
        session = PipelineSession()
        first = session.process(rows[:-1], contracts, quotes, as_of=NOW, strategy="LONG_OPTION")
        self.assertEqual(first.decision.status, "no_trade")
        self.assertFalse(session.process(rows, contracts, quotes, as_of=NOW, strategy="LONG_OPTION").emitted)

    def test_empty_first_attempt_later_data_and_changed_symbol_cannot_reemit(self):
        contracts, quotes = option_fixture()
        session = PipelineSession(namespace="explicit-banknifty-stream")
        empty = session.process((), contracts, quotes, as_of=NOW, strategy="LONG_OPTION")
        self.assertEqual(empty.decision.status, "no_trade")
        self.assertFalse(session.process(underlying_fixture(), contracts, quotes, as_of=NOW, strategy="LONG_OPTION").emitted)
        different_symbol = tuple(replace(row, symbol="OTHER") for row in underlying_fixture())
        self.assertFalse(session.process(different_symbol, contracts, quotes, as_of=NOW, strategy="LONG_OPTION").emitted)
        restarted = PipelineSession(session.checkpoint(), namespace="explicit-banknifty-stream")
        self.assertFalse(restarted.process(underlying_fixture(), contracts, quotes, as_of=NOW, strategy="LONG_OPTION").emitted)

    def test_irrelevant_old_symbol_does_not_change_boundary_key(self):
        contracts, quotes = option_fixture()
        session = PipelineSession()
        rows = underlying_fixture()
        first = session.process(rows, contracts, quotes, as_of=NOW, strategy="LONG_OPTION")
        old = replace(rows[0], symbol="OLD", source_at=NOW-timedelta(days=1), received_at=NOW-timedelta(days=1))
        duplicate = session.process((old,*rows), contracts, quotes, as_of=NOW, strategy="LONG_OPTION")
        self.assertFalse(duplicate.emitted)
        self.assertEqual(duplicate.prior_fingerprint, first.decision.input_fingerprint)

    def test_next_closed_boundary_gets_an_independent_attempt(self):
        contracts, quotes = option_fixture()
        session = PipelineSession()
        first = session.process(underlying_fixture(), contracts, quotes, as_of=NOW, strategy="LONG_OPTION")
        later = NOW+timedelta(minutes=1)
        cs, qs = option_fixture(cutoff=later)
        second = session.process(underlying_fixture(cutoff=later), cs, qs, as_of=later, strategy="LONG_OPTION")
        self.assertTrue(first.emitted)
        self.assertTrue(second.emitted)
        self.assertNotEqual(first.decision.boundary, second.decision.boundary)

    def test_invalid_policy_and_checkpoint_are_rejected(self):
        for kwargs in ({"minimum_samples_per_minute": 1}, {"max_decision_delay_seconds": -1}, {"require_live_verified": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                PipelinePolicy(**kwargs)
        with self.assertRaises(ValueError):
            PipelineSession((("short", "short"),))


if __name__ == "__main__":
    unittest.main()

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from banknifty.options_data import ContractMetadata, OptionQuote, RawProvenance, QualityPolicy
from banknifty.market_archive import ArchiveConflict
from banknifty.options_strategy import ResearchPolicy, generate_signal
from banknifty.options_replay import (
    ReplayPolicy, OhlcBar, replay_signal, candle_path_diagnostic,
    chronological_folds, summarize_replays,
)


D = Decimal
T = datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc)


def provenance(at, kind="synthetic"):
    return RawProvenance("offline-fixture", b"synthetic-test-only", at, kind)


def contract(identifier="CALL50000", strike="50000"):
    before = T - timedelta(days=1)
    return ContractMetadata(identifier, "BANKNIFTY", "NSE", "CALL", D(strike),
                            T + timedelta(days=10), 10, D("0.05"), before, None,
                            before, True, provenance(before))


def quote(seconds, bid="99", ask="100", *, identifier="CALL50000", kind="synthetic", **changes):
    at = T + timedelta(seconds=seconds)
    item = OptionQuote(identifier, at, at, D(bid), D(ask), provenance(at, kind),
                       bid_size=100, ask_size=100, sequence=seconds)
    return replace(item, **changes)


def signal(*, spread=False, quotes=None, metadata=None):
    contracts = metadata or (contract(),)
    observations = quotes or (quote(0),)
    if spread:
        contracts = (contract(), contract("CALL50100", "50100"))
        observations = (quote(0), quote(0, "59", "60", identifier="CALL50100"))
    return generate_signal(contracts, observations, as_of=T, underlying_price=D("50000"),
                           direction="BULLISH", strategy="DEBIT_SPREAD" if spread else "LONG_OPTION",
                           policy=ResearchPolicy(horizon_minutes=1))


def events(end=66):
    return tuple(quote(s, str(D("100") + D(s) / 10), str(D("101") + D(s) / 10)) for s in range(end))


def policy(**changes):
    return replace(ReplayPolicy(fees_total=D("3"), slippage_per_leg=D("0.05"), quantity=10), **changes)


def conflict_for(observation, available_at):
    return ArchiveConflict("option", observation.contract_id, observation.provenance.source,
                           observation.provenance.evidence_kind, "a"*64, observation.source_at,
                           observation.received_at, observation.sequence, available_at,
                           "b"*64, "c"*64, "d"*64)


class OptionReplayTests(unittest.TestCase):
    def test_published_conflict_on_frozen_origin_blocks_entry(self):
        fixed = signal(quotes=(quote(-1, sequence=0),))
        future = tuple(q for q in events() if q.received_at > T)
        for available_at in (T-timedelta(seconds=1), T, T+timedelta(seconds=2)):
            with self.subTest(available_at=available_at):
                fact = conflict_for(fixed.legs[0].quote, available_at)
                outcome = replay_signal(fixed, future, policy=policy(), conflict_events=(fact,))
                self.assertEqual(outcome.status, "unavailable")
                self.assertIsNone(outcome.net_pnl)
                self.assertFalse(outcome.entries)
                self.assertTrue(any("Quarantined archive conflicts" in reason for reason in outcome.reasons))

    def test_later_published_conflict_on_consumed_origin_or_quote_withholds_score(self):
        fixed = signal()
        rows = events()
        for observation in (fixed.legs[0].quote, rows[20]):
            fact = conflict_for(observation, T+timedelta(seconds=30))
            with self.subTest(observation_at=observation.source_at):
                outcome = replay_signal(fixed, rows, policy=policy(), conflict_events=(fact,))
                self.assertEqual(outcome.status, "incomplete")
                self.assertIsNone(outcome.net_pnl)
                self.assertEqual(outcome.entry_at, T+timedelta(seconds=5))
                self.assertEqual(outcome.exit_at, T+timedelta(seconds=65))
                self.assertEqual(summarize_replays((outcome,))["scored"], 0)

    def test_published_conflict_also_applies_when_affected_quote_arrives_later(self):
        rows = events()
        fact = conflict_for(rows[20], T+timedelta(seconds=18))
        outcome = replay_signal(signal(), rows, policy=policy(), conflict_events=(fact,))
        self.assertEqual(outcome.status, "incomplete")
        self.assertIsNone(outcome.net_pnl)
        self.assertIn(T+timedelta(seconds=20), outcome.gaps)

    def test_conflicts_after_resolved_exit_or_other_identities_do_not_rewrite_result(self):
        fixed = signal()
        rows = events()
        clean = replay_signal(fixed, rows, policy=policy())
        matching = conflict_for(rows[20], T+timedelta(seconds=30))
        for fact in (
                replace(matching, available_at=T+timedelta(seconds=66)),
                replace(matching, symbol="ANOTHER_CONTRACT"),
                replace(matching, source="another-provider"),
                replace(matching, sequence=1000),
                replace(matching, source_at=T+timedelta(microseconds=1)),
                replace(matching, evidence_kind="live_verified")):
            with self.subTest(fact=fact):
                self.assertEqual(clean, replay_signal(fixed, rows, policy=policy(), conflict_events=(fact,)))

    def test_conflict_available_at_exit_prevents_scoring_that_exit(self):
        fixed = signal()
        fact = conflict_for(fixed.legs[0].quote, T+timedelta(seconds=65))
        outcome = replay_signal(fixed, events(), policy=policy(), conflict_events=(fact,))
        self.assertEqual(outcome.status, "incomplete")
        self.assertIsNone(outcome.net_pnl)

    def test_frozen_origin_seeds_sequence_even_when_omitted_or_before_cutoff(self):
        future = tuple(q for q in events() if q.received_at > T)
        for offset in (0, -1):
            fixed = signal(quotes=(quote(offset, sequence=1000),))
            for include_origin in (False, True):
                with self.subTest(origin_offset=offset, include_origin=include_origin):
                    rows = (fixed.legs[0].quote, *future) if include_origin else future
                    outcome = replay_signal(fixed, rows, policy=policy())
                    self.assertEqual(outcome.status, "unavailable")
                    self.assertIsNone(outcome.net_pnl)
                    self.assertIsNone(outcome.entry_at)
                    self.assertEqual(outcome.entries, ())

    def test_frozen_origin_sequence_allows_subsequent_increasing_quotes(self):
        fixed = signal(quotes=(quote(-1, sequence=1000),))
        future = tuple(replace(q, sequence=1000+q.sequence) for q in events() if q.received_at > T)
        outcome = replay_signal(fixed, future, policy=policy())
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.net_pnl, D("46.00"))
        self.assertEqual(outcome.entry_at, T+timedelta(seconds=5))

    def test_source_or_evidence_switch_before_entry_cannot_recover_into_scored_pnl(self):
        metadata = replace(contract(), provenance=provenance(T-timedelta(days=1), "live_verified"))
        fixed = signal(metadata=(metadata,), quotes=(quote(0, kind="live_verified"),))
        clean = tuple(replace(q, provenance=provenance(q.received_at, "live_verified")) for q in events())
        for contamination in ("provider_change", "evidence_change"):
            changed = tuple(replace(q, provenance=replace(q.provenance,
                source="another-provider" if contamination == "provider_change" else q.provenance.source,
                evidence_kind="replay_quote" if contamination == "evidence_change" else "live_verified"))
                if q.received_at == T+timedelta(seconds=1) else q for q in clean)
            with self.subTest(contamination=contamination):
                outcome = replay_signal(fixed, changed, policy=policy())
                self.assertEqual(outcome.status, "unavailable")
                self.assertIsNone(outcome.net_pnl)
                self.assertIsNone(outcome.entry_at)
                self.assertEqual(outcome.source_continuity, contamination != "provider_change")
                self.assertEqual(outcome.evidence_kind, "mixed_source" if contamination == "provider_change" else "mixed")
                self.assertTrue(any("changed from the frozen origin" in reason for reason in outcome.reasons))
                self.assertEqual(summarize_replays((outcome,))["scored"], 0)

    def test_observed_first_capture_delays_execution_without_rewriting_timestamps(self):
        rows = events(71)
        availability = {q.fingerprint: q.received_at+timedelta(seconds=2) for q in rows}
        outcome = replay_signal(signal(), rows, policy=policy(), availability_times=availability)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.entry_at, T+timedelta(seconds=7))
        self.assertEqual(outcome.entries[0].received_at, T+timedelta(seconds=5))
        self.assertEqual(outcome.exit_at, T+timedelta(seconds=67))
        self.assertEqual(outcome.exits[0].received_at, T+timedelta(seconds=65))
        missing = replay_signal(signal(), rows, policy=policy(), availability_times={})
        self.assertEqual(missing.status, "unavailable")
        self.assertIsNone(missing.net_pnl)

    def test_monitoring_evidence_and_provider_changes_cannot_hide_behind_real_exit(self):
        metadata = replace(contract(), provenance=provenance(T-timedelta(days=1), "live_verified"))
        fixed = signal(metadata=(metadata,), quotes=(quote(0, kind="live_verified"),))
        clean = tuple(quote(s, "100", "101", kind="live_verified") for s in range(0, 70, 5))
        for contamination in ("synthetic", "provider_change"):
            changed = tuple(replace(q, provenance=replace(q.provenance,
                evidence_kind="synthetic" if contamination == "synthetic" else "live_verified",
                source="another-provider" if contamination == "provider_change" else q.provenance.source))
                if q.source_at == T+timedelta(seconds=30) else q for q in clean)
            result = replay_signal(fixed, changed, policy=policy())
            self.assertEqual(result.status, "incomplete")
            self.assertIsNone(result.net_pnl)
            self.assertEqual(result.evidence_kind, "synthetic" if contamination == "synthetic" else "mixed_source")
            self.assertEqual(result.source_continuity, contamination != "provider_change")
            self.assertEqual(result.entries[0].source_at, T+timedelta(seconds=5))
            self.assertEqual(result.exits[0].source_at, T+timedelta(seconds=65))

    def test_uniform_synthetic_future_cannot_switch_a_verified_origin_even_without_origin_event(self):
        metadata = replace(contract(), provenance=provenance(T-timedelta(days=1), "live_verified"))
        fixed = signal(metadata=(metadata,), quotes=(quote(0, kind="live_verified"),))
        future = tuple(quote(s, "100", "101", kind="synthetic") for s in range(5, 70, 5))
        for rows in (future, (quote(0, kind="live_verified"), *future)):
            result = replay_signal(fixed, rows, policy=policy())
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(result.evidence_kind, "synthetic")
            self.assertIsNone(result.net_pnl)
            self.assertTrue(any("changed from the frozen origin" in reason for reason in result.reasons))

    def test_quote_side_time_exit_accounts_for_fees_slippage_and_latency(self):
        outcome = replay_signal(signal(), events(), policy=policy())
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.entry_at, T + timedelta(seconds=5))
        self.assertEqual(outcome.exit_trigger_at, T + timedelta(seconds=60))
        self.assertEqual(outcome.exit_at, T + timedelta(seconds=65))
        self.assertEqual(outcome.entry_net_per_unit, D("101.55"))
        self.assertEqual(outcome.exit_net_per_unit, D("106.45"))
        self.assertEqual(outcome.net_pnl, D("46.00"))
        self.assertEqual(outcome.label, "synthetic_option_quote_replay")
        self.assertEqual(outcome.actual_fills_created, 0)
        self.assertIsNone(outcome.confidence)

    def test_replay_is_deterministic_and_does_not_mutate_signal(self):
        fixed = signal()
        before = fixed.to_dict()
        forward = replay_signal(fixed, events(), policy=policy())
        reverse = replay_signal(fixed, reversed(events()), policy=policy())
        self.assertEqual(forward, reverse)
        self.assertEqual(fixed.to_dict(), before)
        with self.assertRaises(FrozenInstanceError):
            forward.net_pnl = D("999999")

    def test_future_data_changes_leave_past_signal_unchanged(self):
        original = signal(quotes=(quote(0), *(q for q in events() if q.received_at > T)))
        changed_future = tuple(replace(q, bid=q.bid * 3, ask=q.ask * 3) for q in events() if q.received_at > T)
        mutated = signal(quotes=(quote(0), *changed_future))
        self.assertEqual(original, mutated)
        self.assertEqual(original.input_fingerprint, mutated.input_fingerprint)

    def test_missing_costs_quantity_and_non_lot_quantity_do_not_score(self):
        for settings in (ReplayPolicy(), policy(fees_total=None), policy(slippage_per_leg=None), policy(quantity=11)):
            with self.subTest(settings=settings):
                outcome = replay_signal(signal(), events(), policy=settings)
                self.assertEqual(outcome.status, "unavailable")
                self.assertIsNone(outcome.net_pnl)
                self.assertEqual(outcome.entries, ())

    def test_stop_triggers_before_delayed_exit_and_does_not_cap_loss(self):
        rows = tuple(quote(s, "100" if s < 10 else "80" if s < 15 else "70",
                           "101" if s < 10 else "81" if s < 15 else "71") for s in range(66))
        outcome = replay_signal(signal(), rows, policy=policy(stop_loss_per_unit=D("2")))
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.exit_reason, "stop")
        self.assertEqual(outcome.exit_trigger_at, T + timedelta(seconds=10))
        self.assertEqual(outcome.exit_at, T + timedelta(seconds=15))
        self.assertLess(outcome.net_pnl, -D("2") * 10)

    def test_target_is_also_subject_to_manual_exit_latency(self):
        rows = tuple(quote(s, "100" if s < 10 else "110" if s < 15 else "109",
                           "101" if s < 10 else "111" if s < 15 else "110") for s in range(66))
        outcome = replay_signal(signal(), rows, policy=policy(target_profit_per_unit=D("5")))
        self.assertEqual(outcome.exit_reason, "target")
        self.assertEqual(outcome.exit_at, T + timedelta(seconds=15))
        self.assertEqual(outcome.exits[0].side, "SELL")

    def test_gap_suppresses_scored_pnl_instead_of_assuming_no_stop(self):
        rows = tuple(q for q in events() if not 20 <= (q.received_at - T).total_seconds() <= 29)
        outcome = replay_signal(signal(), rows, policy=policy())
        self.assertEqual(outcome.status, "incomplete")
        self.assertTrue(outcome.gaps)
        self.assertTrue(outcome.exits)
        self.assertIsNone(outcome.net_pnl)

    def test_missing_exit_is_unavailable_not_zero_return(self):
        outcome = replay_signal(signal(), events(61), policy=policy())
        self.assertEqual(outcome.status, "incomplete")
        self.assertTrue(outcome.entries)
        self.assertFalse(outcome.exits)
        self.assertIsNone(outcome.net_pnl)
        summary = summarize_replays((outcome,))
        self.assertEqual(summary["unavailable"], 1)
        self.assertIsNone(summary["net_pnl"])

    def test_multi_leg_missing_hedge_quote_blocks_entry_or_scoring(self):
        fixed = signal(spread=True)
        self.assertEqual(fixed.status, "candidate")
        missing = replay_signal(fixed, events(), policy=policy())
        self.assertEqual(missing.status, "unavailable")
        full = tuple(q for s in range(66) for q in (
            quote(s, "99", "100"), quote(s, "59", "60", identifier="CALL50100")))
        complete = replay_signal(fixed, full, policy=policy(slippage_per_leg=D("0")))
        self.assertEqual(complete.status, "completed")
        self.assertEqual(complete.entry_net_per_unit, D("41"))
        self.assertEqual(complete.exit_net_per_unit, D("39"))
        self.assertEqual(complete.net_pnl, D("-23"))
        gap = tuple(q for q in full if not (q.contract_id == "CALL50100" and 20 <= (q.received_at-T).total_seconds() <= 30))
        partial = replay_signal(fixed, gap, policy=policy())
        self.assertEqual(partial.status, "incomplete")
        self.assertIsNone(partial.net_pnl)

    def test_unknown_source_and_future_source_never_become_live_quotes(self):
        for transform in (lambda q: replace(q, source_at=None),
                          lambda q: replace(q, source_at=q.received_at + timedelta(seconds=1))):
            outcome = replay_signal(signal(), map(transform, events()), policy=policy(),
                                    quality_policy=QualityPolicy(max_future_skew_seconds=999))
            self.assertEqual(outcome.status, "unavailable")
            self.assertFalse(outcome.entries)

    def test_duplicate_old_quotes_cannot_refresh_or_fill_after_latency(self):
        rows = tuple(replace(quote(s), source_at=T, sequence=0) for s in range(66))
        outcome = replay_signal(signal(), rows, policy=policy())
        self.assertEqual(outcome.status, "unavailable")

    def test_invalid_or_insufficient_depth_cannot_model_execution(self):
        for changes in ({"ask_size": None}, {"bid_size": 0}, {"ask_size": 5}, {"bid": D("999")}):
            with self.subTest(changes=changes):
                rows = tuple(replace(q, **changes) for q in events())
                outcome = replay_signal(signal(), rows, policy=policy())
                self.assertEqual(outcome.status, "unavailable")

    def test_history_candles_or_candle_tagged_quotes_cannot_create_fills(self):
        bar = OhlcBar(T, T + timedelta(minutes=1), D("100"), D("110"), D("90"), D("101"))
        for rows in ((bar,), tuple(replace(q, provenance=provenance(q.received_at, "historical_candle")) for q in events())):
            outcome = replay_signal(signal(), rows, policy=policy())
            self.assertEqual(outcome.status, "unavailable")
            self.assertEqual(outcome.entries, ())

    def test_metadata_expiry_and_horizon_change_are_not_ignored(self):
        outcome = replay_signal(signal(), events(), policy=policy(horizon_seconds=120))
        self.assertEqual(outcome.status, "unavailable")
        old = contract()
        expired_version = replace(old, effective_until=T + timedelta(seconds=20))
        fixed = signal(metadata=(expired_version,))
        outcome = replay_signal(fixed, events(), policy=policy())
        self.assertEqual(outcome.status, "incomplete")
        self.assertIsNone(outcome.net_pnl)

    def test_cancellation_is_bounded_and_never_creates_actual_fills(self):
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            return calls >= 4
        outcome = replay_signal(signal(), events(), policy=policy(), cancelled=cancel)
        self.assertEqual(outcome.status, "cancelled")
        self.assertEqual(calls, 4)
        self.assertEqual(outcome.actual_fills_created, 0)

    def test_slippage_rounds_adversely_to_tick(self):
        outcome = replay_signal(signal(), events(), policy=policy(slippage_per_leg=D("0.03")))
        self.assertEqual(outcome.entries[0].price, D("101.55"))
        self.assertEqual(outcome.exits[0].price, D("106.45"))

    def test_delayed_entry_rechecks_original_spread_filter(self):
        outcome = replay_signal(signal(), (quote(s, "10", "100") for s in range(66)), policy=policy())
        self.assertEqual(outcome.status, "unavailable")
        self.assertFalse(outcome.entries)

    def test_signal_stop_policy_is_preserved_without_replay_override(self):
        fixed = generate_signal((contract(),), (quote(0),), as_of=T, underlying_price=D("50000"),
                                direction="BULLISH", strategy="LONG_OPTION",
                                policy=ResearchPolicy(horizon_minutes=1, stop_loss_fraction=D("0.1")))
        rows = tuple(quote(s, "100" if s < 10 else "80", "101" if s < 10 else "81") for s in range(66))
        outcome = replay_signal(fixed, rows, policy=policy())
        self.assertEqual(outcome.exit_reason, "stop")
        self.assertEqual(outcome.policy, policy())
        self.assertEqual(outcome.quality_policy.max_future_skew_seconds, 0)

    def test_conflicting_same_time_quotes_are_not_accepted_by_sort_order(self):
        rows = events()
        conflict = replace(rows[20], bid=D("90"), ask=D("91"))
        first = replay_signal(signal(), (*rows, conflict), policy=policy())
        second = replay_signal(signal(), (conflict, *rows), policy=policy())
        self.assertEqual(first, second)
        self.assertEqual(first.status, "incomplete")
        self.assertIsNone(first.net_pnl)

    def test_future_after_resolved_exit_does_not_change_earlier_outcome(self):
        outcome = replay_signal(signal(), events(), policy=policy())
        changed_later = events() + (quote(1000, "1", "2"),)
        self.assertEqual(outcome, replay_signal(signal(), changed_later, policy=policy()))

    def test_synthetic_catalogue_cannot_be_relabelled_by_replay_quote_prices(self):
        fixed = signal(quotes=(quote(0, kind="replay_quote"),))
        rows = tuple(replace(q, provenance=provenance(q.received_at, "replay_quote")) for q in events())
        outcome = replay_signal(fixed, rows, policy=policy())
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.label, "synthetic_option_quote_replay")

    def test_thin_exit_depth_cannot_hide_an_observed_stop(self):
        rows = tuple(quote(s, "80" if 10 <= s < 15 else "100" if s < 20 else "120",
                           "81" if 10 <= s < 15 else "101" if s < 20 else "121",
                           bid_size=1 if 10 <= s < 15 else 100) for s in range(66))
        settings = policy(stop_loss_per_unit=D("2"))
        thin = replay_signal(signal(), rows, policy=settings)
        liquid = replay_signal(signal(), (replace(q, bid_size=100) for q in rows), policy=settings)
        self.assertEqual(thin.status, "completed")
        self.assertEqual(thin.exit_reason, "stop")
        self.assertEqual(thin.exit_trigger_at, T + timedelta(seconds=10))
        self.assertEqual(thin.exit_at, T + timedelta(seconds=15))
        self.assertEqual(thin.net_pnl, D("-14"))
        self.assertEqual(thin.net_pnl, liquid.net_pnl)
        self.assertEqual(thin.exit_trigger_at, liquid.exit_trigger_at)

    def test_unexecutable_stop_remains_pending_until_bounded_exit_deadline(self):
        rows = tuple(quote(s, "80" if s >= 10 else "100", "81" if s >= 10 else "101",
                           bid_size=1 if s >= 10 else 100) for s in range(100))
        outcome = replay_signal(signal(), rows, policy=policy(stop_loss_per_unit=D("2")))
        self.assertEqual(outcome.status, "incomplete")
        self.assertEqual(outcome.exit_reason, "stop")
        self.assertEqual(outcome.exit_trigger_at, T + timedelta(seconds=10))
        self.assertIsNone(outcome.net_pnl)
        self.assertFalse(outcome.exits)


class CandleDiagnosticTests(unittest.TestCase):
    def test_same_bar_stop_target_ambiguity_uses_stop_without_option_fill(self):
        bar = OhlcBar(T + timedelta(seconds=5), T + timedelta(minutes=1),
                      D("100"), D("120"), D("80"), D("105"))
        result = candle_path_diagnostic((bar,), signal_at=T, entry_level=D("100"),
                                       stop_level=D("90"), target_level=D("110"), horizon_seconds=60)
        self.assertEqual(result.status, "stop")
        self.assertEqual(result.exit_level, D("90"))
        self.assertTrue(result.ambiguous)
        self.assertEqual(result.option_fills, ())
        self.assertIsNone(result.option_pnl)
        self.assertEqual(result.label, "approximate_underlying_only")

    def test_close_signal_cannot_fill_earlier_in_same_bar_or_latency_window(self):
        previous = OhlcBar(T - timedelta(seconds=60), T, D("100"), D("120"), D("80"), D("100"))
        overlaps = replace(previous, start=T, end=T + timedelta(seconds=60))
        for bar in (previous, overlaps):
            result = candle_path_diagnostic((bar,), signal_at=T, entry_level=D("100"),
                                           stop_level=D("90"), target_level=D("110"), horizon_seconds=60)
            self.assertEqual(result.status, "unavailable")

    def test_gap_through_stop_uses_worse_price_for_long_and_short(self):
        for side, opening, high, low, stop, target in (
            ("LONG", "80", "85", "75", "90", "110"),
            ("SHORT", "120", "125", "115", "110", "90"),
        ):
            bar = OhlcBar(T + timedelta(seconds=5), T + timedelta(seconds=60),
                          D(opening), D(high), D(low), D(opening))
            result = candle_path_diagnostic((bar,), signal_at=T, entry_level=D("100"),
                                           stop_level=D(stop), target_level=D(target), side=side, horizon_seconds=60)
            self.assertEqual(result.exit_level, D(opening))

    def test_candle_path_gap_is_unavailable(self):
        bar = OhlcBar(T + timedelta(seconds=30), T + timedelta(seconds=60),
                      D("100"), D("120"), D("80"), D("100"))
        outcome = candle_path_diagnostic((bar,), signal_at=T, entry_level=D("100"),
                                         stop_level=D("90"), target_level=D("110"), horizon_seconds=60)
        self.assertEqual(outcome.status, "unavailable")
        self.assertIsNone(outcome.option_pnl)


class ChronologicalEvaluationTests(unittest.TestCase):
    def test_holdout_dates_never_train_and_embargo_covers_full_label(self):
        times = tuple(T + timedelta(days=d, minutes=m) for d in range(8) for m in (0, 1, 2))
        folds = chronological_folds(times, horizon_seconds=60, min_train_sessions=3, holdout_sessions=2)
        self.assertTrue(folds)
        held_out = set(folds[-1].test_dates)
        self.assertTrue(folds[-1].holdout)
        for fold in folds:
            self.assertFalse(set(fold.train_dates) & set(fold.test_dates))
            self.assertFalse(held_out & set(fold.train_dates))
            self.assertGreaterEqual(fold.embargo_seconds, 60)
            self.assertTrue(all(t + timedelta(seconds=60) < min(fold.test_origins) for t in fold.train_origins))

    def test_cross_midnight_label_overlap_is_purged(self):
        times = tuple(datetime(2026, 1, day, 23, 59, tzinfo=timezone.utc) for day in (1, 2, 3)) + (
            datetime(2026, 1, 4, 0, 0, tzinfo=timezone.utc), datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc))
        folds = chronological_folds(times, horizon_seconds=120)
        self.assertNotIn(times[2], folds[0].train_origins)

    def test_no_splits_with_too_few_sessions_and_short_embargo_rejected(self):
        self.assertEqual(chronological_folds((T,), horizon_seconds=60), ())
        with self.assertRaises(ValueError):
            chronological_folds((T,), horizon_seconds=60, embargo_seconds=59)

    def test_execution_delayed_label_cannot_leak_across_midnight(self):
        origin = datetime(2026, 1, 3, 23, 58, 55, tzinfo=timezone.utc)
        times = (datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
                 datetime(2026, 1, 2, 12, tzinfo=timezone.utc), origin,
                 datetime(2026, 1, 4, 0, tzinfo=timezone.utc),
                 datetime(2026, 1, 5, 0, tzinfo=timezone.utc))
        folds = chronological_folds(times, horizon_seconds=60)
        self.assertEqual(folds[0].embargo_seconds, 95)
        self.assertNotIn(origin, folds[0].train_origins)
        with self.assertRaises(ValueError):
            chronological_folds(times, horizon_seconds=60, embargo_seconds=60)

    def test_actual_label_end_overrides_a_shorter_nominal_window(self):
        times = tuple(T + timedelta(days=day) for day in range(5))
        actual = {times[2]: times[3] + timedelta(seconds=1)}
        folds = chronological_folds(times, horizon_seconds=60, label_end_times=actual)
        self.assertNotIn(times[2], folds[0].train_origins)


if __name__ == "__main__":
    unittest.main()

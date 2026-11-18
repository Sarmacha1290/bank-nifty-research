"""Pure synthetic fixtures for timestamp and option contract quality boundaries."""

import ast
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import unittest

from banknifty.options_data import (
    ContractMetadata, OptionQuote, QualityPolicy, QuoteBook, RawProvenance,
    canonical_fingerprint, select_contract_version, validate_contract,
    validate_legs, validate_quote,
)


NOW = datetime(2026, 10, 5, 4, 15, tzinfo=timezone.utc)
D = Decimal


def raw(*, evidence="synthetic", at=NOW, payload=b'{"fixture":true}', source="offline-fixture"):
    return RawProvenance(source, payload, at, evidence)


def contract(**changes):
    base = ContractMetadata(
        "FIXTURE-CALL-55000", "BANKNIFTY", "NSE", "CALL", D("55000"),
        NOW + timedelta(days=8), 30, D("0.05"), NOW - timedelta(days=30),
        None, NOW - timedelta(days=20), True, raw(at=NOW - timedelta(days=20)),
    )
    return replace(base, **changes)


def quote(**changes):
    base = OptionQuote("FIXTURE-CALL-55000", NOW, NOW, D("100"), D("100.05"), raw(),
                       bid_size=60, ask_size=90, last=D("100"), open_interest=0,
                       oi_source_at=NOW, sequence=1)
    return replace(base, **changes)


class OptionsDataTests(unittest.TestCase):
    def test_observed_zero_oi_is_distinct_from_missing_and_quote_time_does_not_supply_oi_time(self):
        observed = validate_quote(quote(), contract(), NOW)
        missing = validate_quote(quote(open_interest=None, oi_source_at=None), contract(), NOW)
        unknown = validate_quote(quote(oi_source_at=None), contract(), NOW)
        self.assertEqual((observed.oi_freshness, missing.oi_freshness, unknown.oi_freshness),
                         ("fresh", "missing", "unknown"))
        self.assertTrue(observed.accepted)
        self.assertIn("oi_missing", missing.warnings)
        self.assertIn("oi_unknown", unknown.warnings)
        for row in (quote(open_interest=None, oi_source_at=None), quote(oi_source_at=None)):
            self.assertFalse(validate_quote(row, contract(), NOW, QualityPolicy(require_fresh_oi=True)).accepted)

    def test_receipt_time_never_substitutes_for_source_time(self):
        result = validate_quote(quote(source_at=None, provenance=raw(evidence="live_verified")), contract(), NOW)
        self.assertFalse(result.accepted)
        self.assertFalse(result.verified_live)
        self.assertIsNone(result.source_age_seconds)
        self.assertIn("unknown_source_freshness", result.reasons)

    def test_quote_and_receipt_staleness_are_independent(self):
        for row, code in ((quote(source_at=NOW - timedelta(seconds=6)), "stale_source"),
                          (quote(source_at=NOW - timedelta(seconds=6), received_at=NOW - timedelta(seconds=6)), "stale_receipt")):
            with self.subTest(code=code):
                self.assertIn(code, validate_quote(row, contract(), NOW).reasons)
        result = validate_quote(quote(source_at=NOW - timedelta(seconds=5)), contract(), NOW)
        self.assertTrue(result.accepted)
        self.assertEqual(result.source_age_seconds, 5)

    def test_timestamp_boundaries_and_naive_times_fail_closed(self):
        rows = ((quote(source_at=NOW + timedelta(seconds=2)), "source_timestamp_in_future"),
                (quote(received_at=NOW + timedelta(microseconds=1)), "receipt_after_cutoff"),
                (quote(source_at=NOW.replace(tzinfo=None)), "invalid_source_timestamp"),
                (quote(received_at=NOW.replace(tzinfo=None)), "invalid_receipt_timestamp"),
                (quote(provenance=raw(at=NOW + timedelta(seconds=1))), "quote_provenance_after_cutoff"))
        for row, code in rows:
            with self.subTest(code=code):
                self.assertIn(code, validate_quote(row, contract(), NOW).reasons)
        with self.assertRaises(ValueError):
            validate_quote(quote(), contract(), NOW.replace(tzinfo=None))
        zero_skew = QualityPolicy(max_future_skew_seconds=0)
        self.assertFalse(validate_quote(quote(source_at=NOW + timedelta(microseconds=1)), contract(), NOW, zero_skew).accepted)

    def test_source_after_receipt_cannot_be_hidden_by_decision_time(self):
        result = validate_quote(quote(source_at=NOW, received_at=NOW - timedelta(seconds=2)), contract(), NOW)
        self.assertIn("source_after_receipt", result.reasons)

    def test_invalid_crossed_missing_and_off_tick_prices(self):
        rows = ((quote(bid=None), "missing_bid"), (quote(ask=None), "missing_ask"),
                (quote(bid=D("0")), "invalid_bid"), (quote(ask=D("-1")), "invalid_ask"),
                (quote(bid=D("NaN")), "invalid_bid"), (quote(ask=D("Infinity")), "invalid_ask"),
                (quote(bid=100.0), "invalid_bid"), (quote(ask=D("100.02")), "ask_off_tick"),
                (quote(bid=D("100.10")), "crossed_book"), (quote(last=D("-1")), "invalid_last"),
                (quote(ask=D("1e1000000")), "ask_off_tick"))
        for row, code in rows:
            with self.subTest(code=code):
                self.assertIn(code, validate_quote(row, contract(), NOW).reasons)
        self.assertTrue(validate_quote(quote(bid=D("100.05")), contract(), NOW).accepted)

    def test_size_sequence_and_oi_types_reject_booleans_and_negative_values(self):
        for field, value, code in (("bid_size", -1, "invalid_bid_size"), ("ask_size", True, "invalid_ask_size"),
                                   ("sequence", -1, "invalid_sequence"), ("open_interest", False, "invalid_open_interest")):
            with self.subTest(field=field):
                self.assertIn(code, validate_quote(quote(**{field: value}), contract(), NOW).reasons)
        self.assertTrue(validate_quote(quote(bid_size=None), contract(), NOW).accepted)
        self.assertFalse(validate_quote(quote(bid_size=0), contract(), NOW, QualityPolicy(require_sizes=True)).accepted)

    def test_oi_timestamp_is_independently_stale_or_unknown(self):
        stale = validate_quote(quote(oi_source_at=NOW - timedelta(seconds=31)), contract(), NOW)
        self.assertEqual(stale.oi_freshness, "stale")
        self.assertTrue(stale.accepted)
        self.assertIn("oi_stale", stale.warnings)
        self.assertFalse(validate_quote(quote(oi_source_at=NOW - timedelta(seconds=31)), contract(), NOW,
                                        QualityPolicy(require_fresh_oi=True)).accepted)
        self.assertIn("oi_timestamp_in_future", validate_quote(quote(oi_source_at=NOW + timedelta(seconds=2)), contract(), NOW).reasons)
        self.assertIn("oi_timestamp_without_value", validate_quote(quote(open_interest=None), contract(), NOW).reasons)
        self.assertIn("invalid_oi_timestamp", validate_quote(quote(oi_source_at=NOW.replace(tzinfo=None)), contract(), NOW).reasons)

    def test_future_oi_never_uses_clock_skew_tolerance_as_prior_knowledge(self):
        future = quote(oi_source_at=NOW + timedelta(microseconds=1))
        for policy in (QualityPolicy(), QualityPolicy(require_fresh_oi=True), QualityPolicy(max_future_skew_seconds=60)):
            result = validate_quote(future, contract(), NOW, policy)
            self.assertFalse(result.accepted)
            self.assertEqual(result.oi_freshness, "future")
            self.assertIn("oi_timestamp_in_future", result.reasons)
        older_receipt = quote(received_at=NOW-timedelta(milliseconds=500), oi_source_at=NOW)
        self.assertIn("oi_after_receipt", validate_quote(older_receipt, contract(), NOW).reasons)

    def test_strategy_minimum_oi_gate_rejects_post_cutoff_oi(self):
        from tests.test_options_strategy import pair, NOW as origin
        from banknifty.options_strategy import ResearchPolicy, generate_signal
        contracts, observations = pair()
        future = replace(observations[0], oi_source_at=origin + timedelta(milliseconds=500))
        for quality in (QualityPolicy(require_fresh_oi=True), QualityPolicy(max_future_skew_seconds=10)):
            result = generate_signal(contracts, (future, observations[1]), as_of=origin,
                underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD",
                policy=ResearchPolicy(minimum_open_interest=0), quality_policy=quality)
            self.assertEqual(result.status, "no_trade")

    def test_effective_dated_metadata_not_hardcoded_current_lots_or_ticks(self):
        boundary = NOW + timedelta(days=1)
        old = contract(effective_until=boundary)
        new = contract(effective_from=boundary, lot_size=35, tick_size=D("0.10"),
                       known_at=NOW - timedelta(days=1))
        self.assertEqual(select_contract_version([old, new], old.contract_id, NOW).lot_size, 30)
        self.assertEqual(select_contract_version([old, new], old.contract_id, boundary).lot_size, 35)
        self.assertIn("ask_off_tick", validate_quote(quote(source_at=boundary, received_at=boundary,
                                                           provenance=raw(at=boundary)), new, boundary).reasons)
        expired_version = validate_contract(old, boundary)
        self.assertIn("metadata_no_longer_effective", expired_version)
        self.assertIsNone(select_contract_version([old, replace(old, lot_size=99)], old.contract_id, NOW))

    def test_expiry_and_metadata_knowledge_use_the_cutoff(self):
        rows = ((contract(expiry_at=NOW), "contract_expired"),
                (contract(known_at=NOW + timedelta(seconds=1)), "metadata_not_known_at_cutoff"),
                (contract(effective_from=NOW + timedelta(seconds=1)), "metadata_not_yet_effective"),
                (contract(provenance=raw(at=NOW + timedelta(seconds=1))), "contract_provenance_after_cutoff"),
                (contract(listed=False), "contract_not_listed"), (contract(lot_size=0), "invalid_lot_size"),
                (contract(tick_size=D("0")), "invalid_tick_size"), (contract(option_type="CE"), "invalid_option_type"))
        for row, code in rows:
            with self.subTest(code=code):
                self.assertIn(code, validate_contract(row, NOW))

    def test_future_metadata_changes_do_not_rewrite_prior_contract_resolution(self):
        old = contract()
        future = replace(old, lot_size=500, known_at=NOW + timedelta(days=1), effective_from=NOW + timedelta(days=1))
        before = select_contract_version([old, future], old.contract_id, NOW).fingerprint
        after = select_contract_version([old, replace(future, tick_size=D("999"))], old.contract_id, NOW).fingerprint
        self.assertEqual(before, after)

    def test_new_metadata_cannot_reinterpret_earlier_quote_or_oi_units(self):
        changed = contract(effective_from=NOW, lot_size=35, tick_size=D("0.10"))
        current = quote(bid=D("100"), ask=D("100.10"))
        self.assertTrue(validate_quote(current, changed, NOW).accepted)
        for field in ("source_at", "received_at", "oi_source_at"):
            older = replace(current, **{field: NOW - timedelta(milliseconds=500)})
            with self.subTest(field=field):
                self.assertIn(f"{field}_outside_metadata_interval", validate_quote(older, changed, NOW).reasons)

    def test_metadata_interval_end_excludes_observation_even_with_clock_skew_tolerance(self):
        end = NOW + timedelta(milliseconds=500)
        old = contract(effective_until=end)
        result = validate_quote(quote(source_at=end), old, NOW)
        self.assertIn("source_at_outside_metadata_interval", result.reasons)

    def test_malformed_leg_metadata_is_rejected_without_mutating_book(self):
        self.assertIn("invalid_leg_identity", validate_legs([quote(contract_id=[])], [contract()], NOW).reasons)
        malformed = contract(lot_size=[])
        self.assertFalse(validate_legs([quote()], [malformed], NOW).accepted)
        self.assertEqual(QuoteBook().ingest(quote(contract_id=[]), contract(), NOW).status, "rejected")
        self.assertIn("candle_is_not_contract_catalogue",
                      validate_contract(contract(provenance=raw(evidence="historical_candle")), NOW))

    def test_no_evidence_type_is_promoted(self):
        for kind in ("synthetic", "replay_quote", "live_unverified"):
            with self.subTest(kind=kind):
                result = validate_quote(quote(provenance=raw(evidence=kind)), contract(), NOW)
                self.assertTrue(result.accepted)
                self.assertFalse(result.verified_live)
                self.assertFalse(validate_quote(quote(provenance=raw(evidence=kind)), contract(), NOW,
                                                QualityPolicy(require_live_verified=True)).accepted)
        candle = validate_quote(quote(provenance=raw(evidence="historical_candle")), contract(), NOW)
        self.assertIn("historical_candle_is_not_quote", candle.reasons)
        self.assertFalse(candle.accepted)
        purported = quote(provenance=raw(evidence="live_verified"))
        self.assertFalse(validate_quote(purported, contract(), NOW).verified_live)
        reviewed_contract = contract(provenance=raw(evidence="live_verified", at=NOW - timedelta(days=1)))
        self.assertTrue(validate_quote(purported, reviewed_contract, NOW, QualityPolicy(require_live_verified=True)).verified_live)

    def test_duplicate_never_refreshes_source_or_receipt_but_keeps_raw_evidence(self):
        book = QuoteBook()
        original = quote()
        repeated = replace(original, received_at=NOW + timedelta(seconds=1),
                           provenance=raw(at=NOW + timedelta(seconds=1), payload=b'{"repeat":true}'))
        self.assertEqual(book.ingest(original, contract(), NOW).status, "accepted")
        self.assertEqual(book.ingest(repeated, contract(), NOW + timedelta(seconds=1)).status, "duplicate")
        self.assertIs(book.latest(original.contract_id), original)
        self.assertEqual(book.events[1].quote.provenance.payload, b'{"repeat":true}')
        self.assertFalse(validate_quote(book.latest(original.contract_id), contract(), NOW + timedelta(seconds=6)).accepted)

    def test_out_of_order_does_not_replace_accepted_state(self):
        for candidate in (quote(source_at=NOW - timedelta(seconds=1), sequence=2),
                          quote(received_at=NOW - timedelta(milliseconds=500), oi_source_at=NOW - timedelta(milliseconds=500), sequence=2),
                          quote(source_at=NOW + timedelta(milliseconds=500), sequence=0)):
            with self.subTest(candidate=candidate):
                book = QuoteBook()
                original = quote()
                book.ingest(original, contract(), NOW)
                self.assertEqual(book.ingest(candidate, contract(), NOW).status, "out_of_order")
                self.assertIs(book.latest(original.contract_id), original)

    def test_conflicting_repeated_sequence_and_timestamp_fail_closed(self):
        for first, later, code in ((quote(), quote(bid=D("99.95")), "conflicting_sequence"),
                                   (quote(sequence=None), quote(sequence=None, bid=D("99.95")), "conflicting_source_timestamp"),
                                   (quote(), quote(sequence=None), "sequence_disappeared")):
            with self.subTest(code=code):
                book = QuoteBook()
                book.ingest(first, contract(), NOW)
                result = book.ingest(later, contract(), NOW)
                self.assertEqual(result.status, "rejected")
                self.assertIn(code, result.quality.reasons)
                self.assertIs(book.latest(first.contract_id), first)

    def test_new_sequence_at_same_source_millisecond_can_advance(self):
        book = QuoteBook()
        book.ingest(quote(), contract(), NOW)
        newer = quote(sequence=2, bid=D("99.95"))
        self.assertEqual(book.ingest(newer, contract(), NOW).status, "accepted")
        self.assertIs(book.latest(newer.contract_id), newer)

    def test_rejected_raw_data_is_retained_without_replacing_quote(self):
        book = QuoteBook()
        book.ingest(quote(), contract(), NOW)
        crossed = quote(bid=D("200"), provenance=raw(payload=b'{"bad":"crossed"}'))
        result = book.ingest(crossed, contract(), NOW)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(book.events[-1].quote.provenance.payload, b'{"bad":"crossed"}')
        self.assertEqual(book.latest(crossed.contract_id).bid, D("100"))

    def test_source_or_evidence_switch_requires_explicit_new_book(self):
        for changed, code in ((raw(source="another"), "source_changed_requires_new_book"),
                              (raw(evidence="replay_quote"), "evidence_changed_requires_new_book")):
            with self.subTest(code=code):
                book = QuoteBook()
                book.ingest(quote(), contract(), NOW)
                self.assertIn(code, book.ingest(quote(provenance=changed), contract(), NOW).quality.reasons)

    def test_multileg_synchronization_and_missing_hedge(self):
        hedge = contract(contract_id="FIXTURE-CALL-55100", strike=D("55100"))
        hedge_quote = quote(contract_id=hedge.contract_id)
        self.assertTrue(validate_legs([quote(), hedge_quote], [contract(), hedge], NOW).accepted)
        late = replace(hedge_quote, source_at=NOW - timedelta(seconds=2))
        self.assertIn("unsynchronized_leg_source_at", validate_legs([quote(), late], [contract(), hedge], NOW).reasons)
        late_receipt = replace(hedge_quote, source_at=NOW - timedelta(seconds=2), received_at=NOW - timedelta(seconds=2))
        self.assertIn("unsynchronized_leg_received_at", validate_legs([quote(), late_receipt], [contract(), hedge], NOW).reasons)
        self.assertFalse(validate_legs([quote()], [contract(), hedge], NOW).accepted)
        self.assertFalse(validate_legs([quote(), quote()], [contract()], NOW).accepted)
        self.assertFalse(validate_legs([], [], NOW).accepted)

    def test_spread_legs_must_share_expiry_lot_exchange_underlying_and_source(self):
        hedge = contract(contract_id="FIXTURE-CALL-55100", strike=D("55100"))
        hedge_quote = quote(contract_id=hedge.contract_id)
        for field, value in (("expiry_at", NOW + timedelta(days=9)), ("lot_size", 15),
                             ("exchange", "OTHER"), ("underlying", "OTHER")):
            with self.subTest(field=field):
                result = validate_legs([quote(), hedge_quote], [contract(), replace(hedge, **{field:value})], NOW)
                self.assertIn(f"mismatched_leg_{field}", result.reasons)
        for provenance, code in ((raw(source="other"), "mixed_leg_sources"),
                                 (raw(evidence="replay_quote"), "mixed_leg_evidence")):
            result = validate_legs([quote(), replace(hedge_quote, provenance=provenance)], [contract(), hedge], NOW)
            self.assertIn(code, result.reasons)

    def test_frozen_input_and_raw_payload_fingerprints(self):
        original = quote()
        fingerprint = original.fingerprint
        with self.assertRaises(FrozenInstanceError):
            original.bid = D("1")
        with self.assertRaises(TypeError):
            RawProvenance("fixture", bytearray(b"mutable"), NOW, "synthetic")
        self.assertNotEqual(fingerprint, replace(original, ask=D("100.10")).fingerprint)
        self.assertNotEqual(fingerprint, replace(original, provenance=raw(payload=b"different")).fingerprint)
        self.assertEqual(fingerprint, original.fingerprint)
        self.assertEqual(canonical_fingerprint(D("1.00")), canonical_fingerprint(D("1")))
        self.assertNotEqual(canonical_fingerprint(D("123456789012345678901234567890")),
                            canonical_fingerprint(D("123456789012345678901234567891")))
        self.assertEqual(canonical_fingerprint(NOW), canonical_fingerprint(NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))))
        self.assertNotIn("unique raw payload", repr(raw(payload=b"unique raw payload")))

    def test_operational_policy_is_configurable_and_rejects_invalid_limits(self):
        stale = quote(source_at=NOW - timedelta(seconds=8))
        self.assertFalse(validate_quote(stale, contract(), NOW).accepted)
        self.assertTrue(validate_quote(stale, contract(), NOW, QualityPolicy(max_source_age_seconds=10)).accepted)
        for value in (-1, float("nan"), float("inf"), True, "5"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    QualityPolicy(max_source_age_seconds=value)
        with self.assertRaises(ValueError):
            QualityPolicy(require_fresh_oi="yes")

    def test_quality_module_has_no_broker_or_transport_dependencies(self):
        path = Path(__file__).resolve().parents[1] / "banknifty" / "options_data.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = {"dataclasses", "datetime", "decimal", "hashlib", "json", "math", "typing"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertTrue(all(alias.name.split(".")[0] in allowed for alias in node.names))
            elif isinstance(node, ast.ImportFrom):
                self.assertIn(node.module.split(".")[0], allowed)
        self.assertFalse(any(callable(getattr(QuoteBook, name, None))
                             for name in ("place_order", "modify_order", "cancel_order", "request", "connect")))


if __name__ == "__main__":
    unittest.main()

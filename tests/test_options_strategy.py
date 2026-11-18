"""Deterministic research/payoff tests; every quote and contract is synthetic."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

from banknifty.options_data import ContractMetadata, OptionQuote, QualityPolicy, RawProvenance
from banknifty.options_strategy import ResearchPolicy, SignalLeg, expiry_payoff, generate_signal, payoff_bounds

D = Decimal
NOW = datetime(2026, 10, 15, 5, 0, tzinfo=timezone.utc)


def catalogue(strike, kind="CALL", *, lot=25, contract_id=None, **changes):
    before = NOW - timedelta(days=1)
    record = ContractMetadata(
        contract_id or f"SYNTHETIC-{kind}-{strike}", "BANKNIFTY", "SYNTHETIC", kind,
        D(str(strike)), NOW + timedelta(days=7), lot, D("0.05"), before, None, before, True,
        RawProvenance("synthetic-catalogue", b"catalogue fixture", before, "synthetic"),
    )
    return replace(record, **changes)


def quote(contract, bid, ask, **changes):
    record = OptionQuote(contract.contract_id, NOW - timedelta(seconds=1), NOW, D(str(bid)), D(str(ask)),
                         RawProvenance("synthetic-book", b"book fixture", NOW, "synthetic"),
                         bid_size=1000, ask_size=1000, open_interest=0, oi_source_at=NOW - timedelta(seconds=1))
    return replace(record, **changes)


def pair(kind="CALL", credit=False):
    low, high = catalogue(100, kind), catalogue(200, kind)
    anchor, wing = (low, high) if kind == "CALL" else (high, low)
    qa = quote(anchor, 45, 46) if credit else quote(anchor, 59, 60)
    qw = quote(wing, 19, 20) if credit else quote(wing, 20, 21)
    return (anchor, wing), (qa, qw)


def candidate(kind="CALL", credit=False, **kwargs):
    contracts, quotes = pair(kind, credit)
    direction = ("BEARISH" if kind == "CALL" else "BULLISH") if credit else ("BULLISH" if kind == "CALL" else "BEARISH")
    return generate_signal(contracts, quotes, as_of=NOW, underlying_price=D("100" if kind == "CALL" else "200"),
                           direction=direction, strategy="CREDIT_SPREAD" if credit else "DEBIT_SPREAD", **kwargs)


class PayoffTests(unittest.TestCase):
    def test_debit_spread_oracles_for_calls_and_puts_and_boundaries(self):
        for kind in ("CALL", "PUT"):
            with self.subTest(kind=kind):
                signal = candidate(kind)
                self.assertEqual(signal.entry_net, D("40"))
                bounds = payoff_bounds(signal.legs, quantity=25, fees=D("7"))
                self.assertEqual(bounds.max_loss, D("1007"))
                self.assertEqual(bounds.max_profit, D("1493"))
                spots = (0, 99, 100, 150, 200, 201, 10000)
                expected = (-1007, -1007, -1007, 243, 1493, 1493, 1493) if kind == "CALL" else (1493, 1493, 1493, 243, -1007, -1007, -1007)
                for spot, pnl in zip(spots, expected):
                    self.assertEqual(expiry_payoff(signal.legs, D(spot), quantity=25, fees=D("7")), D(pnl))

    def test_credit_spread_oracles_for_calls_and_puts_and_boundaries(self):
        for kind in ("CALL", "PUT"):
            with self.subTest(kind=kind):
                signal = candidate(kind, credit=True)
                self.assertEqual(signal.entry_net, D("-25"))
                bounds = payoff_bounds(signal.legs, quantity=25, fees=D("7"))
                self.assertEqual(bounds.max_loss, D("1882"))
                self.assertEqual(bounds.max_profit, D("618"))
                spots = (0, 99, 100, 150, 200, 201, 10000)
                expected = (618, 618, 618, -632, -1882, -1882, -1882) if kind == "CALL" else (-1882, -1882, -1882, -632, 618, 618, 618)
                for spot, pnl in zip(spots, expected):
                    self.assertEqual(expiry_payoff(signal.legs, D(spot), quantity=25, fees=D("7")), D(pnl))

    def test_long_call_and_put_bounds(self):
        for kind in ("CALL", "PUT"):
            contract = catalogue(100, kind)
            q = quote(contract, 9, 10)
            legs = (SignalLeg(contract, "BUY", D("10"), q),)
            result = payoff_bounds(legs, quantity=25, fees=D("2"))
            self.assertEqual(result.max_loss, D("252"))
            self.assertEqual(result.max_profit, None if kind == "CALL" else D("2248"))

    def test_naked_and_mismatched_spreads_are_rejected(self):
        signal = candidate()
        first, second = signal.legs
        bad_cases = ((replace(first, side="SELL"),), (first, replace(second, side="BUY")),
                     (first, replace(second, contract=replace(second.contract, lot_size=50))),
                     (first, replace(second, contract=replace(second.contract, expiry_at=NOW + timedelta(days=8)))),
                     (first, replace(second, contract=replace(second.contract, option_type="PUT"))))
        for legs in bad_cases:
            with self.subTest(legs=legs), self.assertRaises(ValueError):
                payoff_bounds(legs)

    def test_invalid_units_prices_and_fees_fail(self):
        signal = candidate()
        for quantity in (0, -1, True, 1.5):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                payoff_bounds(signal.legs, quantity=quantity)
        with self.assertRaises(ValueError):
            expiry_payoff(signal.legs, D("NaN"))
        with self.assertRaises(ValueError):
            expiry_payoff(signal.legs, D("100"), fees=D("-1"))


class StrategyTests(unittest.TestCase):
    def test_all_six_supported_direction_strategy_combinations(self):
        for kind in ("CALL", "PUT"):
            for credit in (False, True):
                signal = candidate(kind, credit)
                self.assertEqual(signal.status, "candidate")
                self.assertEqual(signal.label, "synthetic")
                self.assertIsNone(signal.confidence)
                self.assertFalse(signal.calibrated)
                self.assertFalse(signal.trading_allowed)
                self.assertEqual(len(signal.legs), 2)
        for direction, kind in (("BULLISH", "CALL"), ("BEARISH", "PUT")):
            c = catalogue(100, kind)
            q = quote(c, 9, 10)
            signal = generate_signal([c], [q], as_of=NOW, underlying_price=D("100"), direction=direction, strategy="LONG_OPTION")
            self.assertEqual(signal.status, "candidate")
            self.assertEqual(signal.legs[0].contract.option_type, kind)
            self.assertEqual(signal.legs[0].price, q.ask)

    def test_neutral_and_unavailable_strike_structure_abstain(self):
        contracts, quotes = pair()
        neutral = generate_signal(contracts, quotes, as_of=NOW, underlying_price=D("100"), direction="NEUTRAL", strategy="DEBIT_SPREAD")
        self.assertEqual(neutral.status, "no_trade")
        missing = candidate(policy=ResearchPolicy(spread_width=D("75")))
        self.assertEqual(missing.status, "no_trade")
        self.assertEqual(missing.legs, ())

    def test_contract_selection_uses_listed_otm_earliest_expiry_and_exact_width(self):
        low, middle, high = catalogue(100), catalogue(200), catalogue(300)
        qs = [quote(low, 69, 70), quote(middle, 30, 31), quote(high, 10, 11)]
        signal = generate_signal([high, low, middle], qs, as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD", policy=ResearchPolicy(spread_width=D("200")))
        self.assertEqual([x.contract.strike for x in signal.legs], [D("100"), D("300")])
        self.assertEqual(signal.entry_net, D("60"))

    def test_unlisted_and_horizon_crossing_expiry_are_excluded(self):
        contracts, quotes = pair()
        for bad in (replace(contracts[0], listed=False), replace(contracts[0], expiry_at=NOW + timedelta(minutes=30))):
            result = generate_signal([bad, contracts[1]], quotes, as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
            self.assertEqual(result.status, "no_trade")

    def test_stale_missing_source_crossed_and_excess_spread_quotes_abstain(self):
        contracts, quotes = pair()
        for bad in (replace(quotes[0], source_at=NOW - timedelta(seconds=6)),
                    replace(quotes[0], received_at=NOW - timedelta(seconds=6), source_at=NOW - timedelta(seconds=6)),
                    replace(quotes[0], source_at=None), replace(quotes[0], bid=D("61")),
                    replace(quotes[0], bid=D("20"))):
            with self.subTest(bad=bad):
                result = generate_signal(contracts, [bad, quotes[1]], as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
                self.assertEqual(result.status, "no_trade")
                if bad.source_at is None:
                    self.assertEqual(result.label, "receipt_only")

    def test_synchronized_and_uniform_evidence_legs_are_required(self):
        contracts, quotes = pair()
        for bad in (replace(quotes[1], source_at=NOW - timedelta(seconds=3)),
                    replace(quotes[1], provenance=replace(quotes[1].provenance, evidence_kind="live_unverified"))):
            result = generate_signal(contracts, [quotes[0], bad], as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
            self.assertEqual(result.status, "no_trade")

    def test_missing_oi_is_distinct_from_zero_and_oi_time_is_separate(self):
        self.assertEqual(candidate(policy=ResearchPolicy(minimum_open_interest=0)).status, "candidate")
        contracts, quotes = pair()
        for bad in (replace(quotes[0], open_interest=None, oi_source_at=None),
                    replace(quotes[0], oi_source_at=None),
                    replace(quotes[0], oi_source_at=NOW - timedelta(seconds=31))):
            signal = generate_signal(contracts, [bad, quotes[1]], as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD", policy=ResearchPolicy(minimum_open_interest=0))
            self.assertEqual(signal.status, "no_trade")

    def test_live_verified_requirement_does_not_upgrade_any_other_evidence(self):
        contracts, quotes = pair()
        for kind, label in (("synthetic", "synthetic"), ("replay_quote", "replay"), ("live_unverified", "live_unverified"), ("live_verified", "live_options_only_underlying_unverified_research")):
            qs = [replace(q, provenance=replace(q.provenance, evidence_kind=kind)) for q in quotes]
            cs = [replace(c, provenance=replace(c.provenance, evidence_kind=kind)) for c in contracts]
            result = generate_signal(cs, qs, as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
            self.assertEqual(result.label, label)
            self.assertEqual(result.underlying_quality, "caller_supplied_unverified")
            gated = generate_signal(cs, qs, as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD", quality_policy=QualityPolicy(require_live_verified=True))
            self.assertEqual(gated.status, "candidate" if kind == "live_verified" else "no_trade")
            self.assertFalse(gated.trading_allowed)

    def test_synthetic_metadata_cannot_gain_a_verified_live_label(self):
        contracts, quotes = pair()
        qs = [replace(q, provenance=replace(q.provenance, evidence_kind="live_verified")) for q in quotes]
        result = generate_signal(contracts, qs, as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
        self.assertEqual(result.label, "mixed_unverified")

    def test_equivalent_numeric_and_timezone_inputs_keep_identity(self):
        contracts, quotes = pair()
        actual = generate_signal(contracts, quotes, as_of=NOW.astimezone(timezone(timedelta(hours=5, minutes=30))), underlying_price=D("100.00"), direction="BULLISH", strategy="DEBIT_SPREAD")
        self.assertEqual(actual.input_fingerprint, candidate().input_fingerprint)

    def test_sequence_regression_retains_earlier_source_observation(self):
        contracts, quotes = pair()
        old = replace(quotes[0], sequence=10, received_at=NOW - timedelta(milliseconds=500))
        later = replace(quotes[0], sequence=1, source_at=NOW, bid=D("60"), ask=D("61"))
        actual = generate_signal(contracts, [later, old, quotes[1]], as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
        self.assertEqual(actual.status, "candidate")
        self.assertEqual(actual.legs[0].quote.sequence, 10)
        self.assertEqual(actual.legs[0].price, D("60"))

    def test_source_switch_requires_an_explicit_new_input_session(self):
        contracts, quotes = pair()
        later = replace(quotes[0], source_at=NOW, provenance=replace(quotes[0].provenance, source="different-provider"))
        result = generate_signal(contracts, [*quotes, later], as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
        self.assertEqual(result.status, "no_trade")

    def test_older_conflicting_history_has_order_independent_rejection(self):
        contracts, quotes = pair()
        old = replace(quotes[0], source_at=NOW - timedelta(seconds=2), received_at=NOW - timedelta(seconds=1))
        conflict = replace(old, bid=D("58"))
        first = generate_signal(contracts, [old, conflict, *quotes], as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
        second = generate_signal(contracts, [*quotes, old, conflict], as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
        self.assertEqual(first, second)
        self.assertEqual(first.status, "no_trade")

    def test_historical_candles_never_become_option_fills(self):
        contracts, quotes = pair()
        qs = [replace(q, provenance=replace(q.provenance, evidence_kind="historical_candle")) for q in quotes]
        result = generate_signal(contracts, qs, as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
        self.assertEqual(result.status, "no_trade")
        self.assertEqual(result.label, "candle_approximate")

    def test_future_data_mutations_do_not_change_an_earlier_signal(self):
        contracts, quotes = pair()
        expected = candidate()
        for offset, price, lot in ((1, "1", 100), (100, "99999", 1)):
            later = NOW + timedelta(seconds=offset)
            future_quote = replace(quotes[0], source_at=later, received_at=later, bid=D(price), ask=D(price), provenance=replace(quotes[0].provenance, captured_at=later))
            future_contract = replace(contracts[0], known_at=later, effective_from=later, lot_size=lot, provenance=replace(contracts[0].provenance, captured_at=later))
            actual = generate_signal([*contracts, future_contract], [*quotes, future_quote], as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
            self.assertEqual(actual, expected)
            self.assertEqual(actual.input_fingerprint, expected.input_fingerprint)

    def test_point_in_time_inputs_and_origin_are_immutable(self):
        contracts, quotes = pair()
        signal = candidate()
        with self.assertRaises(FrozenInstanceError):
            signal.legs[0].price = D("1")
        with self.assertRaises(FrozenInstanceError):
            signal.legs[0].quote.bid = D("1")
        qs = list(quotes)
        qs[0] = replace(qs[0], bid=D("1"))
        self.assertEqual(signal.legs[0].price, D("60"))
        self.assertEqual(json.loads(json.dumps(signal.to_dict()))["legs"][0]["price"], "60")
        self.assertEqual(signal.to_dict()["legs"][0]["quote"]["provenance"]["payload"], {"encoding": "hex", "data": b"book fixture".hex()})

    def test_duplicate_and_out_of_order_input_do_not_change_selected_price(self):
        contracts, quotes = pair()
        older = replace(quotes[0], source_at=NOW - timedelta(seconds=3), received_at=NOW - timedelta(seconds=2), bid=D("58"), ask=D("59"))
        duplicate = replace(quotes[0], received_at=NOW - timedelta(milliseconds=100))
        result = generate_signal(contracts, [quotes[0], older, duplicate, quotes[1]], as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
        reordered = generate_signal(reversed(contracts), [quotes[1], duplicate, older, quotes[0]], as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
        self.assertEqual(result, reordered)
        self.assertEqual(result.entry_net, D("40"))
        self.assertEqual(result.legs[0].quote.received_at, duplicate.received_at)

    def test_conflicting_same_timestamp_quote_cannot_override_the_record(self):
        contracts, quotes = pair()
        for bad in (replace(quotes[0], bid=D("58")), replace(quotes[0], provenance=replace(quotes[0].provenance, evidence_kind="live_verified"))):
            result = generate_signal(contracts, [*quotes, bad], as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
            self.assertEqual(result.status, "no_trade")

    def test_effective_dated_lot_changes_and_ambiguous_versions(self):
        contracts, quotes = pair()
        new = [replace(c, lot_size=50, effective_from=NOW + timedelta(days=1), known_at=NOW + timedelta(days=1)) for c in contracts]
        before = generate_signal([*contracts, *new], quotes, as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
        self.assertEqual(before.legs[0].contract.lot_size, 25)
        conflict = replace(contracts[0], lot_size=50)
        ambiguous = generate_signal([*contracts, conflict], quotes, as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
        self.assertEqual(ambiguous.status, "no_trade")
        after = NOW + timedelta(days=1)
        old = [replace(c, effective_until=after) for c in contracts]
        qs = [replace(q, source_at=after, oi_source_at=after, received_at=after, provenance=replace(q.provenance, captured_at=after)) for q in quotes]
        current = generate_signal([*old, *new], qs, as_of=after, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
        self.assertEqual(current.legs[0].contract.lot_size, 50)
        prior_unit_quotes = [replace(q, source_at=after - timedelta(seconds=1)) for q in qs]
        unavailable = generate_signal([*old, *new], prior_unit_quotes, as_of=after, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD")
        self.assertEqual(unavailable.status, "no_trade")

    def test_policy_and_cutoff_validation(self):
        for kwargs in ({"max_risk_fraction": D("1.01")}, {"capital": D("NaN")}, {"max_contracts": True}, {"horizon_minutes": 0}, {"total_fees": D("-1")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ResearchPolicy(**kwargs)
        with self.assertRaises(ValueError):
            generate_signal([], [], as_of=NOW.replace(tzinfo=None), underlying_price=D("100"), direction="BULLISH", strategy="LONG_OPTION")
        with self.assertRaises(ValueError):
            generate_signal([], [], as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="NAKED_SHORT")


class SizingTests(unittest.TestCase):
    def funded(self, **changes):
        return replace(ResearchPolicy(capital=D("100000"), max_risk_fraction=D("0.10"), max_contracts=2, total_fees=D("10"), margin_per_lot=D("5000")), **changes)

    def test_missing_user_inputs_do_not_invent_quantity(self):
        result = candidate()
        self.assertEqual(result.status, "candidate")
        self.assertIsNone(result.quantity)
        self.assertIsNone(result.expiry_bounds)
        for field in ("capital", "max_risk_fraction", "max_contracts", "total_fees"):
            result = candidate(policy=self.funded(**{field: None}))
            self.assertIsNone(result.quantity)
            self.assertIn(field, result.sizing_reasons[0])

    def test_funded_size_respects_lots_risk_fees_and_user_cap(self):
        result = candidate(policy=self.funded())
        self.assertEqual(result.quantity, 50)
        self.assertEqual(result.expiry_bounds.max_loss, D("2010"))
        self.assertEqual(result.expiry_bounds.max_profit, D("2990"))
        risk_limited = candidate(policy=self.funded(max_risk_fraction=D("0.015")))
        self.assertEqual(risk_limited.quantity, 25)
        no_lot = candidate(policy=self.funded(max_risk_fraction=D("0.01")))
        self.assertIsNone(no_lot.quantity)

    def test_credit_margin_is_separate_and_missing_margin_blocks_sizing(self):
        result = candidate(credit=True, policy=self.funded(margin_per_lot=None))
        self.assertIsNone(result.quantity)
        self.assertIn("margin_per_lot", result.sizing_reasons[0])
        constrained = candidate(credit=True, policy=self.funded(margin_per_lot=D("60000")))
        self.assertEqual(constrained.quantity, 25)
        self.assertEqual(constrained.expiry_bounds.max_loss, D("1885"))

    def test_unknown_or_insufficient_displayed_size_blocks_sizing(self):
        contracts, quotes = pair()
        for size in (None, 24):
            result = generate_signal(contracts, [replace(quotes[0], ask_size=size), quotes[1]], as_of=NOW, underlying_price=D("100"), direction="BULLISH", strategy="DEBIT_SPREAD", policy=self.funded())
            self.assertIsNone(result.quantity)

    def test_planned_stop_does_not_reduce_contractual_loss_or_size(self):
        result = candidate(policy=self.funded(stop_loss_fraction=D("0.10"), take_profit_fraction=D("0.50")))
        self.assertEqual(result.planned_stop_pnl_per_unit, D("-4"))
        self.assertEqual(result.planned_target_pnl_per_unit, D("20"))
        self.assertEqual(result.max_loss_per_unit, D("40"))
        self.assertEqual(result.quantity, candidate(policy=self.funded()).quantity)


if __name__ == "__main__":
    unittest.main()

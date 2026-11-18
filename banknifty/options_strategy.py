"""Deterministic options research over immutable, point-in-time data only.

This module has no transport, broker, persistence or order interface. ``price``
means a quote-side research assumption, never an actual fill. All candidates are
unvalidated hypotheses, even when their input data carries a verified label.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

from .options_data import (
    ContractMetadata, OptionQuote, QualityPolicy, canonical_fingerprint,
    validate_quote, validate_legs, validate_contract,
)

ZERO = Decimal("0")
ONE = Decimal("1")
STRATEGY_VERSION = "options-research-v1"


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")
    return value


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _json(value: Any) -> Any:
    """Lossless public serialization, including raw provenance bytes as hex."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"encoding": "hex", "data": value.hex()}
    if is_dataclass(value):
        return {f.name: _json(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class ResearchPolicy:
    """Operational gates, not personal risk advice or optimized parameters.

    The default spread limit is a configurable data/liquidity screen. Capital,
    risk fraction, fees, quantity cap and credit margin are deliberately absent.
    No quantity is suggested until those relevant user inputs are supplied.
    ``total_fees`` is the user's total round-trip cost estimate at the proposed
    size; confirm it externally. Per-unit fees can instead be incorporated by a
    caller that resolves sizing iteratively. Margin is separate from payoff loss.
    """

    horizon_minutes: int = 30
    max_spread_pct: Decimal = Decimal("10")
    minimum_open_interest: int | None = None
    capital: Decimal | None = None
    max_risk_fraction: Decimal | None = None
    max_contracts: int | None = None
    total_fees: Decimal | None = None
    margin_per_lot: Decimal | None = None
    stop_loss_fraction: Decimal | None = None
    take_profit_fraction: Decimal | None = None
    expiry_at: datetime | None = None
    spread_width: Decimal | None = None

    def __post_init__(self) -> None:
        if isinstance(self.horizon_minutes, bool) or not isinstance(self.horizon_minutes, int) or self.horizon_minutes <= 0:
            raise ValueError("horizon_minutes must be a positive integer")
        _decimal(self.max_spread_pct, "max_spread_pct")
        if self.minimum_open_interest is not None and (
            isinstance(self.minimum_open_interest, bool)
            or not isinstance(self.minimum_open_interest, int)
            or self.minimum_open_interest < 0
        ):
            raise ValueError("minimum_open_interest must be a nonnegative integer")
        for name in ("capital", "max_risk_fraction", "margin_per_lot", "stop_loss_fraction", "take_profit_fraction", "spread_width"):
            value = getattr(self, name)
            if value is not None:
                _decimal(value, name, positive=True)
        if self.max_risk_fraction is not None and self.max_risk_fraction > ONE:
            raise ValueError("max_risk_fraction must be at most one")
        if self.max_contracts is not None and (
            isinstance(self.max_contracts, bool) or not isinstance(self.max_contracts, int) or self.max_contracts <= 0
        ):
            raise ValueError("max_contracts is a positive number of exchange lots")
        if self.total_fees is not None:
            _decimal(self.total_fees, "total_fees")
        if self.expiry_at is not None:
            _aware(self.expiry_at, "expiry_at")


@dataclass(frozen=True)
class SignalLeg:
    contract: ContractMetadata
    side: str
    price: Decimal
    quote: OptionQuote
    ratio: int = 1

    def __post_init__(self) -> None:
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("leg side must be BUY or SELL")
        if type(self.ratio) is not int or self.ratio != 1:
            raise ValueError("only equal-unit single-ratio structures are supported")
        _decimal(self.price, "leg price", positive=True)
        if self.quote.contract_id != self.contract.contract_id:
            raise ValueError("leg quote and contract differ")

    @property
    def contract_id(self) -> str:
        return self.contract.contract_id

    def to_dict(self) -> dict[str, Any]:
        return _json(self)


@dataclass(frozen=True)
class PayoffBounds:
    max_loss: Decimal
    max_profit: Decimal | None
    quantity: int
    fees: Decimal
    assumption: str = "Matched filled units held to expiry; fees included, margin separate; incomplete manual fills invalidate spread bounds."

    def to_dict(self) -> dict[str, Any]:
        return _json(self)


def _validate_structure(legs: tuple[SignalLeg, ...]) -> None:
    if not legs or len(legs) > 2:
        raise ValueError("only long options and matched vertical spreads are supported")
    first = legs[0].contract
    if first.option_type not in {"CALL", "PUT"}:
        raise ValueError("unsupported option type")
    if len(legs) == 1:
        if legs[0].side != "BUY":
            raise ValueError("naked selling is unsupported")
        return
    second = legs[1].contract
    if (first.underlying, first.exchange, first.expiry_at, first.option_type, first.lot_size) != (
        second.underlying, second.exchange, second.expiry_at, second.option_type, second.lot_size
    ):
        raise ValueError("spread legs must have matching underlying, exchange, expiry, type and lot size")
    if first.contract_id == second.contract_id or first.strike == second.strike or {x.side for x in legs} != {"BUY", "SELL"}:
        raise ValueError("spread needs distinct strikes and one hedge per short unit")


def expiry_payoff(
    legs: Iterable[SignalLeg], underlying_at_expiry: Decimal, *, quantity: int = 1, fees: Decimal = ZERO,
) -> Decimal:
    """Net contractual expiry payoff, after premiums and explicit total fees.

    Quantity is matched option units, not lots. Quotes here represent an assumed
    entry; callers evaluating actual fills must replace those prices explicitly.
    This cannot cap interim execution losses or uncovered manual short exposure.
    """
    selected = tuple(legs)
    _validate_structure(selected)
    _decimal(underlying_at_expiry, "underlying_at_expiry")
    _decimal(fees, "fees")
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("quantity must be a positive integer of matched units")
    pnl = ZERO
    for leg in selected:
        strike = leg.contract.strike
        intrinsic = max(ZERO, underlying_at_expiry - strike) if leg.contract.option_type == "CALL" else max(ZERO, strike - underlying_at_expiry)
        pnl += (ONE if leg.side == "BUY" else -ONE) * (intrinsic - leg.price)
    return pnl * quantity - fees


def payoff_bounds(legs: Iterable[SignalLeg], *, quantity: int = 1, fees: Decimal = ZERO) -> PayoffBounds:
    selected = tuple(legs)
    _validate_structure(selected)
    strikes = tuple(leg.contract.strike for leg in selected)
    points = (ZERO, *strikes, max(strikes) + ONE)
    values = tuple(expiry_payoff(selected, value, quantity=quantity, fees=fees) for value in points)
    unlimited = len(selected) == 1 and selected[0].contract.option_type == "CALL"
    return PayoffBounds(max(ZERO, -min(values)), None if unlimited else max(values), quantity, fees)


@dataclass(frozen=True)
class ResearchSignal:
    signal_id: str
    as_of: datetime
    status: str
    strategy: str
    direction: str
    horizon_minutes: int
    underlying_price: Decimal
    legs: tuple[SignalLeg, ...]
    entry_net: Decimal | None
    max_loss_per_unit: Decimal | None
    max_profit_per_unit: Decimal | None
    quantity: int | None
    expiry_bounds: PayoffBounds | None
    planned_stop_pnl_per_unit: Decimal | None
    planned_target_pnl_per_unit: Decimal | None
    sizing_reasons: tuple[str, ...]
    reasons: tuple[str, ...]
    label: str
    input_fingerprint: str
    policy: ResearchPolicy
    quality_policy: QualityPolicy
    underlying_quality: str = "caller_supplied_unverified"
    confidence: None = None
    calibrated: bool = False
    trading_allowed: bool = False
    formula_version: str = STRATEGY_VERSION

    def to_dict(self) -> dict[str, Any]:
        result = _json(self)
        result["risk_notice"] = "Unvalidated research. Stops are exit targets, not guaranteed loss caps. Spread expiry bounds require all matched units; margin is separate."
        return result


def _point_in_time(
    contracts: Iterable[ContractMetadata], quotes: Iterable[OptionQuote], as_of: datetime,
) -> tuple[tuple[ContractMetadata, ...], tuple[OptionQuote, ...], tuple[str, ...]]:
    # Future observations/metadata never enter this fingerprint or selection.
    by_contract: dict[str, ContractMetadata] = {}
    reasons: list[str] = []
    conflicts: set[str] = set()
    for contract in contracts:
        errors = validate_contract(contract, as_of)
        if errors:
            temporal = {"metadata_not_known_at_cutoff", "metadata_not_yet_effective", "contract_provenance_after_cutoff", "metadata_no_longer_effective", "contract_expired"}
            if not any(error in temporal for error in errors):
                reasons.extend(f"{contract.contract_id}: {error}" for error in errors)
            continue
        previous = by_contract.get(contract.contract_id)
        if previous is not None and contract != previous:
            conflicts.add(contract.contract_id)
        elif previous is None:
            by_contract[contract.contract_id] = contract
    for contract_id in sorted(conflicts):
        by_contract.pop(contract_id, None)
        reasons.append(f"Conflicting contract metadata: {contract_id}")
    visible_quotes = tuple(q for q in quotes if q.received_at <= as_of and q.provenance.captured_at <= as_of and (q.source_at is None or q.source_at <= as_of))
    by_quote: dict[str, OptionQuote] = {}
    quote_conflicts: set[str] = set()
    event_values: dict[tuple, tuple] = {}
    sources: dict[str, tuple] = {}
    # Receipt is the order in which information became available. A canonical
    # tie-break makes identical history sets independent of input list order.
    ordered = sorted(visible_quotes, key=lambda q: (q.received_at, q.source_at or q.received_at,
                                                   q.sequence if q.sequence is not None else -1, q.fingerprint))
    for quote in ordered:
        if quote.contract_id not in by_contract:
            continue
        source = (quote.provenance.source, quote.provenance.evidence_kind)
        if quote.contract_id in sources and sources[quote.contract_id] != source:
            quote_conflicts.add(quote.contract_id)
        sources[quote.contract_id] = source
        identity = (quote.contract_id, quote.source_at, quote.sequence)
        data_fields = ("bid", "ask", "bid_size", "ask_size", "last", "open_interest", "oi_source_at")
        values = tuple(getattr(quote, name) for name in data_fields) + source
        if identity in event_values and event_values[identity] != values:
            quote_conflicts.add(quote.contract_id)
        event_values[identity] = values
        previous = by_quote.get(quote.contract_id)
        if previous is None:
            by_quote[quote.contract_id] = quote
            continue
        # Missing source time cannot masquerade as later exchange information.
        if previous.source_at is not None and quote.source_at is None:
            continue
        if previous.source_at is not None and quote.source_at is not None and quote.source_at < previous.source_at:
            reasons.append(f"Ignored out-of-order source timestamp: {quote.contract_id}")
            continue
        if previous.sequence is not None and (quote.sequence is None or quote.sequence < previous.sequence):
            reasons.append(f"Ignored out-of-order or missing sequence: {quote.contract_id}")
            continue
        if previous.sequence is not None and quote.sequence == previous.sequence and quote.source_at != previous.source_at:
            quote_conflicts.add(quote.contract_id)
            continue
        # Identical repeats retain their first receipt rather than refreshing age.
        if quote.source_at == previous.source_at and quote.sequence == previous.sequence:
            continue
        if quote.source_at is not None or previous.source_at is None:
            by_quote[quote.contract_id] = quote
    for contract_id in sorted(quote_conflicts):
        by_quote.pop(contract_id, None)
        reasons.append(f"Conflicting quote identity or source/evidence switch: {contract_id}")
    return (
        tuple(by_contract[key] for key in sorted(by_contract)),
        tuple(by_quote[key] for key in sorted(by_quote)),
        tuple(sorted(set(reasons))),
    )


def point_in_time_inputs(contracts, quotes, as_of):
    """Public deterministic cutoff/metadata/history gate for orchestration."""
    return _point_in_time(contracts, quotes, as_of)


def _label(quotes: tuple[OptionQuote, ...], contracts: tuple[ContractMetadata, ...] = ()) -> str:
    kinds = {q.provenance.evidence_kind for q in quotes} | {c.provenance.evidence_kind for c in contracts}
    if any(q.source_at is None for q in quotes):
        return "receipt_only"
    if "historical_candle" in kinds:
        return "candle_approximate"
    if not kinds:
        return "unavailable"
    if len(kinds) > 1:
        return "mixed_unverified"
    kind = next(iter(kinds))
    return {"synthetic": "synthetic", "replay_quote": "replay", "live_unverified": "live_unverified", "live_verified": "live_options_only_underlying_unverified_research", "historical_candle": "candle_approximate"}.get(kind, "unavailable")


def _size(legs: tuple[SignalLeg, ...], bounds: PayoffBounds, strategy: str, policy: ResearchPolicy) -> tuple[int | None, tuple[str, ...]]:
    missing = [name for name in ("capital", "max_risk_fraction", "max_contracts", "total_fees") if getattr(policy, name) is None]
    if strategy == "CREDIT_SPREAD" and policy.margin_per_lot is None:
        missing.append("margin_per_lot")
    if missing:
        return None, ("Sizing unavailable; supply " + ", ".join(missing),)
    assert policy.capital is not None and policy.max_risk_fraction is not None and policy.max_contracts is not None and policy.total_fees is not None
    lot = legs[0].contract.lot_size
    available = policy.capital * policy.max_risk_fraction - policy.total_fees
    if available <= ZERO or bounds.max_loss <= ZERO:
        return None, ("No positive risk budget remains after explicit fees",)
    lots = min(policy.max_contracts, int(available // (bounds.max_loss * lot)))
    premium = sum((leg.price if leg.side == "BUY" else -leg.price) for leg in legs)
    if premium > ZERO:
        lots = min(lots, int(max(ZERO, policy.capital - policy.total_fees) // (premium * lot)))
    if strategy == "CREDIT_SPREAD":
        assert policy.margin_per_lot is not None
        lots = min(lots, int(max(ZERO, policy.capital - policy.total_fees) // policy.margin_per_lot))
    for leg in legs:
        depth = leg.quote.ask_size if leg.side == "BUY" else leg.quote.bid_size
        if depth is None:
            return None, (f"Sizing unavailable; displayed quantity is unknown for {leg.contract_id}",)
        lots = min(lots, depth // lot)
    if lots <= 0:
        return None, ("No full exchange lot fits risk, funding, margin and displayed size limits",)
    return lots * lot, ("Research quantity only; displayed depth is cancellable and fills/margin must be checked manually",)


def quote_filter_reasons(quote: OptionQuote, contract: ContractMetadata, *, as_of: datetime,
                         policy: ResearchPolicy = ResearchPolicy(),
                         quality_policy: QualityPolicy = QualityPolicy()) -> tuple[str, ...]:
    """Shared live/replay structural and configurable spread/OI rejection gate."""
    quality = validate_quote(quote, contract, as_of, quality_policy)
    if not quality.accepted:
        return quality.reasons
    assert quote.bid is not None and quote.ask is not None
    reasons = []
    if quote.ask <= ZERO or quote.bid <= ZERO:
        reasons.append("positive_executable_quote_sides_required")
    elif (quote.ask - quote.bid) / quote.ask * 100 > policy.max_spread_pct:
        reasons.append("spread_exceeds_configured_percent_limit")
    if policy.minimum_open_interest is not None:
        if quote.open_interest is None or quote.oi_source_at is None:
            reasons.append("fresh_open_interest_unavailable")
        else:
            age = (as_of - quote.oi_source_at).total_seconds()
            if age < 0 or age > quality_policy.max_oi_age_seconds or quote.open_interest < policy.minimum_open_interest:
                reasons.append("open_interest_gate_failed")
    return tuple(reasons)


def generate_signal(
    contracts: Iterable[ContractMetadata], quotes: Iterable[OptionQuote], *,
    as_of: datetime, underlying_price: Decimal, direction: str, strategy: str,
    policy: ResearchPolicy = ResearchPolicy(), quality_policy: QualityPolicy = QualityPolicy(),
) -> ResearchSignal:
    """Select listed point-in-time contracts using one reproducible pure function.

    Direction is a caller-supplied hypothesis, not an inferred probability. Calls
    express bullish debit/long views; puts bearish debit/long views. Bullish credit
    spreads use puts and bearish credit spreads calls. The nearer out-of-money
    strike is paired with the next listed outward strike (or exact user width),
    at the earliest eligible listed expiry. No strike, expiry or lot is invented.
    """
    _aware(as_of, "as_of")
    _decimal(underlying_price, "underlying_price", positive=True)
    if strategy not in {"LONG_OPTION", "DEBIT_SPREAD", "CREDIT_SPREAD"}:
        raise ValueError("unsupported strategy; naked selling and martingale are excluded")
    if direction not in {"BULLISH", "BEARISH", "NEUTRAL"}:
        raise ValueError("direction must be BULLISH, BEARISH or NEUTRAL")
    visible_contracts, visible_quotes, conflicts = _point_in_time(contracts, quotes, as_of)
    fingerprint = canonical_fingerprint({"as_of": as_of, "underlying_price": underlying_price, "direction": direction, "strategy": strategy, "policy": policy, "quality_policy": quality_policy, "contracts": visible_contracts, "quotes": visible_quotes, "conflicts": conflicts, "version": STRATEGY_VERSION})
    signal_id = fingerprint
    label = _label(visible_quotes, visible_contracts)

    def abstain(*reasons: str) -> ResearchSignal:
        return ResearchSignal(signal_id, as_of, "no_trade", strategy, direction, policy.horizon_minutes, underlying_price, (), None, None, None, None, None, None, None, ("No candidate to size",), tuple(reasons), label, fingerprint, policy, quality_policy)

    if direction == "NEUTRAL":
        return abstain("Neutral hypothesis: no directional candidate")
    wanted_type = "CALL" if (direction == "BULLISH") != (strategy == "CREDIT_SPREAD") else "PUT"
    by_quote = {q.contract_id: q for q in visible_quotes}
    eligible: list[ContractMetadata] = []
    rejection_reasons = list(conflicts)
    cutoff = as_of + timedelta(minutes=policy.horizon_minutes)
    for contract in visible_contracts:
        if contract.option_type != wanted_type or not contract.listed:
            continue
        if contract.expiry_at <= cutoff or (policy.expiry_at is not None and contract.expiry_at != policy.expiry_at):
            continue
        quote = by_quote.get(contract.contract_id)
        if quote is None:
            rejection_reasons.append(f"No point-in-time quote: {contract.contract_id}")
            continue
        failures = quote_filter_reasons(quote, contract, as_of=as_of, policy=policy, quality_policy=quality_policy)
        if failures:
            rejection_reasons.extend(f"{contract.contract_id}: {reason}" for reason in failures)
            continue
        eligible.append(contract)
    # Exact same underlying is required for all candidates. A mixed input set
    # needs the caller to choose an underlying explicitly, avoiding silent mixes.
    if len({(c.underlying, c.exchange) for c in eligible}) > 1:
        return abstain("Multiple underlying/exchange identities: select one explicit instrument")
    anchors = [c for c in eligible if (c.strike >= underlying_price if wanted_type == "CALL" else c.strike <= underlying_price)]
    anchors.sort(key=lambda c: (c.expiry_at, abs(c.strike - underlying_price), c.contract_id))
    selected: tuple[SignalLeg, ...] | None = None
    for anchor in anchors:
        if strategy == "LONG_OPTION":
            selected = (SignalLeg(anchor, "BUY", by_quote[anchor.contract_id].ask, by_quote[anchor.contract_id]),)
            break
        wings = [c for c in eligible if c.expiry_at == anchor.expiry_at and c.lot_size == anchor.lot_size
                 and (c.strike > anchor.strike if wanted_type == "CALL" else c.strike < anchor.strike)
                 and (policy.spread_width is None or abs(c.strike - anchor.strike) == policy.spread_width)]
        wings.sort(key=lambda c: (abs(c.strike - anchor.strike), c.contract_id))
        for wing in wings:
            pair = (anchor, wing)
            leg_quotes = tuple(by_quote[c.contract_id] for c in pair)
            joint = validate_legs(leg_quotes, pair, as_of, quality_policy)
            if not joint.accepted:
                rejection_reasons.extend(str(reason) for reason in joint.reasons)
                continue
            anchor_side = "SELL" if strategy == "CREDIT_SPREAD" else "BUY"
            wing_side = "BUY" if strategy == "CREDIT_SPREAD" else "SELL"
            candidate = tuple(SignalLeg(c, side, quote.ask if side == "BUY" else quote.bid, quote) for c, side, quote in zip(pair, (anchor_side, wing_side), leg_quotes))
            entry = sum((leg.price if leg.side == "BUY" else -leg.price) for leg in candidate)
            width = abs(anchor.strike - wing.strike)
            if (strategy == "DEBIT_SPREAD" and not ZERO < entry < width) or (strategy == "CREDIT_SPREAD" and not -width < entry < ZERO):
                rejection_reasons.append("Entry premium is inconsistent with the supported spread payoff")
                continue
            selected = candidate
            break
        if selected is not None:
            break
    if selected is None:
        return abstain("No listed, fresh, synchronized contract structure passes configured gates", *tuple(dict.fromkeys(rejection_reasons)))
    bounds = payoff_bounds(selected)
    quantity, sizing_reasons = _size(selected, bounds, strategy, policy)
    entry = sum((leg.price if leg.side == "BUY" else -leg.price) for leg in selected)
    overall = payoff_bounds(selected, quantity=quantity, fees=policy.total_fees) if quantity is not None and policy.total_fees is not None else None
    stop = -abs(entry) * policy.stop_loss_fraction if policy.stop_loss_fraction is not None else None
    target = abs(entry) * policy.take_profit_fraction if policy.take_profit_fraction is not None else None
    selected_label = _label(tuple(leg.quote for leg in selected), tuple(leg.contract for leg in selected))
    return ResearchSignal(signal_id, as_of, "candidate", strategy, direction, policy.horizon_minutes, underlying_price, selected, entry, bounds.max_loss, bounds.max_profit, quantity, overall, stop, target, sizing_reasons,
        ("Unvalidated deterministic research hypothesis; no calibrated confidence", "Underlying reference price is caller-supplied; its source timestamp and freshness are unverified", "Entry uses ask for buys and bid for sells; it is not a fill", "Expiry loss bounds require matched fills; planned stops and margin are separate"), selected_label, fingerprint, policy, quality_policy)

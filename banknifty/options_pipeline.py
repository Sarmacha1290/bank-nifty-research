"""Point-in-time underlying observations to explainable options research.

All computations are pure apart from the optional in-memory deduplication
wrapper. Sampled one-minute OHLC is not an official exchange candle. The fixed
momentum/trend rule is an unvalidated hypothesis with no confidence estimate.
No network, credentials, broker client, order, volume or OI inference exists.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
from typing import Iterable

from .options_data import ContractMetadata, OptionQuote, QualityPolicy, RawProvenance, canonical_fingerprint
from .options_strategy import ResearchPolicy, ResearchSignal, generate_signal, point_in_time_inputs, _json

PIPELINE_VERSION = "closed-minute-momentum-trend-v1"


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


@dataclass(frozen=True)
class UnderlyingObservation:
    """An actual observed index price; absent source time stays unknown.

    ``received_at`` is the observation's original receipt time. An archive import
    time belongs to its import manifest, not this market-data timestamp. Raw
    provider bytes and evidence classification remain immutable provenance.
    """
    symbol: str
    value: Decimal
    source_at: datetime | None
    received_at: datetime
    provenance: RawProvenance
    sequence: int | None = None

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol.strip() or any(ord(c) < 32 for c in self.symbol):
            raise ValueError("Underlying symbol must be explicit printable text")
        if not isinstance(self.value, Decimal) or not self.value.is_finite() or self.value <= 0:
            raise ValueError("Underlying value must be a positive finite Decimal")
        if not _aware(self.received_at) or (self.source_at is not None and not _aware(self.source_at)):
            raise ValueError("Underlying timestamps must be timezone aware")
        if not isinstance(self.provenance, RawProvenance):
            raise TypeError("Underlying observation requires immutable raw provenance")
        if self.sequence is not None and (type(self.sequence) is not int or self.sequence < 0):
            raise ValueError("Underlying sequence must be a nonnegative integer or missing")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self)

    def to_dict(self):
        return _json(self)


@dataclass(frozen=True)
class PipelinePolicy:
    """Operational limits mirroring the existing sampled-minute quality gate.

    Ten closed bars, three-minute momentum and SMA3/SMA10 are fixed rule
    definitions in version v1, not optimized values. Limits here control missing
    data, receipt latency and when a boundary may be processed. They are not
    personal financial advice or a claim of adequate live execution speed.
    """
    minimum_samples_per_minute: int = 12
    max_edge_gap_seconds: float = 10
    max_internal_gap_seconds: float = 15
    max_source_receipt_delay_seconds: float = 5
    max_latest_source_age_seconds: float = 5
    max_decision_delay_seconds: float = 5
    require_live_verified: bool = False

    def __post_init__(self):
        if type(self.minimum_samples_per_minute) is not int or self.minimum_samples_per_minute < 2:
            raise ValueError("At least two distinct samples per minute are required")
        for name in ("max_edge_gap_seconds", "max_internal_gap_seconds", "max_source_receipt_delay_seconds", "max_latest_source_age_seconds", "max_decision_delay_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if type(self.require_live_verified) is not bool:
            raise ValueError("require_live_verified must be boolean")


@dataclass(frozen=True)
class ClosedUnderlyingBar:
    symbol: str
    start_at: datetime
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    sample_count: int
    first_source_at: datetime
    last_source_at: datetime
    observations: tuple[UnderlyingObservation, ...]
    input_fingerprint: str
    label: str
    kind: str = "sampled_one_minute_ohlc"

    def to_dict(self):
        return _json(self)


@dataclass(frozen=True)
class DirectionAssessment:
    symbol: str
    as_of: datetime
    direction: str
    momentum_return: Decimal
    fast_mean: Decimal
    slow_mean: Decimal
    reference_price: Decimal
    reference_source_at: datetime
    heuristic_strength: Decimal
    reasons: tuple[str, ...]
    input_fingerprint: str
    formula_version: str = PIPELINE_VERSION
    confidence: None = None

    @property
    def strength(self) -> Decimal:
        """Absolute 3-minute return divided by 1%, capped at one; not probability."""
        return self.heuristic_strength

    def to_dict(self):
        return _json(self)


@dataclass(frozen=True)
class PipelineDecision:
    status: str
    boundary: datetime
    as_of: datetime
    assessment: DirectionAssessment | None
    signal: ResearchSignal | None
    bars: tuple[ClosedUnderlyingBar, ...]
    reasons: tuple[str, ...]
    input_fingerprint: str
    label: str
    policy: PipelinePolicy
    observed_inputs: tuple[UnderlyingObservation, ...]
    option_contracts: tuple[ContractMetadata, ...] = ()
    option_quotes: tuple[OptionQuote, ...] = ()
    research_policy: ResearchPolicy = ResearchPolicy()
    quality_policy: QualityPolicy = QualityPolicy()
    formula_version: str = PIPELINE_VERSION
    trading_allowed: bool = False
    confidence: None = None
    data_gate_passed: bool = False
    conflict_events: tuple = ()

    def to_dict(self):
        result = _json(self)
        # The signal's explicit notice is retained for JSON/export consumers.
        if self.signal is not None:
            result["signal"] = self.signal.to_dict()
        return result


def closed_boundary(as_of: datetime) -> datetime:
    if not _aware(as_of):
        raise ValueError("Pipeline cutoff must be timezone aware")
    return as_of.astimezone(timezone.utc).replace(second=0, microsecond=0)


def _evidence_label(observations: Iterable[UnderlyingObservation], contracts=(), quotes=()) -> str:
    rows = tuple(observations)
    if any(item.source_at is None for item in rows):
        return "receipt_only"
    kinds = {row.provenance.evidence_kind for row in rows}
    kinds.update(item.provenance.evidence_kind for item in contracts)
    kinds.update(item.provenance.evidence_kind for item in quotes)
    if not kinds:
        return "unavailable"
    if "historical_candle" in kinds:
        return "candle_approximate"
    if len(kinds) != 1:
        return "mixed_unverified"
    return {"synthetic": "synthetic", "replay_quote": "replay", "live_unverified": "live_unverified",
            "live_verified": "live_data_verified_research_unvalidated"}[next(iter(kinds))]


def _visible_underlying(observations: Iterable[UnderlyingObservation], as_of: datetime, start_at: datetime, end_at: datetime,
                        policy: PipelinePolicy) -> tuple[tuple[UnderlyingObservation, ...], tuple[str, ...], tuple[UnderlyingObservation, ...]]:
    visible = []
    for row in observations:
        if not isinstance(row, UnderlyingObservation):
            raise TypeError("Pipeline requires UnderlyingObservation records")
        if row.received_at > as_of or row.provenance.captured_at > as_of or (row.source_at is not None and row.source_at > as_of):
            continue
        if (row.source_at or row.received_at) < start_at:
            continue
        if row.source_at is not None and row.source_at >= end_at:
            continue
        visible.append(row)
    # Canonical sort allows the same archive to be replayed in any input order.
    visible.sort(key=lambda x: (x.received_at, x.source_at or x.received_at, x.sequence if x.sequence is not None else -1, x.fingerprint))
    reasons = []
    identities = {(row.symbol, row.provenance.source, row.provenance.evidence_kind) for row in visible}
    if len(identities) > 1:
        reasons.append("underlying_source_symbol_or_evidence_switch")
    source_values: dict[datetime, Decimal] = {}
    sequence_values: dict[int, tuple] = {}
    accepted: dict[datetime, UnderlyingObservation] = {}
    previous: UnderlyingObservation | None = None
    sequence_high_water: int | None = None
    for row in visible:
        if row.source_at is None:
            reasons.append("underlying_unknown_source_freshness")
            continue
        if row.provenance.evidence_kind == "historical_candle":
            reasons.append("historical_candle_not_underlying_observation")
            continue
        if policy.require_live_verified and row.provenance.evidence_kind != "live_verified":
            reasons.append("underlying_live_evidence_unverified")
            continue
        lag = (row.received_at - row.source_at).total_seconds()
        if lag < 0 or lag > policy.max_source_receipt_delay_seconds:
            reasons.append("underlying_source_receipt_latency_rejected")
            continue
        if row.source_at in source_values and source_values[row.source_at] != row.value:
            reasons.append("underlying_conflicting_source_timestamp")
            continue
        source_values[row.source_at] = row.value
        if row.sequence is not None:
            event = (row.source_at, row.value)
            if row.sequence in sequence_values and sequence_values[row.sequence] != event:
                reasons.append("underlying_conflicting_sequence")
                continue
            sequence_values[row.sequence] = event
        if sequence_high_water is not None and (row.sequence is None or row.sequence < sequence_high_water):
            reasons.append("underlying_sequence_regressed_or_disappeared")
            continue
        if row.sequence is not None:
            sequence_high_water = row.sequence
        if row.source_at in accepted:
            # Identical repeated cache values keep their original receipt.
            # Their sequence still advances the stream high-water mark.
            continue
        if previous is not None:
            if row.source_at < previous.source_at:
                reasons.append("underlying_out_of_order_source")
                continue
            if previous.sequence is not None and (row.sequence is None or row.sequence < previous.sequence):
                reasons.append("underlying_sequence_regressed_or_disappeared")
                continue
        accepted[row.source_at] = row
        previous = row
    # Conflict and sequencing diagnostics deliberately block this bounded
    # history. Reconnect cannot silently rewrite a previously processed bar.
    return tuple(sorted(accepted.values(), key=lambda x: x.source_at)), tuple(sorted(set(reasons))), tuple(visible)


def build_closed_bars(observations: Iterable[UnderlyingObservation], *, as_of: datetime,
                      policy: PipelinePolicy = PipelinePolicy()) -> tuple[tuple[ClosedUnderlyingBar, ...], tuple[UnderlyingObservation, ...], tuple[str, ...]]:
    """Build exactly ten complete [start,end) minute intervals at a cutoff.

    No forward fill, sparse-bar approximation, tick at ``end`` or subsequently
    received record enters a bar. Every bar retains its exact source records.
    """
    boundary = closed_boundary(as_of)
    start = boundary - timedelta(minutes=10)
    rows, reasons, visible = _visible_underlying(observations, as_of, start, boundary, policy)
    closed = tuple(row for row in rows if row.source_at < boundary)
    failures = list(reasons)
    bars = []
    if (as_of - boundary).total_seconds() > policy.max_decision_delay_seconds:
        failures.append("decision_after_closed_boundary_window")
    if not closed:
        failures.append("no_timestamped_closed_underlying_observations")
    elif (as_of - closed[-1].source_at).total_seconds() > policy.max_latest_source_age_seconds:
        failures.append("latest_underlying_source_stale")
    for minute in range(10):
        begin = start + timedelta(minutes=minute)
        end = begin + timedelta(minutes=1)
        values = tuple(row for row in closed if begin <= row.source_at < end)
        minute_failures = []
        if len(values) < policy.minimum_samples_per_minute:
            minute_failures.append("insufficient_distinct_samples")
        if values:
            if (values[0].source_at - begin).total_seconds() > policy.max_edge_gap_seconds:
                minute_failures.append("missing_start_edge")
            if (end - values[-1].source_at).total_seconds() > policy.max_edge_gap_seconds:
                minute_failures.append("missing_end_edge")
            if any((right.source_at - left.source_at).total_seconds() > policy.max_internal_gap_seconds for left, right in zip(values, values[1:])):
                minute_failures.append("internal_sample_gap")
        if minute_failures:
            failures.extend(f"{begin.isoformat()}:{reason}" for reason in minute_failures)
            continue
        prices = tuple(row.value for row in values)
        bars.append(ClosedUnderlyingBar(values[0].symbol, begin, end, prices[0], max(prices), min(prices), prices[-1], len(values),
                                        values[0].source_at, values[-1].source_at, values,
                                        canonical_fingerprint(values, begin, end, policy), _evidence_label(values)))
    return tuple(bars), visible, tuple(sorted(set(failures)))


def assess_direction(bars: Iterable[ClosedUnderlyingBar]) -> DirectionAssessment:
    """Fixed rule: three-minute close return and SMA3 minus SMA10 must agree."""
    rows = tuple(bars)
    if len(rows) != 10 or any(left.end_at != right.start_at for left, right in zip(rows, rows[1:])):
        raise ValueError("Direction rule requires ten contiguous closed minute bars")
    if len({row.symbol for row in rows}) != 1:
        raise ValueError("Direction bars must share one exact underlying symbol")
    closes = tuple(row.close for row in rows)
    momentum = closes[-1] / closes[-4] - Decimal(1)
    fast = sum(closes[-3:]) / Decimal(3)
    slow = sum(closes) / Decimal(10)
    direction = "BULLISH" if momentum > 0 and fast > slow else "BEARISH" if momentum < 0 and fast < slow else "NEUTRAL"
    strength = min(Decimal(1), abs(momentum) / Decimal("0.01"))
    reasons = (f"Three-minute close return: {momentum}", f"SMA3: {fast}; SMA10: {slow}",
               f"Momentum and trend {'agree ' + direction.lower() if direction != 'NEUTRAL' else 'do not agree; abstain'}",
               "Fixed unvalidated rule; no predictive probability or index volume/OI inference")
    return DirectionAssessment(rows[-1].symbol, rows[-1].end_at, direction, momentum, fast, slow,
                               closes[-1], rows[-1].last_source_at, strength, reasons,
                               canonical_fingerprint(rows, PIPELINE_VERSION))


def _relevant_conflicts(events, underlying, contracts, quotes, as_of):
    """Admit only published conflict facts affecting this frozen input window."""
    events = tuple(events)
    if not events:
        return ()
    from .market_archive import ArchiveConflict
    if any(not isinstance(event, ArchiveConflict) for event in events):
        raise TypeError("Pipeline conflicts must be typed immutable archive facts")
    contract_ids = {item.contract_id for item in contracts}
    visible_quotes = tuple(item for item in quotes if isinstance(item, OptionQuote)
        and item.contract_id in contract_ids and _aware(item.received_at) and item.received_at <= as_of
        and _aware(item.provenance.captured_at) and item.provenance.captured_at <= as_of
        and (item.source_at is None or (_aware(item.source_at) and item.source_at <= as_of)))
    consumed = (*underlying, *contracts, *visible_quotes)
    relevant = {event.fingerprint: event for event in events
        if event.available_at <= as_of and any(event.matches(item) for item in consumed)}
    return tuple(relevant[key] for key in sorted(relevant))


def run_pipeline(
    underlying: Iterable[UnderlyingObservation], contracts: Iterable[ContractMetadata], quotes: Iterable[OptionQuote], *,
    as_of: datetime, strategy: str, research_policy: ResearchPolicy = ResearchPolicy(),
    quality_policy: QualityPolicy = QualityPolicy(), pipeline_policy: PipelinePolicy = PipelinePolicy(),
    conflict_events=(),
) -> PipelineDecision:
    """Shared replay/live computation; never reads a clock, provider or account."""
    boundary = closed_boundary(as_of)
    bars, rows, failures = build_closed_bars(underlying, as_of=as_of, policy=pipeline_policy)
    quote_history = tuple(quotes)
    contract_rows, quote_rows, option_diagnostics = point_in_time_inputs(contracts, quote_history, as_of)
    conflicts = _relevant_conflicts(conflict_events, rows, contract_rows, quote_history, as_of)
    fingerprint = canonical_fingerprint(PIPELINE_VERSION, rows, bars, failures, contract_rows, quote_rows,
                                        option_diagnostics, as_of, strategy, research_policy, quality_policy, pipeline_policy,
                                        conflicts)
    label = _evidence_label(rows, contract_rows, quote_rows)

    def result(status, assessment, signal, reasons, identity, *, gated=False):
        return PipelineDecision(status, boundary, as_of, assessment, signal, bars, reasons, identity,
                                label, pipeline_policy, rows, contract_rows, quote_rows, research_policy, quality_policy,
                                data_gate_passed=gated, conflict_events=conflicts)

    if failures or len(bars) != 10:
        return result("no_trade", None, None, failures or ("ten_complete_bars_required",), fingerprint)
    assessment = assess_direction(bars)
    if conflicts:
        return result("no_trade", assessment, None, ("published_archive_identity_conflict",), fingerprint)
    option_failures = tuple(reason for reason in option_diagnostics if not reason.startswith("Ignored out-of-order"))
    if option_failures:
        return result("no_trade", assessment, None, ("option_input_gate_failed", *option_failures), fingerprint)
    if label == "mixed_unverified":
        return result("no_trade", assessment, None, ("pipeline_mixed_evidence_kinds",), fingerprint)
    if quality_policy.require_live_verified and any(row.provenance.evidence_kind != "live_verified" for row in rows):
        return result("no_trade", assessment, None, ("underlying_live_evidence_unverified",), fingerprint)
    if any(contract.underlying != assessment.symbol for contract in contract_rows):
        return result("no_trade", assessment, None, ("underlying_and_option_catalogue_identity_mismatch",), fingerprint)
    if assessment.direction == "NEUTRAL":
        # A neutral rule outcome is a valid abstention only when both of its
        # directional comparisons have usable books/metadata/structures. The
        # manual strategy's NEUTRAL shortcut deliberately does not check those
        # inputs, so it cannot establish successful pipeline data coverage.
        checks = tuple(generate_signal(contract_rows, quote_rows, as_of=as_of,
                       underlying_price=assessment.reference_price, direction=direction,
                       strategy=strategy, policy=research_policy, quality_policy=quality_policy)
                       for direction in ("BULLISH", "BEARISH"))
        failed = tuple(check for check in checks if check.status != "candidate")
        if failed:
            reasons = ("neutral_option_data_gate_failed",) + tuple(sorted({
                reason for check in failed for reason in check.reasons}))
            return result("no_trade", assessment, None, reasons, fingerprint)
    signal = generate_signal(contract_rows, quote_rows, as_of=as_of, underlying_price=assessment.reference_price,
                             direction=assessment.direction, strategy=strategy, policy=research_policy, quality_policy=quality_policy)
    combined = canonical_fingerprint(fingerprint, assessment, signal.input_fingerprint)
    # Pipeline lineage is additive. Manual generate_signal retains its original
    # unverified-price label and behavior for existing users.
    updated_reasons = tuple(reason for reason in signal.reasons if not reason.startswith("Underlying reference price is caller-supplied"))
    signal = replace(signal, signal_id=combined, input_fingerprint=combined,
                     underlying_quality="timestamped_closed_bar_structural_checks_passed",
                     label=label, reasons=updated_reasons + assessment.reasons)
    return result(signal.status, assessment, signal, signal.reasons, combined,
                  gated=signal.status == "candidate" or assessment.direction == "NEUTRAL")


def generate_baseline(decision: PipelineDecision, contracts, quotes, *, direction: str, strategy: str,
                      research_policy: ResearchPolicy = ResearchPolicy(), quality_policy: QualityPolicy = QualityPolicy()) -> ResearchSignal | None:
    """Always-direction comparison using the identical qualified snapshot gates.

    Baselines are permitted only after the underlying pipeline gate completed;
    they cannot turn a data failure into a candidate. They are distinct, labelled
    hypotheses and remain subject to all option metadata/liquidity filters.
    """
    if direction not in {"BULLISH", "BEARISH"}:
        raise ValueError("Baseline direction must be BULLISH or BEARISH")
    if decision.assessment is None or decision.signal is None or len(decision.bars) != 10:
        return None
    contract_rows, quote_rows, diagnostics = point_in_time_inputs(contracts, quotes, decision.as_of)
    if (canonical_fingerprint(contract_rows, quote_rows) != canonical_fingerprint(decision.option_contracts, decision.option_quotes)
            or research_policy != decision.research_policy or quality_policy != decision.quality_policy):
        raise ValueError("Baseline requires the identical frozen option snapshot and research/quality policies")
    if any(contract.underlying != decision.assessment.symbol for contract in contract_rows):
        raise ValueError("Baseline underlying identity differs from the qualified pipeline")
    signal = generate_signal(contract_rows, quote_rows, as_of=decision.as_of, underlying_price=decision.assessment.reference_price,
                             direction=direction, strategy=strategy, policy=research_policy, quality_policy=quality_policy)
    fingerprint = canonical_fingerprint("always-direction-baseline-v1", decision.input_fingerprint, direction, signal.input_fingerprint)
    return replace(signal, signal_id=fingerprint, input_fingerprint=fingerprint, label=decision.label,
                   underlying_quality="timestamped_closed_bar_structural_checks_passed",
                   reasons=tuple(x for x in signal.reasons if not x.startswith("Underlying reference price is caller-supplied"))
                   + (f"Fixed always-{direction.lower()} comparison baseline; no predictive confidence",))


@dataclass(frozen=True)
class PipelineEmission:
    emitted: bool
    decision: PipelineDecision | None
    reason: str
    prior_fingerprint: str | None = None


class PipelineSession:
    """One frozen decision attempt per closed boundary and strategy policy.

    Persist ``checkpoint()`` through the caller's archive if restart durability
    is needed. This wrapper itself never writes or connects. Late/reconnected
    data cannot revise an already attempted boundary, including an abstention.
    Duplicate calls return no new decision, avoiding a stale candidate refresh.
    """
    def __init__(self, checkpoint: Iterable[tuple[str, str]] = (), *, namespace: str = "default"):
        if not isinstance(namespace, str) or not namespace.strip() or any(ord(char) < 32 for char in namespace):
            raise ValueError("Pipeline session namespace must be fixed printable text")
        self._namespace = namespace
        self._processed = dict(checkpoint)
        if any(not isinstance(key, str) or not isinstance(value, str) or len(key) != 64 or len(value) != 64 for key, value in self._processed.items()):
            raise ValueError("Invalid pipeline checkpoint")

    def checkpoint(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._processed.items()))

    def process(self, underlying, contracts, quotes, *, as_of, strategy,
                research_policy=ResearchPolicy(), quality_policy=QualityPolicy(), pipeline_policy=PipelinePolicy(),
                conflict_events=()) -> PipelineEmission:
        underlying_rows = tuple(underlying)
        # A fixed caller namespace is independent of arriving data: an empty
        # first attempt, a symbol switch or irrelevant old rows cannot bypass
        # the frozen boundary. Use one namespace per instrument/session stream.
        key = canonical_fingerprint(self._namespace, closed_boundary(as_of), strategy, research_policy, quality_policy, pipeline_policy)
        if key in self._processed:
            return PipelineEmission(False, None, "closed_boundary_already_processed", self._processed[key])
        decision = run_pipeline(underlying_rows, contracts, quotes, as_of=as_of, strategy=strategy,
                                research_policy=research_policy, quality_policy=quality_policy, pipeline_policy=pipeline_policy,
                                conflict_events=conflict_events)
        self._processed[key] = decision.input_fingerprint
        return PipelineEmission(True, decision, "closed_boundary_processed_once")

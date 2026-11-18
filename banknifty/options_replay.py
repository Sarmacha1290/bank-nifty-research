"""Offline options replay; no broker, HTTP, credentials or order interfaces.

Quoted prices model possible manual execution, not actual fills. A valid bid/ask
is not a guarantee of available liquidity. Complete quote coverage is required
for scored P&L; a gap might conceal a stop. Costs and slippage must be supplied
explicitly. Underlying OHLC is only an approximate scenario diagnostic and never
supplies an option fill, Greek or option P&L.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import heapq
from typing import Callable, Iterable, Mapping

from .options_data import (OptionQuote, QualityPolicy, QuoteBook, validate_legs)
from .options_strategy import ResearchSignal, payoff_bounds, quote_filter_reasons
from .market_archive import ArchiveConflict


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be an aware datetime")


def _money(value: Decimal, name: str, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be a finite {'positive' if positive else 'non-negative'} Decimal")


def _seconds(value: int, name: str, *, positive: bool = False) -> None:
    if type(value) is not int or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be a {'positive' if positive else 'non-negative'} integer")


@dataclass(frozen=True)
class ReplayPolicy:
    """Operational assumptions, not recommended financial limits.

    ``fees_total`` covers all opening/closing legs for the supplied quantity.
    ``slippage_per_leg`` is adverse currency points on *each* leg and each side.
    A stop/target is an observed net-per-unit trigger, followed by manual latency;
    it is never a guaranteed loss cap. Missing costs/quantity prevent scoring.
    """

    manual_latency_seconds: int = 5
    max_entry_wait_seconds: int = 30
    max_exit_wait_seconds: int = 30
    horizon_seconds: int | None = None
    fees_total: Decimal | None = None
    slippage_per_leg: Decimal | None = None
    quantity: int | None = None
    stop_loss_per_unit: Decimal | None = None
    target_profit_per_unit: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("manual_latency_seconds", "max_entry_wait_seconds", "max_exit_wait_seconds"):
            _seconds(getattr(self, name), name)
        if self.horizon_seconds is not None:
            _seconds(self.horizon_seconds, "horizon_seconds", positive=True)
        if self.horizon_seconds is not None and self.manual_latency_seconds >= self.horizon_seconds:
            raise ValueError("Manual latency must be shorter than the research horizon")
        for name in ("fees_total", "slippage_per_leg"):
            if getattr(self, name) is not None:
                _money(getattr(self, name), name)
        for name in ("stop_loss_per_unit", "target_profit_per_unit"):
            if getattr(self, name) is not None:
                _money(getattr(self, name), name, positive=True)
        if self.quantity is not None and (type(self.quantity) is not int or self.quantity <= 0):
            raise ValueError("quantity must be positive matched option units")


@dataclass(frozen=True)
class SimulatedLeg:
    contract_id: str
    side: str
    price: Decimal
    source_at: datetime
    received_at: datetime
    quote_fingerprint: str


@dataclass(frozen=True)
class ReplayResult:
    status: str
    label: str
    signal_fingerprint: str
    entry_at: datetime | None = None
    exit_at: datetime | None = None
    exit_trigger_at: datetime | None = None
    exit_reason: str | None = None
    entries: tuple[SimulatedLeg, ...] = ()
    exits: tuple[SimulatedLeg, ...] = ()
    quantity: int | None = None
    entry_net_per_unit: Decimal | None = None
    exit_net_per_unit: Decimal | None = None
    fees_total: Decimal | None = None
    net_pnl: Decimal | None = None
    reasons: tuple[str, ...] = ()
    gaps: tuple[datetime, ...] = ()
    confidence: None = None
    actual_fills_created: int = 0
    policy: ReplayPolicy | None = None
    quality_policy: QualityPolicy | None = None
    evidence_kind: str = "unknown"
    consumed_evidence_kinds: tuple[str, ...] = ()
    source_continuity: bool = True


def replay_signal(
    signal: ResearchSignal, quote_events: Iterable[OptionQuote], *,
    policy: ReplayPolicy = ReplayPolicy(), quality_policy: QualityPolicy | None = None,
    cancelled: Callable[[], bool] | None = None,
    availability_times: Mapping[str, datetime] | None = None,
    conflict_events: Iterable[ArchiveConflict] = (),
) -> ReplayResult:
    """Replay a frozen hypothesis using future *observed option books*.

    The strategy is not re-fit or regenerated here. Every event is evaluated at
    its original receipt time, with strict source/receipt/provenance cutoffs.
    Opening and closing all legs requires fresh synchronized quotes received
    after the respective manual latency. This is a simultaneous-legs estimate,
    not a prediction of manual fill sequence. Missing size, stale books, paths
    with gaps, expired metadata, absent costs and unresolved exits stay unscored.
    Stops are monitored only at observations, then delayed by manual latency.
    An observed archive supplies ``availability_times`` keyed by immutable quote
    fingerprint. Events cannot appear before actual local first capture. Original
    source/receipt/provenance timestamps stay unchanged and are revalidated.
    Quarantined conflict facts arrive at their own publication time. A conflict
    affecting a consumed observation permanently prevents scoring that path;
    facts published after a resolved exit cannot rewrite the earlier result.
    """
    if not isinstance(signal, ResearchSignal) or not isinstance(policy, ReplayPolicy):
        raise TypeError("Expected a frozen ResearchSignal and ReplayPolicy")
    quality = replace(quality_policy or signal.quality_policy, max_future_skew_seconds=0, require_sizes=True)
    if signal.policy.minimum_open_interest is not None:
        quality = replace(quality, require_fresh_oi=True)
    synthetic_origin = any(leg.contract.provenance.evidence_kind == "synthetic"
                           or leg.quote.provenance.evidence_kind == "synthetic" for leg in signal.legs)
    base = dict(label="synthetic_option_quote_replay" if synthetic_origin else "offline_option_quote_replay",
                signal_fingerprint=signal.input_fingerprint,
                quantity=policy.quantity if policy.quantity is not None else signal.quantity,
                fees_total=policy.fees_total, policy=policy, quality_policy=quality)
    evidence = {leg.contract.provenance.evidence_kind for leg in signal.legs}
    evidence.update(leg.quote.provenance.evidence_kind for leg in signal.legs)
    origin_sources = {leg.contract.contract_id: leg.quote.provenance.source for leg in signal.legs}
    origin_kinds = {leg.contract.contract_id: leg.quote.provenance.evidence_kind for leg in signal.legs}
    source_continuity = True
    origin_continuity = True
    conflict_continuity = True

    def record_evidence(quote=None):
        nonlocal source_continuity
        if quote is not None:
            evidence.add(quote.provenance.evidence_kind)
            source_continuity = source_continuity and quote.provenance.source == origin_sources[quote.contract_id]
        kind = ("synthetic" if "synthetic" in evidence else "mixed_source" if not source_continuity
                else next(iter(evidence)) if len(evidence) == 1 else "mixed" if evidence else "unknown")
        base.update(evidence_kind=kind, consumed_evidence_kinds=tuple(sorted(evidence)),
                    source_continuity=source_continuity)
        if kind == "synthetic":
            base["label"] = "synthetic_option_quote_replay"

    record_evidence()

    def result(status: str, *reasons: str, **values) -> ReplayResult:
        continuity_reason = () if origin_continuity else ("Option source/evidence changed from the frozen origin; those updates were rejected.",)
        conflict_reason = () if conflict_continuity else ("Quarantined archive conflicts affect the consumed quote path; P&L is unavailable.",)
        return ReplayResult(status=status, reasons=tuple(reasons)+continuity_reason+conflict_reason, **(base | values))

    if cancelled is not None and cancelled():
        return result("cancelled", "Read-only replay cancelled; no actual fills were created.")
    if signal.status != "candidate" or not signal.legs:
        return result("unavailable", "Signal is no-trade; no execution scenario is created.")
    try:
        payoff_bounds(signal.legs)
    except (ValueError, TypeError):
        return result("unavailable", "Unsupported or uncovered structure; no execution scenario is created.")
    horizon = signal.horizon_minutes * 60
    if policy.horizon_seconds is not None and policy.horizon_seconds != horizon:
        return result("unavailable", "Replay horizon must equal the frozen signal horizon; create a new signal to change it.")
    if policy.manual_latency_seconds >= horizon:
        return result("unavailable", "Manual latency consumes the whole signal horizon.")
    quantity = base["quantity"]
    if quantity is None or policy.fees_total is None or policy.slippage_per_leg is None:
        return result("unavailable", "Explicit quantity, total round-trip fees and adverse per-leg slippage are required.")
    if any(quantity % leg.contract.lot_size != 0 for leg in signal.legs):
        return result("unavailable", "Quantity must be complete lots for every dated contract version.")
    contracts = tuple(leg.contract for leg in signal.legs)
    lookup = {item.contract_id: item for item in contracts}
    if len(lookup) != len(contracts):
        return result("unavailable", "Duplicate contract legs are unsupported.")
    as_of = signal.as_of
    _aware(as_of, "signal.as_of")
    origin_quality = validate_legs(tuple(leg.quote for leg in signal.legs), contracts, as_of, quality)
    if not origin_quality.accepted:
        return result("unavailable", "Frozen signal origin does not pass the strict quote-data gate.", *origin_quality.reasons)
    stop_loss = policy.stop_loss_per_unit
    target_profit = policy.target_profit_per_unit
    if stop_loss is None and signal.planned_stop_pnl_per_unit is not None:
        stop_loss = -signal.planned_stop_pnl_per_unit
    if target_profit is None:
        target_profit = signal.planned_target_pnl_per_unit
    earliest = as_of + timedelta(seconds=policy.manual_latency_seconds)
    horizon_at = as_of + timedelta(seconds=horizon)
    entry_deadline = min(earliest + timedelta(seconds=policy.max_entry_wait_seconds), horizon_at)
    final_deadline = horizon_at + timedelta(seconds=policy.manual_latency_seconds + policy.max_exit_wait_seconds)
    grouped: dict[datetime, list[OptionQuote]] = {}
    for count, quote in enumerate(quote_events):
        if cancelled is not None and cancelled():
            return result("cancelled", "Read-only replay cancelled; no actual fills were created.")
        if count >= 100_000:
            return result("unavailable", "Replay exceeds the bounded 100000-event limit; split the archive into sessions.")
        if not isinstance(quote, OptionQuote):
            return result("unavailable", "Only timestamped option quotes can model fills; underlying candles cannot.")
        _aware(quote.received_at, "quote.received_at")
        available = quote.received_at
        if quote.contract_id in lookup and availability_times is not None:
            if quote.fingerprint not in availability_times:
                return result("unavailable", "Actual first-observed availability is missing for an option quote.")
            first_seen = availability_times[quote.fingerprint]
            _aware(first_seen, "first observed availability")
            available = max(available, first_seen, quote.provenance.captured_at)
        if quote.contract_id in lookup and as_of <= available <= final_deadline:
            grouped.setdefault(available, []).append(quote)
    conflict_groups: dict[datetime, list[ArchiveConflict]] = {}
    for count, conflict in enumerate(conflict_events):
        if cancelled is not None and cancelled():
            return result("cancelled", "Read-only replay cancelled; no actual fills were created.")
        if count >= 100_000:
            return result("unavailable", "Replay exceeds the bounded 100000-conflict limit; split the archive into sessions.")
        if not isinstance(conflict, ArchiveConflict):
            return result("unavailable", "Replay conflict evidence must be a validated archive conflict fact.")
        if (conflict.kind in {"option", "catalogue"} and conflict.symbol in lookup
                and conflict.available_at <= final_deadline):
            conflict_groups.setdefault(max(as_of, conflict.available_at), []).append(conflict)
    pending = list(set(grouped) | set(conflict_groups) | {earliest, entry_deadline, horizon_at, final_deadline})
    heapq.heapify(pending)
    scheduled = set(pending)
    book = QuoteBook()
    # The frozen signal already consumed these observations. Seed their source,
    # timestamp and sequence high-water marks even when callers omit them from
    # the future stream, or their receipt predates the decision cutoff.
    for leg in signal.legs:
        if book.ingest(leg.quote, leg.contract, as_of, quality).status != "accepted":
            return result("unavailable", "Frozen signal origin cannot initialize quote continuity.")
    def quote_identity(quote):
        return (quote.contract_id, quote.provenance.source, quote.source_at, quote.sequence,
                quote.received_at if quote.source_at is None else None)

    def conflict_identity(conflict):
        return (conflict.symbol, conflict.source, conflict.source_at, conflict.sequence,
                conflict.received_at if conflict.source_at is None else None)

    # Index exact observations so old conflicts cannot contaminate unrelated
    # contracts, providers or source instants, and matching remains bounded.
    consumed = {}
    active_conflicts = {}
    for leg in signal.legs:
        consumed.setdefault(quote_identity(leg.quote), {})[leg.quote.provenance.evidence_kind] = leg.quote
    blocked = set()
    gaps: list[datetime] = []
    coverage_until = None
    entries: tuple[SimulatedLeg, ...] = ()
    entry_at = None
    entry_net = None
    trigger_at = None
    exit_due = None
    exit_reason = None

    def sides(rows: tuple[OptionQuote, ...], *, close: bool,
              require_execution: bool = True) -> tuple[SimulatedLeg, ...] | None:
        by_id = {q.contract_id: q for q in rows}
        computed = []
        for leg in signal.legs:
            quote = by_id[leg.contract_id]
            side = ("SELL" if leg.side == "BUY" else "BUY") if close else leg.side
            price = quote.ask + policy.slippage_per_leg if side == "BUY" else quote.bid - policy.slippage_per_leg
            tick = leg.contract.tick_size
            price = (price / tick).to_integral_value(rounding=ROUND_CEILING if side == "BUY" else ROUND_FLOOR) * tick
            size = quote.ask_size if side == "BUY" else quote.bid_size
            if require_execution and (price <= 0 or size is None or size < quantity):
                return None
            computed.append(SimulatedLeg(leg.contract_id, side, price, quote.source_at,
                                         quote.received_at, quote.fingerprint))
        return tuple(computed)

    def signed_value(rows: tuple[SimulatedLeg, ...], *, closing: bool = False) -> Decimal:
        return sum((row.price * (1 if (row.side == "SELL" if closing else row.side == "BUY") else -1)
                    for row in rows), Decimal(0))

    while pending:
        at = heapq.heappop(pending)
        if cancelled is not None and cancelled():
            return result("cancelled", "Read-only replay cancelled; no actual fills were created.",
                          entries=entries, entry_at=entry_at, entry_net_per_unit=entry_net, gaps=tuple(gaps))
        for conflict in conflict_groups.get(at, ()):
            if conflict.kind == "catalogue":
                if conflict.matches(lookup[conflict.symbol]):
                    conflict_continuity = False
                continue
            key = conflict_identity(conflict)
            active_conflicts.setdefault(key, {})[conflict.evidence_kind] = conflict
            if any(conflict.matches(q) for q in consumed.get(key, {}).values()):
                conflict_continuity = False
        rejected_here = set()
        for quote in sorted(grouped.get(at, ()), key=lambda q: (q.contract_id, q.fingerprint)):
            # Any consumed update can change path validity. Track rejected
            # evidence too, and enforce continuity directly from the frozen
            # origin even when quote_events omits the redundant origin quote.
            record_evidence(quote)
            key = quote_identity(quote)
            consumed.setdefault(key, {})[quote.provenance.evidence_kind] = quote
            if any(fact.matches(quote) for fact in active_conflicts.get(key, {}).values()):
                conflict_continuity = False
                rejected_here.add(quote.contract_id)
                continue
            if (quote.provenance.source != origin_sources[quote.contract_id]
                    or quote.provenance.evidence_kind != origin_kinds[quote.contract_id]):
                origin_continuity = False
                rejected_here.add(quote.contract_id)
                continue
            ingestion = book.ingest(quote, lookup[quote.contract_id], at, quality)
            if ingestion.status == "accepted":
                blocked.discard(quote.contract_id)
            elif ingestion.status in {"rejected", "out_of_order"}:
                rejected_here.add(quote.contract_id)
        # A contradictory update in the same receipt group invalidates the group.
        blocked.update(rejected_here)
        rows = tuple(book.latest(c.contract_id) for c in contracts)
        ready = (not blocked and all(q is not None for q in rows)
                 and validate_legs(rows, contracts, at, quality).accepted
                 and not any(fact.matches(q) for q in rows
                             for fact in active_conflicts.get(quote_identity(q), {}).values()))
        if not entries:
            if (earliest <= at <= entry_deadline and at < horizon_at and ready
                    and origin_continuity and conflict_continuity):
                if all(q.source_at >= earliest and q.received_at >= earliest for q in rows):
                    if any(quote_filter_reasons(q, lookup[q.contract_id], as_of=at,
                                               policy=signal.policy, quality_policy=quality) for q in rows):
                        continue
                    assumed = sides(rows, close=False)
                    if assumed is not None:
                        entries, entry_at = assumed, at
                        entry_net = signed_value(entries)
                        if any(q.provenance.evidence_kind == "synthetic" for q in rows):
                            base["label"] = "synthetic_option_quote_replay"
                        coverage_until = min(min(q.source_at + timedelta(seconds=quality.max_source_age_seconds),
                                                 q.received_at + timedelta(seconds=quality.max_receipt_age_seconds)) for q in rows)
                        continue
            if at >= entry_deadline:
                return result("unavailable", "No synchronized, liquid, post-latency option quotes before the entry deadline.")
            continue

        if not ready or (coverage_until is not None and at > coverage_until):
            if not gaps or gaps[-1] != at:
                gaps.append(at)
        if ready:
            coverage_until = min(min(q.source_at + timedelta(seconds=quality.max_source_age_seconds),
                                     q.received_at + timedelta(seconds=quality.max_receipt_age_seconds)) for q in rows)
        if trigger_at is None:
            if at >= horizon_at:
                trigger_at, exit_reason = horizon_at, "time"
            elif ready:
                # A quoted stop crossing is observable even if the entire
                # position cannot execute at that depth. Keep the trigger and
                # wait for an executable exit; liquidity must never hide a stop.
                closing = sides(rows, close=True, require_execution=False)
                if closing is not None:
                    marked_net = signed_value(closing, closing=True) - entry_net - policy.fees_total / quantity
                    if stop_loss is not None and marked_net <= -stop_loss:
                        trigger_at, exit_reason = at, "stop"
                    elif target_profit is not None and marked_net >= target_profit:
                        trigger_at, exit_reason = at, "target"
            if trigger_at is not None:
                exit_due = trigger_at + timedelta(seconds=policy.manual_latency_seconds)
                for timer in (exit_due, exit_due + timedelta(seconds=policy.max_exit_wait_seconds)):
                    if timer not in scheduled:
                        heapq.heappush(pending, timer)
                        scheduled.add(timer)
        if trigger_at is not None and at >= exit_due:
            if ready and all(q.source_at >= exit_due and q.received_at >= exit_due for q in rows):
                assumed = sides(rows, close=True)
                if assumed is not None:
                    exit_net = signed_value(assumed, closing=True)
                    pnl = (exit_net - entry_net) * quantity - policy.fees_total
                    unscored = bool(gaps) or not origin_continuity or not conflict_continuity
                    return result("incomplete" if unscored else "completed",
                                  "Simultaneous quote-side execution estimate; no actual or guaranteed fills.",
                                  "Stops are observed triggers plus manual latency, never guaranteed loss caps.",
                                  *(('Quote-path gaps may conceal a stop; P&L is unavailable.',) if gaps else ()),
                                  entries=entries, exits=assumed, entry_at=entry_at, exit_at=at,
                                  exit_trigger_at=trigger_at, exit_reason=exit_reason,
                                  entry_net_per_unit=entry_net, exit_net_per_unit=exit_net,
                                  net_pnl=None if unscored else pnl, gaps=tuple(gaps))
            if at >= exit_due + timedelta(seconds=policy.max_exit_wait_seconds):
                break
    return result("incomplete", "Position scenario has no fully quoted synchronized exit before the bounded deadline; P&L unavailable.",
                  entries=entries, entry_at=entry_at, entry_net_per_unit=entry_net,
                  exit_trigger_at=trigger_at, exit_reason=exit_reason, gaps=tuple(gaps))


@dataclass(frozen=True)
class OhlcBar:
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        _aware(self.start, "bar.start")
        _aware(self.end, "bar.end")
        if self.start >= self.end:
            raise ValueError("bar end must follow its start")
        for name in ("open", "high", "low", "close"):
            _money(getattr(self, name), f"bar.{name}", positive=True)
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("bar OHLC range is inconsistent")


@dataclass(frozen=True)
class CandleDiagnostic:
    label: str
    status: str
    exit_at: datetime | None
    exit_level: Decimal | None
    reasons: tuple[str, ...]
    ambiguous: bool = False
    option_fills: tuple = ()
    option_pnl: None = None


def candle_path_diagnostic(
    bars: Iterable[OhlcBar], *, signal_at: datetime, entry_level: Decimal,
    stop_level: Decimal, target_level: Decimal, side: str = "LONG",
    manual_latency_seconds: int = 5, horizon_seconds: int = 900,
) -> CandleDiagnostic:
    """Approximate underlying-only path, deliberately incapable of option fills.

    Bars overlapping the signal or latency cutoff are excluded in full. A bar
    that touches stop and target has unknown order: pessimistically count the
    stop first. A gap through a stop uses the worse opening level. Exit timestamps
    are bar ends (the actual intrabar moment is unknown), not invented ticks.
    """
    _aware(signal_at, "signal_at")
    for name, value in (("entry_level", entry_level), ("stop_level", stop_level), ("target_level", target_level)):
        _money(value, name, positive=True)
    _seconds(manual_latency_seconds, "manual_latency_seconds")
    _seconds(horizon_seconds, "horizon_seconds", positive=True)
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if side == "LONG" and not stop_level < entry_level < target_level:
        raise ValueError("LONG levels must satisfy stop < entry < target")
    if side == "SHORT" and not target_level < entry_level < stop_level:
        raise ValueError("SHORT levels must satisfy target < entry < stop")
    materialized = tuple(sorted(bars, key=lambda b: b.start))
    if any(a.end > b.start for a, b in zip(materialized, materialized[1:])):
        raise ValueError("overlapping bars cannot establish a unique chronology")
    earliest = signal_at + timedelta(seconds=manual_latency_seconds)
    deadline = signal_at + timedelta(seconds=horizon_seconds)
    eligible = tuple(b for b in materialized if b.start >= earliest and b.end <= deadline)
    caveat = ("Underlying-only OHLC is approximate; no option fills or option P&L are inferred.",
              "Intrabar execution time and manual reaction cannot be resolved from OHLC.")
    last = None
    covered_until = earliest
    for bar in eligible:
        if bar.start > covered_until:
            return CandleDiagnostic("approximate_underlying_only", "unavailable", None, None,
                                    caveat + ("A missing underlying-bar interval may conceal an earlier stop; path unavailable.",))
        last = bar
        covered_until = bar.end
        stopped = bar.low <= stop_level if side == "LONG" else bar.high >= stop_level
        targeted = bar.high >= target_level if side == "LONG" else bar.low <= target_level
        if stopped:
            level = min(stop_level, bar.open) if side == "LONG" else max(stop_level, bar.open)
            why = "Both stop and target occur in one OHLC bar: pessimistic stop-first policy." if targeted else "Underlying stop level touched; opening gaps use the worse opening level."
            return CandleDiagnostic("approximate_underlying_only", "stop", bar.end, level,
                                    caveat + (why,), targeted)
        if targeted:
            return CandleDiagnostic("approximate_underlying_only", "target", bar.end, target_level,
                                    caveat + ("Underlying target level touched; no favourable opening-gap improvement assumed.",))
    if last is not None and last.end == deadline:
        return CandleDiagnostic("approximate_underlying_only", "time", last.end, last.close, caveat)
    return CandleDiagnostic("approximate_underlying_only", "unavailable", None, None,
                            caveat + ("No full post-latency path to an exit or exact horizon is available.",))


@dataclass(frozen=True)
class EvaluationFold:
    name: str
    train_dates: tuple[date, ...]
    test_dates: tuple[date, ...]
    train_origins: tuple[datetime, ...]
    test_origins: tuple[datetime, ...]
    embargo_seconds: int
    holdout: bool
    notes: tuple[str, ...]


def chronological_folds(
    decision_times: Iterable[datetime], *, horizon_seconds: int,
    min_train_sessions: int = 3, test_sessions: int = 1,
    holdout_sessions: int = 1, embargo_seconds: int | None = None,
    manual_latency_seconds: int = 5, max_exit_wait_seconds: int = 30,
    label_end_times: Mapping[datetime, datetime] | None = None,
) -> tuple[EvaluationFold, ...]:
    """Expanding whole-date walk-forward folds plus one untouched final holdout.

    Caller freezes the strategy before evaluation. This helper performs no
    fitting/selection and returns no calibrated confidence. Training origins
    whose actual label end can overlap the test cutoff are purged. Without an
    actual end, assume horizon + exit latency + the entire allowed exit wait.
    All dates use the input
    timestamps' local calendar; mixed UTC offsets are rejected to prevent a
    caller accidentally splitting one session across different calendars.
    """
    _seconds(horizon_seconds, "horizon_seconds", positive=True)
    _seconds(manual_latency_seconds, "manual_latency_seconds")
    _seconds(max_exit_wait_seconds, "max_exit_wait_seconds")
    label_window = horizon_seconds + manual_latency_seconds + max_exit_wait_seconds
    for name, value in (("min_train_sessions", min_train_sessions), ("test_sessions", test_sessions), ("holdout_sessions", holdout_sessions)):
        _seconds(value, name, positive=True)
    embargo = label_window if embargo_seconds is None else embargo_seconds
    _seconds(embargo, "embargo_seconds", positive=True)
    if embargo < label_window:
        raise ValueError("embargo must cover horizon plus exit latency and maximum exit wait")
    materialized = tuple(decision_times)
    for item in materialized:
        _aware(item, "decision time")
    actual_ends = dict(label_end_times or {})
    for origin, end in actual_ends.items():
        _aware(origin, "label origin")
        _aware(end, "actual label end")
        if end < origin:
            raise ValueError("Actual label end cannot precede its decision")
    if len({t.utcoffset() for t in materialized}) > 1:
        raise ValueError("decision timestamps must use one session-calendar UTC offset")
    origins = tuple(sorted(set(materialized)))
    dates = tuple(sorted({t.date() for t in origins}))
    if len(dates) < min_train_sessions + test_sessions + holdout_sessions:
        return ()
    development_end = len(dates) - holdout_sessions
    result = []

    def fold(name: str, split: int, stop: int, holdout: bool) -> EvaluationFold:
        testing_dates = dates[split:stop]
        testing = tuple(t for t in origins if t.date() in testing_dates)
        cutoff = testing[0] - timedelta(seconds=embargo)
        training = tuple(t for t in origins if t.date() in dates[:split]
                         and t < cutoff
                         and actual_ends.get(t, t + timedelta(seconds=label_window)) < testing[0])
        return EvaluationFold(name, tuple(sorted({t.date() for t in training})), testing_dates,
                              training, testing, embargo, holdout,
                              ("Strategy/policy must be frozen before held-out evaluation.",
                               "No optimization or calibration is performed by this split helper.",
                               "Purged training labels include delayed exits and cannot overlap the test interval."))

    for start in range(min_train_sessions, development_end, test_sessions):
        stop = min(start + test_sessions, development_end)
        result.append(fold(f"walk_forward_{len(result) + 1}", start, stop, False))
    result.append(fold("untouched_holdout", development_end, len(dates), True))
    return tuple(result)


def summarize_replays(results: Iterable[ReplayResult]) -> dict:
    """Coverage-first descriptive summary; incomplete outcomes are never zeroes."""
    records = tuple(results)
    scored = tuple(r for r in records if r.status == "completed" and r.net_pnl is not None)
    return {"label": "offline_replay_estimates", "attempted": len(records),
            "scored": len(scored), "unavailable": len(records) - len(scored),
            "net_pnl": str(sum((r.net_pnl for r in scored), Decimal(0))) if scored else None,
            "wins": sum(r.net_pnl > 0 for r in scored),
            "losses": sum(r.net_pnl < 0 for r in scored),
            "confidence": None, "validated_accuracy": None,
            "actual_fills_created": 0,
            "limitations": ["Quoted execution is an estimate, never a broker fill.",
                            "Incomplete quote paths are excluded and counted as unavailable.",
                            "Supplied costs, slippage, quantity and latency are assumptions."]}

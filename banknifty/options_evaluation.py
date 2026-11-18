"""Frozen, offline, coverage-first evaluation of the shared options pipeline.

No optimizer, broker, credential or order interface belongs here. A numerical
calibration estimate describes completed quote replay under the supplied costs;
it is never proof of live profitability or a calibrated trading recommendation.
Synthetic data cannot produce a fitted probability model.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .options_data import QualityPolicy
from .options_replay import ReplayPolicy, replay_signal
from .options_strategy import ResearchPolicy

def _session_zone(name):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Kolkata":
            # The supported modern NSE calendar uses fixed +05:30; Windows
            # may lack the optional IANA tzdata database.
            return timezone(timedelta(hours=5, minutes=30), "IST")
        raise



VERSION = "options-evaluation-v1"
_BIN_EDGES = (Decimal("0"), Decimal("0.25"), Decimal("0.5"), Decimal("0.75"), Decimal("1"))


def _jsonable(value):
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return {"sha256": sha256(value).hexdigest(), "bytes": len(value)}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _hash(value):
    return sha256(json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _aware(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be an aware datetime")


def _positive_int(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _stamp(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    _aware(result, "timestamp")
    return result


def _policy_from(cls, raw, decimal_names=(), timestamp_names=()):
    if not isinstance(raw, dict) or set(raw) - {field.name for field in fields(cls)}:
        raise ValueError(f"Unsupported {cls.__name__} fields")
    converted = dict(raw)
    for key in decimal_names:
        if converted.get(key) is not None:
            if isinstance(converted[key], bool):
                raise ValueError("Boolean monetary values are not accepted")
            converted[key] = Decimal(str(converted[key]))
    for key in timestamp_names:
        if converted.get(key) is not None:
            converted[key] = _stamp(converted[key])
    return cls(**converted)


@dataclass(frozen=True)
class FrozenExperimentConfig:
    name: str
    start_at: datetime
    end_at: datetime
    strategy: str = "LONG_OPTION"
    archive_mode: str = "reconstructed"
    research_policy: ResearchPolicy = ResearchPolicy()
    replay_policy: ReplayPolicy = ReplayPolicy()
    quality_policy: QualityPolicy = QualityPolicy()
    pipeline_policy: Any = None
    decision_interval_minutes: int = 15
    min_train_sessions: int = 3
    validation_sessions: int = 1
    holdout_sessions: int = 1
    walkforward_test_sessions: int = 1
    embargo_seconds: int | None = None
    min_calibration_samples: int = 100
    min_bin_samples: int = 20
    min_calibration_days: int = 20
    min_class_samples: int = 10
    allow_unverified_calibration: bool = False
    max_decisions: int = 2000
    session_timezone: str = "Asia/Kolkata"
    expected_archive_fingerprint: str | None = None
    expected_session_intervals: tuple[tuple[datetime, datetime], ...] | None = None

    def __post_init__(self):
        from .options_pipeline import PipelinePolicy
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 120 or any(ord(c) < 32 for c in self.name):
            raise ValueError("Use a bounded experiment name without control characters")
        for name in ("start_at", "end_at"):
            _aware(getattr(self, name), name)
        if self.end_at <= self.start_at:
            raise ValueError("Experiment end must follow start")
        if self.strategy not in {"LONG_OPTION", "DEBIT_SPREAD", "CREDIT_SPREAD"}:
            raise ValueError("Unsupported options strategy")
        if self.archive_mode not in {"observed", "reconstructed"}:
            raise ValueError("Archive mode must be observed or reconstructed")
        if not isinstance(self.research_policy, ResearchPolicy) or not isinstance(self.replay_policy, ReplayPolicy) or not isinstance(self.quality_policy, QualityPolicy):
            raise ValueError("Supply typed frozen research, replay and quality policies")
        if self.pipeline_policy is None:
            object.__setattr__(self, "pipeline_policy", PipelinePolicy())
        elif not isinstance(self.pipeline_policy, PipelinePolicy):
            raise ValueError("Supply a typed PipelinePolicy")
        for name in ("decision_interval_minutes", "min_train_sessions", "validation_sessions", "holdout_sessions",
                     "walkforward_test_sessions", "min_calibration_samples", "min_bin_samples",
                     "min_calibration_days", "min_class_samples", "max_decisions"):
            _positive_int(getattr(self, name), name)
        if self.max_decisions > 20_000:
            raise ValueError("The bounded runner supports at most 20000 decision cutoffs")
        if type(self.allow_unverified_calibration) is not bool:
            raise ValueError("allow_unverified_calibration must be boolean")
        if self.embargo_seconds is not None:
            _positive_int(self.embargo_seconds, "embargo_seconds")
            if self.embargo_seconds < self.outcome_window_seconds:
                raise ValueError("Embargo must cover the horizon plus exit latency and maximum exit wait")
        if self.replay_policy.horizon_seconds not in (None, self.research_policy.horizon_minutes * 60):
            raise ValueError("Replay and research horizons must agree")
        calendar = _session_zone(self.session_timezone)
        if self.expected_session_intervals is not None:
            intervals = self.expected_session_intervals
            if not isinstance(intervals, tuple) or not intervals:
                raise ValueError("Expected session intervals must be a nonempty immutable tuple")
            prior_end = None
            for pair in intervals:
                if not isinstance(pair, tuple) or len(pair) != 2:
                    raise ValueError("Expected session intervals must be (first cutoff, last cutoff) pairs")
                start, end = pair
                _aware(start, "session first cutoff")
                _aware(end, "session last cutoff")
                if (start > end or start < self.start_at or end > self.end_at
                        or any(t.second or t.microsecond for t in pair)
                        or start.astimezone(calendar).date() != end.astimezone(calendar).date()
                        or (prior_end is not None and start <= prior_end)):
                    raise ValueError("Expected session intervals must be sorted, disjoint minute cutoffs inside the experiment and one session day")
                prior_end = end
        digest = self.expected_archive_fingerprint
        if digest is not None and (not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("Expected archive fingerprint must be a lowercase SHA-256")

    @property
    def outcome_window_seconds(self):
        return (self.research_policy.horizon_minutes * 60 + self.replay_policy.manual_latency_seconds
                + self.replay_policy.max_exit_wait_seconds)

    @property
    def effective_embargo_seconds(self):
        return self.embargo_seconds or self.outcome_window_seconds

    @property
    def fingerprint(self):
        return _hash({"version": VERSION, "config": self})

    def to_dict(self):
        return _jsonable(self)

    @classmethod
    def from_dict(cls, raw):
        from .options_pipeline import PipelinePolicy
        if not isinstance(raw, dict) or set(raw) - {field.name for field in fields(cls)}:
            raise ValueError("Unsupported experiment configuration fields")
        values = dict(raw)
        for key in ("start_at", "end_at"):
            if key not in values:
                raise ValueError(f"Missing experiment {key}")
            values[key] = _stamp(values[key])
        if "research_policy" in values:
            values["research_policy"] = _policy_from(ResearchPolicy, values["research_policy"],
                ("max_spread_pct", "capital", "max_risk_fraction", "total_fees", "margin_per_lot",
                 "stop_loss_fraction", "take_profit_fraction", "spread_width"), ("expiry_at",))
        if "replay_policy" in values:
            values["replay_policy"] = _policy_from(ReplayPolicy, values["replay_policy"],
                ("fees_total", "slippage_per_leg", "stop_loss_per_unit", "target_profit_per_unit"))
        if "quality_policy" in values:
            values["quality_policy"] = _policy_from(QualityPolicy, values["quality_policy"])
        if values.get("pipeline_policy") is not None:
            values["pipeline_policy"] = _policy_from(PipelinePolicy, values["pipeline_policy"])
        if values.get("expected_session_intervals") is not None:
            raw_intervals = values["expected_session_intervals"]
            if not isinstance(raw_intervals, (list, tuple)) or any(not isinstance(p, (list, tuple)) or len(p) != 2 for p in raw_intervals):
                raise ValueError("Expected session intervals must contain timestamp pairs")
            values["expected_session_intervals"] = tuple(tuple(_stamp(t) for t in pair) for pair in raw_intervals)
        return cls(**values)


@dataclass(frozen=True)
class EvaluationRow:
    decision_at: datetime
    session_date: date
    model: str
    status: str
    evidence_kind: str
    input_fingerprint: str
    signal_id: str | None = None
    strength: Decimal | None = None
    replay_status: str | None = None
    net_pnl: Decimal | None = None
    positive_net_outcome: bool | None = None
    reasons: tuple[str, ...] = ()
    label_available_at: datetime | None = None
    data_gate_passed: bool = True


@dataclass(frozen=True)
class CalibrationBin:
    lower: Decimal
    upper: Decimal
    samples: int
    positive: int
    sessions: int
    probability: Decimal | None


@dataclass(frozen=True)
class CalibrationModel:
    status: str
    target: str
    bins: tuple[CalibrationBin, ...]
    samples: int
    sessions: int
    base_rate: Decimal | None
    training_fingerprint: str
    last_training_decision: datetime | None
    evidence_kind: str
    reasons: tuple[str, ...]
    validated: bool = False

    def predict(self, strength):
        if not isinstance(strength, Decimal) or not strength.is_finite() or not Decimal(0) <= strength <= Decimal(1):
            return None
        if self.status not in {"experimental_verified_observations", "experimental_unverified"}:
            return None
        for index, cell in enumerate(self.bins):
            if cell.lower <= strength < cell.upper or (index == len(self.bins) - 1 and strength == cell.upper):
                return cell.probability
        return None

    def to_dict(self):
        return _jsonable(self)


def fit_calibration(rows: Iterable[EvaluationRow], *, min_samples=100, min_bin_samples=20,
                    min_days=20, min_class_samples=10, allow_unverified=False) -> CalibrationModel:
    """Fit fixed empirical bins only on caller-supplied training observations.

    The score is a heuristic strength feature, not an input probability. Only
    complete, costed option replays with known outcomes count. Minimum sample
    and day counts are configurable refusal rules, not a certification standard.
    No synthetic, mixed, receipt-only or unknown-origin corpus is fit.
    """
    for name, value in (("min_samples", min_samples), ("min_bin_samples", min_bin_samples),
                        ("min_days", min_days), ("min_class_samples", min_class_samples)):
        _positive_int(value, name)
    if type(allow_unverified) is not bool:
        raise ValueError("allow_unverified must be boolean")
    source = tuple(rows)
    eligible = tuple(r for r in source if r.model == "pipeline" and r.status == "candidate"
                   and r.replay_status == "completed" and type(r.positive_net_outcome) is bool
                   and r.net_pnl is not None and r.strength is not None
                   and isinstance(r.strength, Decimal) and r.strength.is_finite()
                   and Decimal(0) <= r.strength <= Decimal(1))
    unique = {}
    for row in eligible:
        _aware(row.decision_at, "calibration decision")
        if not isinstance(row.net_pnl, Decimal) or not row.net_pnl.is_finite() or (row.net_pnl > 0) != row.positive_net_outcome:
            raise ValueError("Calibration label must match a finite completed net P&L")
        if row.decision_at in unique and unique[row.decision_at] != row:
            raise ValueError("Conflicting calibration records for one frozen decision cutoff")
        unique[row.decision_at] = row
    usable = tuple(unique.values())
    evidence = {r.evidence_kind for r in usable}
    kind = next(iter(evidence)) if len(evidence) == 1 else "mixed" if evidence else "unknown"
    days = len({r.session_date for r in usable})
    wins = sum(r.positive_net_outcome for r in usable)
    reasons = []
    if evidence - {"live_verified", "replay_quote"}:
        reasons.append("Synthetic, mixed or unknown evidence cannot support calibration.")
    if len(evidence) > 1:
        reasons.append("Use one evidence class in a calibration corpus.")
    if kind == "replay_quote" and not allow_unverified:
        reasons.append("Unverified historical chronology requires explicit research-only calibration opt-in.")
    if len(usable) < min_samples:
        reasons.append("Insufficient complete costed training outcomes.")
    if days < min_days:
        reasons.append("Insufficient distinct training sessions; repeated ticks are not independent days.")
    if wins < min_class_samples or len(usable) - wins < min_class_samples:
        reasons.append("Both positive and non-positive training outcomes need the minimum count.")
    bins = []
    for index, (lower, upper) in enumerate(zip(_BIN_EDGES, _BIN_EDGES[1:])):
        selected = tuple(r for r in usable if lower <= r.strength < upper or (index == len(_BIN_EDGES) - 2 and r.strength == upper))
        positive = sum(r.positive_net_outcome for r in selected)
        bin_days = len({r.session_date for r in selected})
        probability = Decimal(positive) / len(selected) if not reasons and len(selected) >= min_bin_samples and bin_days >= min_days else None
        bins.append(CalibrationBin(lower, upper, len(selected), positive, bin_days, probability))
    status = "unavailable" if reasons else "experimental_verified_observations" if kind == "live_verified" else "experimental_unverified"
    if not reasons and not any(cell.probability is not None for cell in bins):
        status = "unavailable"
        reasons.append("Every fixed strength bin has too few training samples.")
    reasons.append("No tuning on test outcomes; empirical bins do not establish future calibration or independent observations.")
    return CalibrationModel(status, "positive_net_quote_replay_pnl", tuple(bins), len(usable), days,
                            Decimal(wins) / len(usable) if status != "unavailable" else None,
                            _hash(tuple(sorted(usable, key=lambda r: (r.decision_at, r.input_fingerprint)))),
                            max((r.decision_at for r in usable), default=None), kind, tuple(reasons))


def calibration_metrics(model: CalibrationModel, rows: Iterable[EvaluationRow], *, sample_role="out_of_sample") -> dict:
    """Score frozen probabilities and identify fitting diagnostics explicitly."""
    if sample_role not in {"in_sample", "out_of_sample"}:
        raise ValueError("Calibration sample role must be in_sample or out_of_sample")
    attempted, predictions = 0, []
    for row in rows:
        if row.model != "pipeline" or row.status != "candidate":
            continue
        attempted += 1
        if (sample_role == "out_of_sample" and model.last_training_decision is not None
                and row.decision_at <= model.last_training_decision):
            continue
        probability = model.predict(row.strength)
        if (probability is not None and row.evidence_kind == model.evidence_kind
                and row.replay_status == "completed" and type(row.positive_net_outcome) is bool):
            predictions.append((probability, Decimal(int(row.positive_net_outcome))))
    cells = []
    for index, (lower, upper) in enumerate(zip(_BIN_EDGES, _BIN_EDGES[1:])):
        selected = tuple((p, y) for p, y in predictions if lower <= p < upper or (index == len(_BIN_EDGES) - 2 and p == upper))
        cells.append({"lower": str(lower), "upper": str(upper), "count": len(selected),
                      "mean_predicted": str(sum((p for p, _ in selected), Decimal(0)) / len(selected)) if selected else None,
                      "observed_positive_rate": str(sum((y for _, y in selected), Decimal(0)) / len(selected)) if selected else None})
    brier = sum(((p-y)**2 for p, y in predictions), Decimal(0)) / len(predictions) if predictions else None
    base_brier = (sum(((model.base_rate-y)**2 for _, y in predictions), Decimal(0)) / len(predictions)
                  if predictions and model.base_rate is not None else None)
    return {"candidate_count": attempted, "predicted_and_labelled": len(predictions),
            "unavailable": attempted - len(predictions), "brier_score": str(brier) if brier is not None else None,
            "training_base_rate_brier": str(base_brier) if base_brier is not None else None,
            "reliability_bins": cells, "validated": False, "sample_role": sample_role,
            "note": ("In-sample fitting diagnostics; these are not held-out scores." if sample_role == "in_sample" else
                     "Descriptive out-of-sample scores; dependent sessions and repeated holdout use limit interpretation.")}


def summarize_rows(rows: Iterable[EvaluationRow], *, period_coverage_known=False) -> dict:
    rows = tuple(rows)
    candidates = tuple(r for r in rows if r.status == "candidate")
    complete = tuple(r for r in candidates if r.replay_status == "completed" and r.net_pnl is not None)
    missing = len(candidates) - len(complete)
    unavailable_data = sum(not r.data_gate_passed for r in rows)
    total = sum((r.net_pnl for r in complete), Decimal(0))
    days = []
    for day in sorted({r.session_date for r in rows}):
        day_rows = tuple(r for r in rows if r.session_date == day)
        day_candidates = tuple(r for r in day_rows if r.status == "candidate")
        day_complete = tuple(r for r in day_candidates if r.replay_status == "completed" and r.net_pnl is not None)
        day_missing = len(day_candidates) - len(day_complete)
        day_unavailable = sum(not r.data_gate_passed for r in day_rows)
        day_net = sum((r.net_pnl for r in day_complete), Decimal(0))
        days.append({"date": day.isoformat(), "decisions": len(day_rows), "candidates": len(day_candidates),
                     "scored": len(day_complete), "missing_outcomes": day_missing, "data_unavailable_decisions": day_unavailable,
                     "scored_net_pnl": str(day_net) if day_complete else None,
                     "complete_day_net_pnl": str(day_net) if period_coverage_known and not day_missing and not day_unavailable else None})
    complete_days = tuple(Decimal(row["complete_day_net_pnl"]) for row in days if row["complete_day_net_pnl"] is not None)
    return {"decisions": len(rows), "candidates": len(candidates), "no_trade": len(rows)-len(candidates),
            "scored": len(complete), "missing_outcomes": missing, "data_unavailable_decisions": unavailable_data,
            "expected_period_coverage_known": period_coverage_known,
            "trade_coverage": len(candidates)/len(rows) if rows else None,
            "outcome_coverage": len(complete)/len(candidates) if candidates else None,
            "scored_net_pnl": str(total) if complete else None,
            "complete_period_net_pnl": str(total) if rows and period_coverage_known and not missing and not unavailable_data else None,
            "positive_outcomes": sum(r.net_pnl > 0 for r in complete),
            "nonpositive_outcomes": sum(r.net_pnl <= 0 for r in complete),
            "descriptive_positive_rate": sum(r.net_pnl > 0 for r in complete)/len(complete) if complete else None,
            "complete_session_count": len(complete_days),
            "mean_complete_day_pnl": str(sum(complete_days)/len(complete_days)) if complete_days else None,
            "per_day": days, "independent_sample_count": None,
            "confidence": None, "validated_accuracy": None}


def _comparisons(rows):
    keyed = {(r.model, r.decision_at): r for r in rows}
    model_rows = tuple(r for r in rows if r.model == "pipeline" and r.replay_status == "completed" and r.net_pnl is not None)
    comparisons = {}
    for baseline in ("always_bullish", "always_bearish"):
        pairs = tuple((r, keyed.get((baseline, r.decision_at))) for r in model_rows)
        matched = tuple((a, b) for a, b in pairs if b is not None and b.replay_status == "completed" and b.net_pnl is not None)
        delta = sum((a.net_pnl-b.net_pnl for a, b in matched), Decimal(0))
        comparisons[baseline] = {"matched_decisions": len(matched), "unmatched_pipeline_scored": len(pairs)-len(matched),
                                 "matched_pnl_difference": str(delta) if matched else None,
                                 "note": "Only both-complete decisions compared; each uses the same policy and its own candidate sizing."}
    comparisons["no_trade_cash"] = {"scored_decisions": len(model_rows), "cash_pnl": "0",
                                     "pipeline_scored_pnl": str(sum((r.net_pnl for r in model_rows), Decimal(0))) if model_rows else None,
                                     "note": "Zero-position benchmark; missing strategy outcomes remain unavailable, never zero."}
    return comparisons


@dataclass(frozen=True)
class EvaluationReport:
    status: str
    experiment_fingerprint: str
    archive_fingerprint: str
    config: FrozenExperimentConfig
    rows: tuple[EvaluationRow, ...]
    folds: tuple[dict, ...]
    summary: dict
    reasons: tuple[str, ...]
    schema_version: int = 1
    version: str = VERSION
    validated: bool = False
    actual_fills_created: int = 0

    def to_dict(self):
        return _jsonable(self)


def _archive_reader(archive):
    if isinstance(archive, (str, Path)):
        from .market_archive import MarketArchive
        return MarketArchive(archive, read_only=True)
    if getattr(archive, "read_only", None) is not True or not callable(getattr(archive, "query", None)):
        raise ValueError("Supply an archive path or a read-only MarketArchive")
    return archive


def _corpus(reader, config):
    return reader.query(config.end_at + timedelta(seconds=config.outcome_window_seconds),
                        mode=config.archive_mode, start_at=config.start_at-timedelta(minutes=10))


def freeze_experiment(archive, config: FrozenExperimentConfig) -> FrozenExperimentConfig:
    """Bind explicit settings to a fixed semantic corpus without modifying it."""
    snapshot = _corpus(_archive_reader(archive), config)
    if config.expected_archive_fingerprint is not None and config.expected_archive_fingerprint != snapshot.fingerprint:
        raise ValueError("Frozen experiment archive fingerprint changed; create a new experiment explicitly")
    return replace(config, expected_archive_fingerprint=snapshot.fingerprint)


def _decision_schedule(snapshot, config):
    calendar = _session_zone(config.session_timezone)
    boundaries = {}
    for row in snapshot.underlying:
        # Unknown source time still produces a scheduled abstention, never a
        # reconstructed exchange timestamp or an eligible directional input.
        stamp = row.source_at or row.received_at
        boundary = stamp.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = boundary.astimezone(calendar).date()
        if boundary <= config.end_at:
            boundaries.setdefault(day, []).append(boundary)
    anchor = config.start_at.astimezone(timezone.utc).replace(second=0, microsecond=0)
    if anchor < config.start_at:
        anchor += timedelta(minutes=1)
    interval = config.decision_interval_minutes * 60
    schedule = []
    intervals = (config.expected_session_intervals if config.expected_session_intervals is not None else
                 tuple((max(min(boundaries[day]), anchor), min(max(boundaries[day]), config.end_at)) for day in sorted(boundaries)))
    for start, end in intervals:
        elapsed = max(0, int((start-anchor).total_seconds()))
        offset = (elapsed+interval-1)//interval
        at = anchor + timedelta(seconds=offset*interval)
        while at <= end:
            schedule.append(at)
            if len(schedule) > config.max_decisions:
                raise ValueError("Experiment exceeds max_decisions; reduce the date range or increase the interval")
            at += timedelta(seconds=interval)
    return tuple(sorted(set(schedule)))


def _evidence_kind(decision, signal, mode):
    observations = tuple(decision.observed_inputs)
    if any(row.source_at is None for row in observations):
        return "receipt_only"
    kinds = {row.provenance.evidence_kind for row in observations}
    if signal is not None:
        kinds.update(leg.contract.provenance.evidence_kind for leg in signal.legs)
        kinds.update(leg.quote.provenance.evidence_kind for leg in signal.legs)
    if "synthetic" in kinds:
        return "synthetic"
    if len(kinds) != 1:
        return "mixed" if kinds else "unknown"
    kind = next(iter(kinds))
    if kind == "historical_candle":
        return "candle_approximate"
    return "replay_quote" if mode == "reconstructed" else kind


def _outcome_evidence_kind(origin_kind, outcome, mode):
    """A future outcome can weaken origin evidence, never upgrade it."""
    outcome_kind = outcome.evidence_kind
    if "synthetic" in {origin_kind, outcome_kind}:
        return "synthetic"
    if not outcome.source_continuity:
        return "mixed_source"
    if outcome_kind in {"mixed", "mixed_source", "unknown", "historical_candle"}:
        return outcome_kind
    if mode == "reconstructed":
        outcome_kind = "replay_quote"
    return origin_kind if origin_kind == outcome_kind else "mixed"


def _partition_report(rows, calibration, *, sample_role="out_of_sample", period_coverage_known=False):
    rows = tuple(rows)
    return {"models": {model: summarize_rows((r for r in rows if r.model == model), period_coverage_known=period_coverage_known)
                       for model in ("pipeline", "always_bullish", "always_bearish")},
            "calibration": calibration_metrics(calibration, rows, sample_role=sample_role),
            "comparisons": _comparisons(rows)}


def evaluation_folds(rows: Iterable[EvaluationRow], config: FrozenExperimentConfig) -> tuple[dict, ...]:
    """Fit bins on training only; validation and all test labels stay untouched."""
    rows = tuple(rows)
    dates = tuple(sorted({row.session_date for row in rows}))
    first_test = config.min_train_sessions + config.validation_sessions
    development_end = len(dates)-config.holdout_sessions
    if development_end < first_test:
        return ()
    specs = [(f"walk_forward_{index+1}", start, min(start+config.walkforward_test_sessions, development_end), False)
             for index, start in enumerate(range(first_test, development_end, config.walkforward_test_sessions))]
    specs.append(("untouched_holdout", development_end, len(dates), True))
    result = []
    for name, split, end, holdout in specs:
        training_dates = dates[:split-config.validation_sessions]
        validation_dates = dates[split-config.validation_sessions:split]
        test_dates = dates[split:end]
        test = tuple(r for r in rows if r.session_date in test_dates)
        validation = tuple(r for r in rows if r.session_date in validation_dates)
        training = tuple(r for r in rows if r.session_date in training_dates)
        test_start = min(r.decision_at for r in test)
        validation_start = min(r.decision_at for r in validation)

        def purged(partition, next_start):
            return tuple(r for r in partition
                         if r.decision_at + timedelta(seconds=config.effective_embargo_seconds) < next_start
                         and (r.label_available_at or (r.decision_at+timedelta(seconds=config.outcome_window_seconds))) < next_start)

        train_used = purged(training, validation_start)
        validation_used = purged(validation, test_start)
        calibration = fit_calibration(train_used, min_samples=config.min_calibration_samples,
            min_bin_samples=config.min_bin_samples, min_days=config.min_calibration_days,
            min_class_samples=config.min_class_samples, allow_unverified=config.allow_unverified_calibration)
        result.append({"name": name, "holdout": holdout, "train_dates": [d.isoformat() for d in training_dates],
            "validation_dates": [d.isoformat() for d in validation_dates], "test_dates": [d.isoformat() for d in test_dates],
            "embargo_seconds": config.effective_embargo_seconds,
            "training_purged_decisions": len({r.decision_at for r in training})-len({r.decision_at for r in train_used}),
            "validation_purged_decisions": len({r.decision_at for r in validation})-len({r.decision_at for r in validation_used}),
            "training": _partition_report(train_used, calibration, sample_role="in_sample",
                period_coverage_known=config.expected_session_intervals is not None and len(train_used) == len(training)),
            "validation": _partition_report(validation_used, calibration,
                period_coverage_known=config.expected_session_intervals is not None and len(validation_used) == len(validation)),
            "test": _partition_report(test, calibration, period_coverage_known=config.expected_session_intervals is not None),
            "calibration_model": calibration.to_dict(),
            "test_labels_used_for_fitting": False, "validation_used_for_parameter_selection": False,
            "parameters_optimized": False,
            "notes": ["Fixed signal rule and fixed strength bins; only training label counts fit probabilities.",
                      "Execution horizon includes exit latency/wait; actual later label availability is also purged.",
                      "No exchange-session authenticity, live execution, independent samples or profitability is established."]})
    return tuple(result)


def evaluate_archive(archive, config: FrozenExperimentConfig, *, cancelled: Callable[[], bool] | None = None) -> EvaluationReport:
    """Read an immutable corpus and run the exact shared closed-bar pipeline.

    Every decision queries its own archive cutoff. Replay outcomes are separate
    future reads and never feed earlier decisions. The semantic corpus is hashed
    again before return to reject concurrent historical additions or changes.
    """
    from .options_pipeline import run_pipeline, generate_baseline
    if not isinstance(config, FrozenExperimentConfig):
        raise TypeError("Expected FrozenExperimentConfig")
    reader = _archive_reader(archive)
    corpus = _corpus(reader, config)
    if config.expected_archive_fingerprint is not None and config.expected_archive_fingerprint != corpus.fingerprint:
        raise ValueError("Frozen experiment archive fingerprint changed; create a new experiment explicitly")
    frozen = replace(config, expected_archive_fingerprint=corpus.fingerprint)
    calendar = _session_zone(frozen.session_timezone)
    rows = []
    assumptions = {"archive_label": corpus.label, "mode": frozen.archive_mode,
        "research_policy": _jsonable(frozen.research_policy), "replay_policy": _jsonable(frozen.replay_policy),
        "quality_policy": _jsonable(frozen.quality_policy), "pipeline_policy": _jsonable(frozen.pipeline_policy),
        "margin_separate_from_max_loss": True, "actual_fills_created": 0,
        "calibration_target": "positive_net_quote_replay_pnl", "session_timezone": frozen.session_timezone}
    caveats = ("All executions are quote-side scenarios; fills, taxes, slippage and liquidity are not broker-verified.",
        "Underlying-only data never supplies option fills; missing outcomes are excluded and counted.",
        "Repeated/overlapping signals and sessions are dependent; sample count is not independent sample size.",
        "Aggregate P&L sums hypothetical scenarios, not a funded portfolio; no cross-signal capital or margin reservation is modeled.",
        "Fixed rules and unvalidated empirical calibration do not establish predictive accuracy or profitability.",
        "Repeated experiments against the same holdout compromise its independence; freeze a new future test period.")

    def report(status, reasons, folds=()):
        expected_known = frozen.expected_session_intervals is not None
        fully_scheduled = expected_known and len({r.decision_at for r in rows}) == len(schedule)
        summary = {"assumptions": assumptions, "models": {model: summarize_rows((r for r in rows if r.model == model), period_coverage_known=fully_scheduled)
            for model in ("pipeline", "always_bullish", "always_bearish")}, "comparisons": _comparisons(rows),
            "schedule_coverage": {"mode": "explicit_expected_sessions" if expected_known else "observed_spans_only",
                "expected_decision_count": len(schedule) if expected_known else None,
                "evaluated_decision_count": len({r.decision_at for r in rows}),
                "expected_period_coverage_available": expected_known,
                "note": ("User-supplied decision windows; exchange holidays/session authenticity are not inferred." if expected_known else
                         "Only spans of observed underlying data were scheduled. Missing whole sessions and leading/trailing windows are unknown; requested-period coverage and complete-period P&L are unavailable.")},
            "evidence_kinds": sorted({r.evidence_kind for r in rows}),
            "manifest_ids": list(corpus.manifest_ids), "calibrated_confidence": None,
            "independent_sample_count": None, "validated": False}
        return EvaluationReport(status, frozen.fingerprint, corpus.fingerprint, frozen, tuple(rows), tuple(folds), summary, tuple(reasons)+caveats)

    schedule = _decision_schedule(corpus, frozen)
    if not schedule:
        return report("unavailable_no_underlying", ("No timestamped underlying decision schedule exists in the requested range.",))
    for cutoff in schedule:
        if cancelled is not None and cancelled():
            return report("cancelled", ("Read-only evaluation cancelled; partial diagnostics must not be treated as a completed experiment.",))
        snapshot = reader.query(cutoff, mode=frozen.archive_mode, start_at=cutoff-timedelta(minutes=10))
        decision = run_pipeline(snapshot.underlying, snapshot.contracts, snapshot.quotes,
            as_of=cutoff, strategy=frozen.strategy, research_policy=frozen.research_policy,
            quality_policy=frozen.quality_policy, pipeline_policy=frozen.pipeline_policy,
            conflict_events=getattr(snapshot, "conflicts", ()))
        candidates = {"pipeline": decision.signal}
        for model, direction in (("always_bullish", "BULLISH"), ("always_bearish", "BEARISH")):
            candidates[model] = generate_baseline(decision, decision.option_contracts, decision.option_quotes,
                direction=direction, strategy=frozen.strategy, research_policy=frozen.research_policy,
                quality_policy=frozen.quality_policy)
        outcome_limit = cutoff+timedelta(seconds=frozen.outcome_window_seconds)
        outcome_quotes = tuple(q for q in corpus.quotes if cutoff <= q.received_at <= outcome_limit)
        for model, signal in candidates.items():
            kind = _evidence_kind(decision, signal, frozen.archive_mode)
            strength = decision.assessment.strength if decision.assessment is not None and model == "pipeline" else None
            if signal is None or signal.status != "candidate":
                rows.append(EvaluationRow(cutoff, cutoff.astimezone(calendar).date(), model, "no_trade", kind,
                    decision.input_fingerprint if signal is None else signal.input_fingerprint,
                    signal.signal_id if signal is not None else None, strength,
                    reasons=decision.reasons if signal is None else signal.reasons,
                    data_gate_passed=(model == "pipeline" and decision.data_gate_passed
                                      and signal is not None and signal.direction == "NEUTRAL")))
                continue
            availability = None
            if frozen.archive_mode == "observed":
                availability = getattr(corpus, "availability_by_fingerprint", {})
            outcome = replay_signal(signal, outcome_quotes, policy=frozen.replay_policy,
                                    quality_policy=frozen.quality_policy, cancelled=cancelled,
                                    availability_times=availability,
                                    conflict_events=getattr(corpus, "conflicts", ()))
            if outcome.status == "cancelled":
                return report("cancelled", ("Read-only evaluation cancelled during replay.",))
            pnl = outcome.net_pnl if outcome.status == "completed" else None
            kind = _outcome_evidence_kind(kind, outcome, frozen.archive_mode)
            rows.append(EvaluationRow(cutoff, cutoff.astimezone(calendar).date(), model, "candidate", kind,
                signal.input_fingerprint, signal.signal_id, strength, outcome.status, pnl,
                pnl > 0 if pnl is not None else None, tuple(outcome.reasons),
                outcome.exit_at if pnl is not None else outcome_limit))
    if _corpus(reader, frozen).fingerprint != corpus.fingerprint:
        raise ValueError("Archive changed during evaluation; retry using a frozen corpus")
    folds = evaluation_folds(rows, frozen)
    return report("completed_research" if folds else "insufficient_sessions",
                  () if folds else ("Not enough whole sessions for the frozen train/validation/holdout split.",), folds)


def main(argv=None):
    """Offline CLI: explicit archive/config paths and a new report destination."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, help="Existing immutable market archive SQLite path")
    parser.add_argument("--config", required=True, help="FrozenExperimentConfig JSON")
    parser.add_argument("--output", required=True, help="New JSON evaluation report path")
    parser.add_argument("--frozen-config", help="Optional new path for the exact corpus-bound configuration")
    args = parser.parse_args(argv)
    try:
        source = Path(args.config)
        if source.stat().st_size > 128_000:
            raise ValueError("Experiment configuration exceeds the 128 KB limit")
        document = json.loads(source.read_text(encoding="utf-8"))
        config = FrozenExperimentConfig.from_dict(document)
        targets = [Path(args.output)] + ([Path(args.frozen_config)] if args.frozen_config else [])
        if any(path.exists() for path in targets) or len({path.resolve() for path in targets}) != len(targets):
            raise ValueError("Choose distinct new output paths; prior experiment artifacts are never overwritten")
        outcome = evaluate_archive(args.archive, config)
        with targets[0].open("x", encoding="utf-8") as stream:
            json.dump(outcome.to_dict(), stream, sort_keys=True, indent=2, allow_nan=False)
        if args.frozen_config:
            with targets[1].open("x", encoding="utf-8") as stream:
                json.dump(outcome.config.to_dict(), stream, sort_keys=True, indent=2, allow_nan=False)
        print(json.dumps({"status": outcome.status, "experiment_fingerprint": outcome.experiment_fingerprint,
                          "report": str(targets[0]), "validated": False, "actual_fills_created": 0}))
        return 0
    except (OSError, ValueError, InvalidOperation) as error:
        parser.exit(2, f"Evaluation unavailable: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())

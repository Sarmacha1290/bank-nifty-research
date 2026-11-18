"""Final research gate for matching specialist records; no forecast model yet."""
from dataclasses import dataclass
from datetime import datetime

from ..feed_quality import SAMPLED_SOURCE
from .bull_bear import DirectionCase
from .luna import FeatureSnapshot, HORIZONS, LunaAssessment
from .risk import HorizonRisk, RiskResult
from .verification import VerificationResult


FORMULA_VERSION = "master-research-gate-v0.1"


@dataclass(frozen=True)
class HorizonReview:
    horizon_minutes: int
    status: str
    technical_bias: str
    bull_observations: int
    bear_observations: int
    conflicting_past_evidence: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MasterReview:
    as_of: datetime
    symbol: str
    source: str
    horizons: tuple[HorizonReview, ...]
    forecast_available: bool = False
    trading_allowed: bool = False
    formula_version: str = FORMULA_VERSION


def _by_horizon(rows, expected_type, label):
    items = tuple(rows)
    if (len(items) != len(HORIZONS) or
            any(not isinstance(row, expected_type) for row in items) or
            {row.horizon_minutes for row in items} != set(HORIZONS)):
        raise ValueError(f"{label} needs one valid record for each horizon")
    return {row.horizon_minutes: row for row in items}


def review_bundle(snapshot, assessments, bull_cases, bear_cases, verification, risk):
    """Assemble an inspectable, blocked-by-default per-horizon research review."""
    if not isinstance(snapshot, FeatureSnapshot):
        raise TypeError("A validated feature snapshot is required")
    if not isinstance(verification, VerificationResult) or not isinstance(risk, RiskResult):
        raise TypeError("Verification and Risk results are required")
    technical = _by_horizon(assessments, LunaAssessment, "Technical")
    bull = _by_horizon(bull_cases, DirectionCase, "Bull")
    bear = _by_horizon(bear_cases, DirectionCase, "Bear")
    decisions = _by_horizon(risk.horizons, HorizonRisk, "Risk")
    if (verification.as_of, verification.symbol, verification.source) != (
            snapshot.as_of, snapshot.symbol, snapshot.source):
        raise ValueError("Verification does not describe this snapshot")
    result = []
    for horizon in HORIZONS:
        tech, up, down, gate = (rows[horizon] for rows in
                                (technical, bull, bear, decisions))
        if (tech.as_of, tech.symbol, tech.source) != (
                snapshot.as_of, snapshot.symbol, snapshot.source):
            raise ValueError("Technical evidence cutoff or identity mismatch")
        for case, agent in ((up, "Bull"), (down, "Bear")):
            if (case.agent, case.as_of, case.symbol, case.source) != (
                    agent, snapshot.as_of, snapshot.symbol, snapshot.source):
                raise ValueError(f"{agent} evidence cutoff or identity mismatch")
        if gate.paper_eligible and gate.status != "paper_eligible":
            raise ValueError("Risk decision conflicts with its status")
        if not gate.paper_eligible and gate.status != "blocked":
            raise ValueError("Risk decision conflicts with its status")
        reasons = []
        if not verification.passed:
            reasons.append("Structural verification failed")
        if snapshot.source != SAMPLED_SOURCE:
            reasons.append("No verified sampled-feed source")
        if not gate.paper_eligible:
            reasons.extend(gate.reasons or ("Risk blocked paper research",))
        if tech.bias == "unavailable" or up.status != "descriptive" or down.status != "descriptive":
            reasons.append("Specialist evidence unavailable")
        eligible = not reasons
        result.append(HorizonReview(horizon,
            "paper_research_only" if eligible else "blocked",
            tech.bias, len(up.supporting_observations),
            len(down.supporting_observations),
            bool(up.supporting_observations and down.supporting_observations),
            tuple(dict.fromkeys(reasons)) if reasons else
            ("No calibrated forecasting model; research record only",)))
    return MasterReview(snapshot.as_of, snapshot.symbol, snapshot.source, tuple(result))

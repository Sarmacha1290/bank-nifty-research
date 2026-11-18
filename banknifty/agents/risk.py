"""Conservative paper-publication gate; no trade sizing or order authority."""

from dataclasses import dataclass

from ..core import Session, prepare_windows
from ..feed_quality import SAMPLED_SOURCE
from ..groww import IST
from ..market_calendar import regular_session
from .luna import FeatureSnapshot, LunaAssessment
from .verification import VerificationResult


@dataclass(frozen=True)
class HorizonRisk:
    horizon_minutes: int
    paper_eligible: bool
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RiskResult:
    horizons: tuple[HorizonRisk, ...]
    public_forecast_allowed: bool = False
    trading_allowed: bool = False


@dataclass(frozen=True)
class OptionsResearchRisk:
    research_visible: bool
    reasons: tuple[str, ...]
    public_forecast_allowed: bool = False
    live_signal_available: bool = False
    trading_allowed: bool = False


def check_options_research(signal):
    """Keep the options research branch separate from a forecast release.

    Valid quote structure and contractual payoff arithmetic do not establish
    predictive skill. This adapter cannot authorize an execution or turn a
    research label into a calibrated probability.
    """
    from ..options_strategy import ResearchSignal
    if not isinstance(signal, ResearchSignal):
        raise TypeError("Expected an immutable options research signal")
    return OptionsResearchRisk(signal.status == "candidate", tuple(signal.reasons) + (
        "Live option data validation pending",
        "Held-out real option performance and calibration unavailable",
        "Research display only; no execution interface",
    ))


def check_risk(snapshot, assessments, verification, session):
    """Permit only fixed experimental logging with validated input lineage."""
    if not isinstance(snapshot, FeatureSnapshot) or not isinstance(verification, VerificationResult):
        raise TypeError("Expected a feature snapshot and verification result")
    if not isinstance(session, Session):
        raise TypeError("session must be a Session")
    rows = list(assessments)
    if any(not isinstance(item, LunaAssessment) for item in rows):
        raise TypeError("Expected Technical Agent assessments")
    windows = {row["horizon_minutes"]: row for row in
               prepare_windows(snapshot.as_of, session, (item.horizon_minutes for item in rows))}
    official_session = None
    if snapshot.source == SAMPLED_SOURCE:
        try:
            official_session = regular_session(snapshot.as_of.astimezone(IST).date())
        except ValueError:
            pass
    trusted_session = (official_session is not None and session.open == official_session.open
                       and session.close == official_session.close)
    results = []
    for item in rows:
        reasons = []
        if not verification.passed:
            reasons.append("Verification failed")
        if snapshot.source != SAMPLED_SOURCE:
            reasons.append("No verified live Groww sampled-feed source")
        if not trusted_session:
            reasons.append("Regular NSE session not verified")
        if snapshot.data_freshness_seconds > 120:
            reasons.append("Input older than 120 seconds")
        if windows[item.horizon_minutes]["status"] != "ready":
            reasons.append(f"Horizon {windows[item.horizon_minutes]['status']}")
        if item.bias == "unavailable" or item.score is None:
            reasons.append("Technical assessment unavailable")
        if item.generated_at < snapshot.as_of:
            reasons.append("Assessment timing invalid")
        eligible = not reasons
        results.append(HorizonRisk(item.horizon_minutes, eligible,
                                   "paper_eligible" if eligible else "blocked", tuple(reasons)))
    return RiskResult(tuple(results))

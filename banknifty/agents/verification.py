"""Independent structural checks for one point-in-time evidence bundle.

This verifies internal consistency. It cannot certify Groww's exchange data or
the predictive validity of a score.
"""

from dataclasses import dataclass
from datetime import datetime

from ..core import Session, validate_series
from .luna import FeatureSnapshot, HORIZONS, LunaAssessment


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    issues: tuple[str, ...]
    limitations: tuple[str, ...]
    as_of: datetime | None
    symbol: str | None
    source: str | None


def verify_bundle(candles, snapshot, assessments, session):
    """Check continuity, cutoff, provenance and assessment identity."""
    issues = []
    limitations = ["Provider authenticity and historical predictive skill are not established by these checks."]
    if not isinstance(snapshot, FeatureSnapshot):
        raise TypeError("snapshot must be a FeatureSnapshot")
    if not isinstance(session, Session):
        raise TypeError("session must be a Session")
    values = list(candles)
    rows = list(assessments)
    try:
        checked = validate_series(values, snapshot.as_of, expected_minutes=1)
    except ValueError as exc:
        issues.append(f"Candle series: {exc}")
        checked = []
    if len(checked) < 61:
        issues.append("Fewer than 61 continuous completed minutes")
    if checked:
        first, last = checked[0], checked[-1]
        if first.start < session.open or last.end > session.close:
            issues.append("Candles extend outside the supplied session")
        if last.end != snapshot.as_of:
            issues.append("Snapshot cutoff does not match last completed candle")
        if (first.symbol, first.source, first.kind) != (snapshot.symbol, snapshot.source, "index"):
            issues.append("Snapshot instrument or source does not match candles")
        if last.close != snapshot.current_price:
            issues.append("Snapshot current price does not match last close")
    if len(rows) != 3 or {item.horizon_minutes for item in rows if isinstance(item, LunaAssessment)} != set(HORIZONS):
        issues.append("Expected one assessment for each 15/30/60-minute horizon")
    if any(not isinstance(item, LunaAssessment) for item in rows):
        issues.append("Assessment has an unexpected type")
    for item in rows:
        if not isinstance(item, LunaAssessment):
            continue
        if (item.as_of, item.symbol, item.source) != (snapshot.as_of, snapshot.symbol, snapshot.source):
            issues.append(f"{item.horizon_minutes}-minute assessment provenance mismatch")
        if item.confidence is not None:
            issues.append(f"{item.horizon_minutes}-minute assessment has unvalidated confidence")
        if (item.score is None) != (item.bias == "unavailable"):
            issues.append(f"{item.horizon_minutes}-minute score and availability conflict")
        if item.generated_at < snapshot.as_of:
            issues.append(f"{item.horizon_minutes}-minute assessment predates its input cutoff")
    if snapshot.source == "synthetic":
        limitations.append("Inputs are invented demonstration prices.")
    elif "sampled" in snapshot.source.lower():
        limitations.append("Sampled feed bars are approximations, not official exchange OHLC candles.")
    return VerificationResult(not issues, tuple(issues), tuple(limitations),
                              snapshot.as_of, snapshot.symbol, snapshot.source)

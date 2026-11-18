"""Point-in-time assessment attempt when a new feed minute begins."""

from datetime import timedelta

from .agents.risk import check_risk
from .agents.verification import verify_bundle
from .feed_technical import build_feed_evidence
from .market_calendar import regular_session
from .paper_log import record_paper_assessments
from .tick_history import load_index_ticks


def attempt_boundary_assessment(database, boundary, generated_at):
    """Fix eligible experimental assessments using only earlier saved updates.

    Returns the number newly logged. Insufficient coverage, freshness, or an
    ineligible session yields zero; unexpected storage failures propagate to
    the collector's separate paper-error count without stopping raw capture.
    """
    prior = load_index_ticks(database, start=boundary - timedelta(minutes=61), end=boundary)
    try:
        _, candles, snapshot, assessments = build_feed_evidence(prior, generated_at=generated_at)
    except ValueError:
        return 0
    if not assessments or assessments[0].as_of != boundary:
        return 0
    session = regular_session(boundary.date())
    verification = verify_bundle(candles, snapshot, assessments, session)
    risk = check_risk(snapshot, assessments, verification, session)
    eligible = [item for item, gate in zip(assessments, risk.horizons) if gate.paper_eligible]
    return record_paper_assessments(database, eligible, recorded_at=generated_at,
                                    evidence_ticks=prior)["inserted"]

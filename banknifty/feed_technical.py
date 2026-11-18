"""Observed-feed Technical Agent entry point, never a calibrated forecast."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .agents.luna import assess
from .core import prepare_windows
from .feed import IndexTick
from .feed_quality import (MAX_RESEARCH_ENDPOINT_AGE_SECONDS,
                           accepted_close_age_by_end, inspect_sampled_minutes)
from .market_calendar import regular_session
from .groww import IST
from .technical_features import build_technical_snapshot


def build_feed_evidence(ticks, *, generated_at=None):
    """Return quality, the exact input slice, snapshot and rule assessments.

    The observed bars are approximations based on feed updates, not official
    exchange OHLC candles.
    """
    generated_at = generated_at or datetime.now(timezone.utc)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    timely = []
    for tick in ticks:
        if not isinstance(tick, IndexTick) or tick.exchange_at.tzinfo is None or tick.received_at.tzinfo is None:
            raise ValueError("Expected timezone-aware index updates")
        local = tick.exchange_at.astimezone(IST)
        minute_end = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
        if (tick.received_at < tick.exchange_at or tick.received_at > generated_at or
                tick.received_at > minute_end + timedelta(seconds=5)):
            continue
        timely.append(tick)
    quality, accepted = inspect_sampled_minutes(timely)
    if len(accepted) < 61:
        raise ValueError(f"Need 61 accepted sampled minutes; only {len(accepted)} available")
    latest = accepted[-61:]
    if latest[0].start.astimezone(latest[-1].end.tzinfo).date() != latest[-1].end.date():
        raise ValueError("Sampled minutes cross a session date")
    as_of = latest[-1].end
    close_age = accepted_close_age_by_end(quality).get(as_of.astimezone(timezone.utc))
    if close_age is None or close_age > MAX_RESEARCH_ENDPOINT_AGE_SECONDS:
        raise ValueError("Latest sampled close is too far from its minute boundary")
    if (generated_at - as_of).total_seconds() > 120:
        raise ValueError("Latest accepted sampled minute is older than 120 seconds")
    snapshot = build_technical_snapshot(latest, as_of, generated_at=generated_at)
    session = regular_session(as_of.date())
    windows = prepare_windows(as_of, session)
    results = []
    for window in windows:
        item = assess(snapshot, window["horizon_minutes"], generated_at=generated_at)
        if window["status"] != "ready":
            item = replace(item, bias="unavailable", score=None, reasons=(),
                           risks=(f"Horizon unavailable: {window['status']}",),
                           invalidation_conditions=())
        results.append(item)
    return quality, latest, snapshot, results


def assess_recorded_feed(ticks, *, generated_at=None):
    """Interpret 61 recent continuous high-coverage sampled minutes."""
    quality, _, _, results = build_feed_evidence(ticks, generated_at=generated_at)
    return quality, results

"""Quality-gated *sampled* minute bars from exchange-timestamped index updates.

These bars describe only updates observed by this recorder. Their high and low
need not equal the exchange's official one-minute high and low. They must never
be silently relabelled as historical provider candles.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite

from .core import Candle
from .feed import IndexTick
from .groww import IST
from .market_calendar import regular_market_status


SAMPLED_SOURCE = "Groww index feed sampled updates"
MAX_RESEARCH_ENDPOINT_AGE_SECONDS = 5.0
MIN_SAMPLES_PER_ACCEPTED_MINUTE = 12


def accepted_close_age_by_end(quality):
    """Seconds from each accepted minute boundary back to its last actual update."""
    return {item.end.astimezone(timezone.utc): 60.0 - item.last_offset_seconds
            for item in quality if item.status == "accepted"}


@dataclass(frozen=True)
class MinuteQuality:
    start: datetime
    end: datetime
    sample_count: int
    first_offset_seconds: float | None
    last_offset_seconds: float | None
    largest_gap_seconds: float | None
    status: str


def inspect_sampled_minutes(ticks, *, min_samples=MIN_SAMPLES_PER_ACCEPTED_MINUTE, edge_seconds=10,
                            max_gap_seconds=15):
    """Return quality rows and accepted sampled bars for observed session minutes.

    Only minutes with at least 12 distinct exchange timestamps, an update in
    each 10-second edge, and no internal gap over 15 seconds are accepted by
    default. Missing whole minutes remain visible as gaps in later continuity
    checks; they are not filled or interpolated.
    """
    if not isinstance(min_samples, int) or isinstance(min_samples, bool) or min_samples < 2:
        raise ValueError("min_samples must be an integer of at least 2")
    if not 0 < edge_seconds < 30 or not 0 < max_gap_seconds < 60:
        raise ValueError("Invalid minute coverage thresholds")
    ordered = sorted(ticks, key=lambda item: item.exchange_at)
    groups = {}
    seen = set()
    for tick in ordered:
        if not isinstance(tick, IndexTick) or tick.source != "Groww index feed":
            raise ValueError("Expected validated Groww index updates")
        if tick.exchange_at.tzinfo is None or tick.received_at.tzinfo is None:
            raise ValueError("Index update has no timezone")
        if not isfinite(tick.value) or tick.value <= 0:
            raise ValueError("Index update has an invalid value")
        local = tick.exchange_at.astimezone(IST)
        start = local.replace(second=0, microsecond=0)
        if regular_market_status(local) != "regular_session":
            continue
        identity = (tick.symbol, tick.exchange_at.astimezone(timezone.utc))
        if identity in seen:
            raise ValueError("Duplicate exchange timestamp in input")
        seen.add(identity)
        groups.setdefault((tick.symbol, start), []).append(tick)
    quality, accepted = [], []
    for (symbol, start), group in sorted(groups.items(), key=lambda item: item[0][1]):
        end = start + timedelta(minutes=1)
        offsets = [(tick.exchange_at.astimezone(IST) - start).total_seconds() for tick in group]
        gaps = [right - left for left, right in zip(offsets, offsets[1:])]
        largest = max(gaps, default=0.0)
        status = "accepted"
        if len(group) < min_samples:
            status = "too_few_samples"
        elif offsets[0] > edge_seconds or 60 - offsets[-1] > edge_seconds:
            status = "missing_minute_edge"
        elif largest > max_gap_seconds:
            status = "internal_gap"
        quality.append(MinuteQuality(start, end, len(group), offsets[0], offsets[-1], largest, status))
        if status == "accepted":
            values = [tick.value for tick in group]
            accepted.append(Candle(symbol, "index", start, end, values[0], max(values),
                                   min(values), values[-1], SAMPLED_SOURCE))
    return quality, accepted


def missing_minutes_between_observations(quality):
    """Count entirely unobserved minutes between first and last recorded minute of each date."""
    starts = sorted({row.start.astimezone(IST) for row in quality})
    total = 0
    for left, right in zip(starts, starts[1:]):
        if left.date() == right.date():
            total += max(0, int((right - left).total_seconds() // 60) - 1)
    return total

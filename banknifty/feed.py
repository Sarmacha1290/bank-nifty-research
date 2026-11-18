"""Validate timestamped Groww index-feed values before local recording.

This records individual updates, not candles. A missing or stale exchange time
is never replaced with the laptop's receipt time.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from numbers import Real


@dataclass(frozen=True)
class IndexTick:
    symbol: str
    exchange_at: datetime
    received_at: datetime
    value: float
    source: str = "Groww index feed"


def parse_index_tick(payload, instrument, received_at, *, max_age_seconds=120):
    """Return one fresh Bank Nifty update, or None when the feed has no value."""
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("received_at must include a timezone")
    if not isinstance(max_age_seconds, (int, float)) or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    exchange = instrument["exchange"]
    segment = instrument["segment"]
    exchange_token = instrument["exchange_token"]
    try:
        row = payload[exchange][segment][exchange_token]
    except (TypeError, KeyError):
        return None
    if row is None:
        # The reviewed Groww SDK includes subscribed topics before the first
        # socket message and represents their current data as None.
        return None
    if not isinstance(row, dict):
        raise ValueError("Unexpected index feed value")
    milliseconds, value = row.get("tsInMillis"), row.get("value")
    if (isinstance(milliseconds, bool) or not isinstance(milliseconds, Real) or
            not isfinite(milliseconds) or milliseconds <= 0):
        raise ValueError("Index feed has no valid exchange timestamp")
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value) or value <= 0:
        raise ValueError("Index feed has no valid index value")
    try:
        exchange_at = datetime.fromtimestamp(milliseconds / 1000.0, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError("Index feed timestamp is out of range") from None
    received_utc = received_at.astimezone(timezone.utc)
    age = (received_utc - exchange_at).total_seconds()
    if age < -5:
        raise ValueError("Index feed timestamp is unexpectedly in the future")
    if age > max_age_seconds:
        raise ValueError("Index feed update is stale")
    return IndexTick(instrument["groww_symbol"], exchange_at, received_utc, float(value))

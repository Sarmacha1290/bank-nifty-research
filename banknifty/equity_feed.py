"""Validate Groww equity LTP feed updates with exchange timestamps.

Values are raw last-traded prices. They are not synchronized index observations,
official candles, or an Index Contribution result.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from numbers import Real


@dataclass(frozen=True)
class EquityTick:
    symbol: str
    exchange_at: datetime
    received_at: datetime
    ltp: float
    source: str = "Groww equity LTP feed"


def parse_equity_ticks(payload, instruments, received_at, *, max_age_seconds=120):
    """Return (valid ticks, invalid-row count); missing feed rows stay missing."""
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("received_at must include a timezone")
    if not isinstance(max_age_seconds, (int, float)) or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    try:
        # GrowwFeed.get_ltp() removes the top-level "ltp" topic key before
        # returning the exchange/segment/token map (reviewed SDK 1.5.0).
        rows = payload["NSE"]["CASH"]
    except (TypeError, KeyError):
        return (), 0
    if not isinstance(rows, dict):
        return (), len(instruments)
    received_utc = received_at.astimezone(timezone.utc)
    ticks, rejected = [], 0
    for instrument in instruments:
        row = rows.get(instrument["exchange_token"])
        if row is None:
            continue
        if not isinstance(row, dict):
            rejected += 1
            continue
        milliseconds, price = row.get("tsInMillis"), row.get("ltp")
        if (isinstance(milliseconds, bool) or not isinstance(milliseconds, Real) or
                not isfinite(milliseconds) or milliseconds <= 0 or
                isinstance(price, bool) or not isinstance(price, Real) or
                not isfinite(price) or price <= 0):
            rejected += 1
            continue
        try:
            exchange_at = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
        except (OverflowError, OSError, ValueError):
            rejected += 1
            continue
        age = (received_utc - exchange_at).total_seconds()
        if age < -5 or age > max_age_seconds:
            rejected += 1
            continue
        ticks.append(EquityTick(instrument["groww_symbol"], exchange_at,
                                received_utc, float(price)))
    return tuple(ticks), rejected

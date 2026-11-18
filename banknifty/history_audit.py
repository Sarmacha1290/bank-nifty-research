"""Audit Groww's raw one-minute index history before assigning candle boundaries.

Groww's documented response does not specify whether a timestamp marks the
start or end of a candle, or state the timezone of its naive strings. This
module examines coverage under both interpretations but never normalizes or
archives the prices as usable forecast inputs.
"""

from datetime import date, datetime, time, timedelta
from math import isfinite
from numbers import Real


def audit_session(response, session_date):
    """Return a credential-free coverage report for a regular NSE session.

    The caller must independently establish that this date had a regular
    09:15-15:30 IST session. Special sessions and holidays are not inferred.
    """
    if not isinstance(session_date, date) or isinstance(session_date, datetime):
        raise ValueError("session_date must be a date")
    if not isinstance(response, dict) or response.get("status") != "SUCCESS":
        raise ValueError("Groww did not return a successful response")
    payload = response.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
        raise ValueError("Groww candle payload is missing")
    if payload["candles"] and payload.get("interval_in_minutes") != 1:
        raise ValueError("Groww did not return one-minute candles")

    stamps = []
    for row in payload["candles"]:
        if not isinstance(row, list) or len(row) not in (5, 6, 7):
            raise ValueError("Groww candle row has an unexpected shape")
        prices = row[1:5]
        if any(isinstance(value, bool) or not isinstance(value, Real) or
               not isfinite(value) or value <= 0 for value in prices):
            raise ValueError("Groww candle has invalid prices")
        opening, high, low, closing = prices
        if not low <= min(opening, closing) <= max(opening, closing) <= high:
            raise ValueError("Groww candle has inconsistent prices")
        try:
            stamp = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Groww candle timestamp cannot be parsed") from None
        if stamp.tzinfo is not None or stamp.date() != session_date or stamp.second or stamp.microsecond:
            raise ValueError("Unexpected date, timezone or seconds in Groww candle timestamp")
        stamps.append(stamp)

    if len(stamps) != len(set(stamps)):
        raise ValueError("Groww returned duplicate candle timestamps")
    stamps.sort()
    open_at = datetime.combine(session_date, time(9, 15))
    close_at = datetime.combine(session_date, time(15, 30))
    start_grid = {open_at + timedelta(minutes=i) for i in range(375)}
    end_grid = {open_at + timedelta(minutes=i) for i in range(1, 376)}
    observed = set(stamps)
    on_grid = {stamp for stamp in observed if open_at <= stamp <= close_at}
    gaps = sum(b - a != timedelta(minutes=1) for a, b in zip(stamps, stamps[1:]))
    missing_start = len(start_grid - observed)
    missing_end = len(end_grid - observed)
    outside = len(observed - on_grid)
    if not stamps:
        status = "no_data"
    elif outside or gaps or (missing_start and missing_end):
        status = "incomplete_or_irregular"
    elif len(observed) > 375:
        status = "boundary_ambiguous"
    else:
        status = "coverage_observed_convention_unverified"

    return {
        "session_date": session_date.isoformat(),
        "session_assumption": "regular NSE 09:15-15:30 Asia/Kolkata; caller must verify calendar",
        "provider_timestamp_timezone": "unverified; response strings contain no offset",
        "provider_timestamp_basis": "unverified; start/end boundary not documented",
        "status": status,
        "returned_rows": len(stamps),
        "expected_one_minute_intervals": 375,
        "first_timestamp": stamps[0].isoformat() if stamps else None,
        "last_timestamp": stamps[-1].isoformat() if stamps else None,
        "internal_gaps": gaps,
        "timestamps_outside_session": outside,
        "missing_if_start_stamped": missing_start,
        "missing_if_end_stamped": missing_end,
        "ready_for_forecast_archive": False,
    }

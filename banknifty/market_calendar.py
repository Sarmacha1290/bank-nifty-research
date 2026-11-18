"""Conservative NSE capital-market regular-session calendar for 2026.

Source: NSE/CMTR/71775 (12 Dec 2025), NSE/CMTR/72260 (12 Jan 2026),
and NSE's equities holiday/timing page, reviewed 4 Oct 2026. Exchange
circulars may change this calendar. Special sessions are not represented.
Unknown years fail closed until independently reviewed.
"""

from datetime import date, datetime, time

from .core import Session
from .groww import IST


SOURCES = (
    "https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf",
    "https://nsearchives.nseindia.com/content/circulars/CMTR72260.pdf",
    "https://www.nseindia.com/resources/exchange-communication-holidays/",
)
HOLIDAYS_2026 = frozenset(date.fromisoformat(value) for value in (
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26",
    "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01",
    "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
    "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
))
OPEN = time(9, 15)
CLOSE = time(15, 30)


def regular_session_date(day):
    """Return (eligible, reason). Unknown years and special sessions fail closed."""
    if not isinstance(day, date):
        raise ValueError("day must be a date")
    if day.year != 2026:
        return False, "calendar_year_unverified"
    if day.weekday() >= 5:
        return False, "weekend"
    if day in HOLIDAYS_2026:
        return False, "nse_holiday"
    return True, "regular_session"


def regular_market_status(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    local = now.astimezone(IST)
    allowed, reason = regular_session_date(local.date())
    if not allowed:
        return reason
    return "regular_session" if OPEN <= local.time() < CLOSE else "outside_regular_hours"


def regular_session(day):
    allowed, reason = regular_session_date(day)
    if not allowed:
        raise ValueError(f"No verified regular session: {reason}")
    return Session(datetime.combine(day, OPEN, IST), datetime.combine(day, CLOSE, IST),
                   "NSE capital-market 2026 regular calendar")

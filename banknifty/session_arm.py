"""Pre-open arming rules for a one-session, token-in-memory feed capture."""
from datetime import datetime, time, timedelta, timezone

from .groww import IST
from .market_calendar import OPEN, regular_session_date


ARM_FROM = time(9, 0)
PREPARE_LEAD = timedelta(seconds=90)


def armable_open(now):
    """Return today's regular opening only during its reviewed pre-open window."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Current time must be timezone-aware")
    local = now.astimezone(IST)
    if not regular_session_date(local.date())[0] or not ARM_FROM <= local.time() < OPEN:
        raise ValueError("Arm only on a reviewed regular session from 09:00 to 09:15 IST")
    return datetime.combine(local.date(), OPEN, IST)


def wait_until(target, stop_event, *, now=None):
    """Sleep responsively until a timezone-aware target or user cancellation."""
    if not isinstance(target, datetime) or target.tzinfo is None or target.utcoffset() is None:
        raise ValueError("A timezone-aware wait target is required")
    clock = now or (lambda: datetime.now(timezone.utc))
    while True:
        if stop_event.is_set():
            return False
        instant = clock()
        if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Current time must be timezone-aware")
        remaining = (target - instant).total_seconds()
        if remaining <= 0:
            return True
        stop_event.wait(min(remaining, 5.0))

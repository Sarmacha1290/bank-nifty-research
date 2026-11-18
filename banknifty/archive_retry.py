"""One bounded retry for a transient SQLite writer lock during live capture."""

import sqlite3
import time


_BUSY_CODES = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


def _is_busy(exc):
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 255 in _BUSY_CODES
    return str(exc).lower() in {"database is locked", "database is busy"}


def save_with_busy_retry(write, *, pause=time.sleep, delay_seconds=0.2, on_retry=None):
    """Return (write result, retries); never retry validation or disk errors.

    Callers must use an idempotent, transactionally committed archive write.
    The same parsed observation is retried without changing its receipt time.
    """
    try:
        return write(), 0
    except sqlite3.OperationalError as exc:
        if not _is_busy(exc):
            raise
    if on_retry is not None:
        on_retry()
    pause(delay_seconds)
    return write(), 1

"""Local, credential-free checks before a regular-session feed trial."""

from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .groww import IST
from .market_calendar import OPEN, regular_market_status, regular_session_date
from .tick_history import count_index_ticks
from .groww_feed_security import GrowwFeedSecurityError, verify_reviewed_sdk_source


def next_reviewed_session(now):
    """Return the next 2026 regular opening; unknown years fail closed."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(IST)
    for offset in range(367):
        day = local.date() + timedelta(days=offset)
        allowed, _ = regular_session_date(day)
        if not allowed:
            continue
        opening = datetime.combine(day, OPEN, IST)
        if offset == 0 and local >= opening:
            # The opening has passed even if today's market remains open.
            continue
        return opening
    return None


def check_local_readiness(data_dir, *, now=None):
    """Inspect local prerequisites without contacting Groww or reading a token."""
    instant = now or datetime.now(timezone.utc)
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    folder = Path(data_dir)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".write-check-", dir=folder) as stream:
            stream.write(b"ok")
            stream.flush()
        archive_writable = True
    except OSError:
        archive_writable = False
    try:
        free_bytes = shutil.disk_usage(folder if folder.exists() else folder.parent).free
        free_gb = round(free_bytes / (1024 ** 3), 2)
    except OSError:
        free_gb = None
    try:
        recorded_ticks = count_index_ticks(folder / "market-data.sqlite3")
    except (OSError, ValueError, sqlite3.Error):
        recorded_ticks = None
    next_session = next_reviewed_session(instant)
    try:
        groww_sdk_version = version("growwapi")
    except PackageNotFoundError:
        groww_sdk_version = None
    try:
        verify_reviewed_sdk_source()
        socket_guard_source_reviewed = True
    except (GrowwFeedSecurityError, PackageNotFoundError, ImportError):
        socket_guard_source_reviewed = False
    return {
        "checked_at_ist": instant.astimezone(IST).isoformat(),
        "market_status": regular_market_status(instant),
        "next_reviewed_open_ist": next_session.isoformat() if next_session else None,
        "groww_sdk_installed": find_spec("growwapi") is not None,
        "groww_sdk_version": groww_sdk_version,
        "groww_socket_guard_reviewed": socket_guard_source_reviewed,
        "archive_writable": archive_writable,
        "disk_free_gb": free_gb,
        "recorded_ticks": recorded_ticks,
    }

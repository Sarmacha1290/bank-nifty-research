"""Verified local database snapshot after a recording attempt.

This protects against later accidental corruption of the active SQLite file.
It remains on the same laptop and does not protect against drive loss.
"""

from datetime import datetime, timezone
from pathlib import Path
from shutil import disk_usage
from uuid import uuid4

from .backup import backup_database


def backup_after_capture(database, started_at, backup_dir):
    """Create a new integrity-checked copy; never overwrite an older snapshot."""
    if not isinstance(started_at, datetime) or started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("Capture start must be timezone-aware")
    source, directory = Path(database), Path(backup_dir)
    if not source.is_file():
        raise ValueError("No market database exists to back up")
    if directory.resolve() == source.resolve():
        raise ValueError("Backup directory must differ from the market database")
    source_bytes = source.stat().st_size
    directory.mkdir(parents=True, exist_ok=True)
    # Reserve room for the temporary SQLite snapshot and normal app activity.
    if disk_usage(directory).free < source_bytes * 2 + 64 * 1024 * 1024:
        raise OSError("Not enough free space for a verified local capture backup")
    stamp = started_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"capture-{stamp}-{uuid4().hex}.sqlite3"
    size = backup_database(source, target)
    return {"path": target, "bytes": size,
            "scope": "local_same_laptop_database_snapshot_not_offsite"}

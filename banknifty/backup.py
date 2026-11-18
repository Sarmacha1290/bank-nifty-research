"""Consistent, integrity-checked copies of the local market database."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile


def backup_database(database, destination):
    """Create a new SQLite snapshot without changing the working archive."""
    source, target = Path(database), Path(destination)
    if not source.is_file():
        raise ValueError("Market database does not exist")
    if source.resolve() == target.resolve() or target.exists():
        raise ValueError("Choose a new backup file; existing files are never overwritten")
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix="banknifty-backup-", suffix=".sqlite3",
                                         dir=target.parent, delete=False)
    temporary = Path(handle.name)
    handle.close()
    try:
        # A read/write connection can checkpoint a crash-left WAL when it
        # closes. Open the source read-only while still observing committed WAL
        # records; immutable=1 would incorrectly ignore those records.
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as live:
            with closing(sqlite3.connect(temporary)) as snapshot:
                live.backup(snapshot)
                result = snapshot.execute("PRAGMA integrity_check").fetchone()
                if result is None or result[0] != "ok":
                    raise ValueError("Backup integrity check failed")
        # The destination was checked before work began. Recheck to avoid
        # replacing a file created while the snapshot was being made.
        if target.exists():
            raise ValueError("Backup destination appeared during the copy")
        os.rename(temporary, target)
        return target.stat().st_size
    finally:
        temporary.unlink(missing_ok=True)

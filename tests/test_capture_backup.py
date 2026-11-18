"""Automatic capture backup must be a separate verified, non-overwriting copy."""
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from banknifty.capture_backup import backup_after_capture
from banknifty.feed import IndexTick
from banknifty.tick_history import save_index_tick


class CaptureBackupTests(unittest.TestCase):
    def test_two_backups_are_distinct_and_restore_saved_rows(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "data" / "market.sqlite3"
            at = datetime(2026, 10, 5, 3, 45, tzinfo=timezone.utc)
            save_index_tick(database, IndexTick("NSE-BANKNIFTY", at, at, 55000))
            first = backup_after_capture(database, at, Path(folder) / "data" / "backups")
            second = backup_after_capture(database, at, Path(folder) / "data" / "backups")
            self.assertNotEqual(first["path"], second["path"])
            self.assertTrue(first["path"].is_file())
            with closing(sqlite3.connect(first["path"])) as restored:
                self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(restored.execute("SELECT COUNT(*) FROM index_ticks").fetchone()[0], 1)
            self.assertEqual(first["scope"], "local_same_laptop_database_snapshot_not_offsite")

    def test_missing_archive_never_creates_backup(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder) / "backups"
            at = datetime(2026, 10, 5, 3, 45, tzinfo=timezone.utc)
            with self.assertRaisesRegex(ValueError, "No market database"):
                backup_after_capture(Path(folder) / "missing.sqlite3", at, directory)
            self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
from contextlib import closing
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from banknifty.backup import backup_database
from banknifty.feed import IndexTick
from banknifty.groww import IST
from banknifty.tick_history import count_index_ticks, save_index_tick
from datetime import datetime, timedelta


class BackupTests(unittest.TestCase):
    def test_live_database_is_copied_and_existing_files_are_protected(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / "market.sqlite3"
            target = Path(folder) / "backup.sqlite3"
            moment = datetime(2026, 10, 5, 10, tzinfo=IST)
            save_index_tick(source, IndexTick("NSE-BANKNIFTY", moment, moment + timedelta(seconds=1), 55000))
            self.assertGreater(backup_database(source, target), 0)
            self.assertEqual(count_index_ticks(target), 1)
            with closing(sqlite3.connect(target)) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            with self.assertRaisesRegex(ValueError, "never overwritten"):
                backup_database(source, target)
            self.assertEqual(count_index_ticks(source), 1)
            self.assertEqual(count_index_ticks(target), 1)
            with self.assertRaisesRegex(ValueError, "never overwritten"):
                backup_database(source, source)


if __name__ == "__main__":
    unittest.main()

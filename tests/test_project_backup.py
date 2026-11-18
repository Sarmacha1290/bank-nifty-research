"""A whole-project backup restores its database and avoids credential files."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile, ZIP_DEFLATED

from banknifty.project_backup import backup_project, rehearse_project_restore


class ProjectBackupTests(unittest.TestCase):
    def test_verified_portable_backup_and_no_overwrite(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "app"
            (project / "data").mkdir(parents=True)
            (project / "project.json").write_text('{"version":"test"}')
            (project / "README.md").write_text("project")
            (project / "PROJECT_ROADMAP.md").write_text("roadmap")
            (project / "deliverables").mkdir()
            (project / "deliverables" / "Bank_Nifty_Project_Architecture_2026-10-04-v4.pptx").write_bytes(b"deck")
            (project / ".env").write_text("SECRET")
            (project / "api-token.txt").write_text("SECRET")
            (project / "innocent-notes.md").write_text("SECRET")
            (project / "data" / "private.json").write_text('{"credential":"SECRET"}')
            (project / "banknifty").mkdir()
            (project / "banknifty" / "secret.py").write_text("API_KEY='SECRET'")
            with closing(sqlite3.connect(project / "data" / "market-data.sqlite3")) as connection:
                connection.execute("CREATE TABLE sample (value INTEGER)")
                connection.execute("INSERT INTO sample VALUES (42)")
                connection.commit()
            target = root / "backup.zip"
            report = backup_project(project, target)
            self.assertEqual(report["files"], 5)
            self.assertEqual(len(report["sha256"]), 64)
            rehearsal = rehearse_project_restore(target)
            self.assertEqual(rehearsal["files"], 5)
            self.assertEqual(rehearsal["project_version"], "test")
            self.assertEqual(rehearsal["database_integrity"], "ok")
            self.assertEqual((project / "README.md").read_text(), "project")
            with ZipFile(target) as archive:
                self.assertIn("README.md", archive.namelist())
                self.assertIn("PROJECT_ROADMAP.md", archive.namelist())
                self.assertIn("deliverables/Bank_Nifty_Project_Architecture_2026-10-04-v4.pptx", archive.namelist())
                self.assertNotIn(".env", archive.namelist())
                self.assertNotIn("api-token.txt", archive.namelist())
                self.assertNotIn("innocent-notes.md", archive.namelist())
                self.assertNotIn("data/private.json", archive.namelist())
                self.assertNotIn("banknifty/secret.py", archive.namelist())
                manifest = json.loads(archive.read("backup-manifest.json"))
                self.assertIn("data/market-data.sqlite3", manifest["file_sha256"])
            with self.assertRaises(ValueError):
                backup_project(project, target)
            with self.assertRaises(ValueError):
                backup_project(project, project / "nested.zip")

    def test_restore_rehearsal_rejects_tampering_and_unsafe_entries(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "app"
            (project / "data").mkdir(parents=True)
            (project / "project.json").write_text('{"version":"test"}')
            (project / "README.md").write_text("original")
            with closing(sqlite3.connect(project / "data" / "market-data.sqlite3")) as connection:
                connection.execute("CREATE TABLE sample (value INTEGER)")
                connection.commit()
            backup = root / "original.zip"
            backup_project(project, backup)
            changed = root / "changed.zip"
            with ZipFile(backup) as original, ZipFile(changed, "w", ZIP_DEFLATED) as altered:
                for name in original.namelist():
                    altered.writestr(name, b"tampered" if name == "README.md" else original.read(name))
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                rehearse_project_restore(changed)
            unsafe = root / "unsafe.zip"
            with ZipFile(backup) as original, ZipFile(unsafe, "w", ZIP_DEFLATED) as altered:
                for name in original.namelist():
                    altered.writestr(name, original.read(name))
                altered.writestr("../outside.txt", "escape")
            with self.assertRaises(ValueError):
                rehearse_project_restore(unsafe)
            self.assertFalse((root / "outside.txt").exists())
            self.assertEqual((project / "README.md").read_text(), "original")


if __name__ == "__main__":
    unittest.main()

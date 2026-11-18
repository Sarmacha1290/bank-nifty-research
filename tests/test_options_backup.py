"""Options research data survives a bounded, validated project backup."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

from banknifty.options_ledger import OptionsLedger
from banknifty.options_workspace import OptionsWorkspaceController, sample_import_document
from banknifty.project_backup import backup_project, rehearse_project_restore


T = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
PREFIX = "data/options-research/"


class OptionsBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        (self.project / "data").mkdir(parents=True)
        (self.project / "project.json").write_text('{"version":"options-backup-fixture"}', encoding="utf-8")
        with closing(sqlite3.connect(self.project / "data" / "market-data.sqlite3")) as db:
            db.execute("CREATE TABLE sample (value INTEGER)")
            db.execute("INSERT INTO sample VALUES (42)")
            db.commit()
        self.storage = self.project / "data" / "options-research"
        self.controller = OptionsWorkspaceController(self.storage, clock=lambda: T)

    def imported_workspace(self, *, previous=False):
        source = self.root / "provided-quotes.json"
        if previous:
            earlier = sample_import_document(T - timedelta(days=1))
            source.write_text(json.dumps(earlier), encoding="utf-8")
            self.controller.import_file(source)
        source.write_text(json.dumps(sample_import_document(T)), encoding="utf-8")
        self.controller.import_file(source)
        settings = {"direction": "BULLISH", "strategy": "LONG_OPTION", "horizon_minutes": "15",
                    "capital": "100000", "max_risk_fraction": ".01", "max_contracts": "5", "total_fees": "2"}
        fixed = self.controller.evaluate(settings)
        self.assertEqual(fixed.status, "candidate")
        self.controller.clock = lambda: T + timedelta(seconds=10)
        self.controller.manual_fill(signal_id=fixed.signal_id, contract_id=fixed.legs[0].contract_id,
                                    side="BUY", quantity=1, price="101", occurred_at=T + timedelta(seconds=1),
                                    event_id="manual-fill-fixture", external_fill_id="reported-fixture", fees="1")
        self.controller.set_paused(True)
        return fixed

    def test_import_manual_event_pause_and_previous_import_survive_restore(self):
        fixed = self.imported_workspace(previous=True)
        before_events = self.controller.ledger().events()
        before_hash = sha256((self.storage / "options-ledger.sqlite3").read_bytes()).hexdigest()
        target = self.root / "backup.zip"
        report = backup_project(self.project, target)
        self.assertEqual(report["options_workspace"], {"state": True, "imports": 2, "ledger_events": 2})
        self.assertEqual(sha256((self.storage / "options-ledger.sqlite3").read_bytes()).hexdigest(), before_hash)
        rehearsal = rehearse_project_restore(target)
        self.assertEqual(rehearsal["options_workspace"], report["options_workspace"])
        restored = self.root / "restored"
        with ZipFile(target) as archive:
            # Only a backup just created and rehearsed by this test is extracted.
            archive.extractall(restored)
        restarted = OptionsWorkspaceController(restored / "data" / "options-research", clock=lambda: T)
        self.assertTrue(restarted.paused)
        self.assertEqual(restarted.market, self.controller.market)
        self.assertEqual(restarted.settings, self.controller.settings)
        self.assertEqual(restarted.ledger().events(), before_events)
        self.assertEqual(restarted.ledger().exposure(fixed.signal_id)["status"], "long_only_open")

    def test_synthetic_state_and_empty_paused_state_survive(self):
        self.controller.load_demo()
        self.controller.set_paused(True)
        target = self.root / "synthetic.zip"
        report = backup_project(self.project, target)
        self.assertEqual(report["options_workspace"], {"state": True, "imports": 0, "ledger_events": None})
        self.assertEqual(rehearse_project_restore(target)["options_workspace"], report["options_workspace"])
        state = {"version": 1, "kind": "empty", "settings": {}, "paused": True}
        (self.storage / "workspace-state.json").write_text(json.dumps(state), encoding="utf-8")
        empty = self.root / "empty.zip"
        backup_project(self.project, empty)
        self.assertTrue(rehearse_project_restore(empty)["options_workspace"]["state"])

    def test_innocent_private_files_and_unapproved_data_are_excluded(self):
        self.imported_workspace()
        (self.storage / "notes.json").write_text('{"memo":"DUMMY-PRIVATE-FIXTURE"}', encoding="utf-8")
        (self.storage / "imports" / "normal-name.json").write_text('{"memo":"DUMMY-PRIVATE-FIXTURE"}', encoding="utf-8")
        (self.storage / "workspace-state.pending").write_text("DUMMY-PRIVATE-FIXTURE", encoding="utf-8")
        (self.storage / "credentials.sqlite3").write_bytes(b"DUMMY-PRIVATE-FIXTURE")
        target = self.root / "private-excluded.zip"
        backup_project(self.project, target)
        with ZipFile(target) as archive:
            self.assertFalse(any("notes.json" in name or "normal-name" in name or "credentials" in name or ".pending" in name
                                 for name in archive.namelist()))
            self.assertFalse(any(b"DUMMY-PRIVATE-FIXTURE" in archive.read(name) for name in archive.namelist()))

    def test_constructor_validates_only_temporary_ledger_copy(self):
        self.imported_workspace()
        original = (self.storage / "options-ledger.sqlite3").resolve()
        seen = []
        from banknifty.options_ledger import validate_ledger
        def checked(path):
            self.assertNotEqual(Path(path).resolve(), original)
            seen.append(Path(path))
            return validate_ledger(path)
        target = self.root / "copy-validated.zip"
        with patch("banknifty.options_ledger.validate_ledger", checked):
            backup_project(self.project, target)
            rehearse_project_restore(target)
        self.assertEqual(len(seen), 2)

    def test_malformed_state_missing_import_and_hash_mismatch_fail(self):
        self.imported_workspace()
        state_path = self.storage / "workspace-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state_path.write_text('{"version":1,"kind":"empty","settings":{"unknown":"DUMMY"}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported settings"):
            backup_project(self.project, self.root / "bad-state.zip")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        imported = next((self.storage / "imports").glob("*.json"))
        original_content = imported.read_bytes()
        imported.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            backup_project(self.project, self.root / "bad-hash.zip")
        imported.write_bytes(original_content)
        state["import_id"] = "f" * 64
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing validated import"):
            backup_project(self.project, self.root / "missing-import.zip")
        self.assertFalse((self.root / "bad-state.zip").exists())

    def test_hash_named_but_invalid_schema_import_fails_without_leaking(self):
        self.controller.load_demo()
        imports = self.storage / "imports"
        imports.mkdir()
        raw = b'{"note":"DUMMY-PRIVATE-FIXTURE"}'
        (imports / (sha256(raw).hexdigest() + ".json")).write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "schema"):
            backup_project(self.project, self.root / "malformed-import.zip")
        self.assertFalse((self.root / "malformed-import.zip").exists())

    def test_import_file_count_is_bounded(self):
        self.imported_workspace(previous=True)
        with patch("banknifty.project_backup._OPTIONS_IMPORT_LIMIT", 1):
            with self.assertRaisesRegex(ValueError, "100-file"):
                backup_project(self.project, self.root / "too-many.zip")

    def test_options_symlinks_and_junctions_are_rejected_before_reading(self):
        self.controller.load_demo()
        actual_is_symlink = Path.is_symlink
        def marked(path):
            return path == self.storage or actual_is_symlink(path)
        with patch.object(Path, "is_symlink", marked):
            with self.assertRaisesRegex(ValueError, "symlink or junction"):
                backup_project(self.project, self.root / "linked.zip")
        actual_is_junction = Path.is_junction
        def junction(path):
            return path == self.storage or actual_is_junction(path)
        with patch.object(Path, "is_junction", junction):
            with self.assertRaisesRegex(ValueError, "symlink or junction"):
                backup_project(self.project, self.root / "junction.zip")

    def test_ledger_tampering_and_extra_database_tables_fail(self):
        self.imported_workspace()
        ledger = self.storage / "options-ledger.sqlite3"
        with closing(sqlite3.connect(ledger)) as db:
            db.execute("CREATE TABLE unrelated_private_data (value TEXT)")
            db.commit()
        with self.assertRaisesRegex(ValueError, "unsupported database schema"):
            backup_project(self.project, self.root / "unexpected-db.zip")
        with closing(sqlite3.connect(ledger)) as db:
            db.execute("DROP TABLE unrelated_private_data")
            db.execute("DROP TRIGGER options_events_no_update")
            db.execute("UPDATE options_events SET sha256=? WHERE sequence=1", ("0" * 64,))
            db.execute("""CREATE TRIGGER options_events_no_update BEFORE UPDATE ON options_events
                       BEGIN SELECT RAISE(ABORT, 'append-only ledger'); END""")
            db.commit()
        before = ledger.read_bytes()
        with self.assertRaisesRegex(ValueError, "hash chain"):
            backup_project(self.project, self.root / "tampered-db.zip")
        self.assertEqual(ledger.read_bytes(), before)

    def test_restore_rejects_malformed_options_even_with_recomputed_zip_hash(self):
        self.imported_workspace()
        target = self.root / "original.zip"
        backup_project(self.project, target)
        changed = self.root / "changed.zip"
        name = PREFIX + "workspace-state.json"
        bad = b'{"version":1,"kind":"empty","settings":{"unknown":"DUMMY"}}'
        with ZipFile(target) as original, ZipFile(changed, "w", ZIP_DEFLATED) as altered:
            manifest = json.loads(original.read("backup-manifest.json"))
            manifest["file_sha256"][name] = sha256(bad).hexdigest()
            for entry in original.namelist():
                payload = bad if entry == name else json.dumps(manifest).encode() if entry == "backup-manifest.json" else original.read(entry)
                altered.writestr(entry, payload)
        with self.assertRaisesRegex(ValueError, "unsupported settings"):
            rehearse_project_restore(changed)


if __name__ == "__main__":
    unittest.main()

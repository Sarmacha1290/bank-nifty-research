"""Restore the exact research history and reject unvalidated archive sidecars."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from banknifty.market_archive import ImportManifest, MarketArchive
from banknifty.options_ledger import OptionsLedger
from banknifty.options_workspace import (OptionsWorkspaceController, read_analysis_results,
    read_research_runs, synthetic_market)
from banknifty.project_backup import backup_project, rehearse_project_restore

T = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)


class ResearchBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root/'project'
        (self.project/'data').mkdir(parents=True)
        (self.project/'project.json').write_text('{"version":"0.98.0-test"}')
        with closing(sqlite3.connect(self.project/'data/market-data.sqlite3')) as db:
            db.execute('CREATE TABLE fixture (n INTEGER)'); db.commit()
        self.storage = self.project/'data/options-research'
        self.controller = OptionsWorkspaceController(self.storage,clock=lambda:T)

    def archive(self):
        sample = synthetic_market(T)
        path = self.storage/'synthetic-archive.sqlite3'
        MarketArchive(path).append_batch(contracts=sample.contracts,quotes=sample.quotes[:5],
            manifest=ImportManifest('backup-fixture',T+timedelta(hours=1),b'Synthetic archive fixture',
                                    evidence_kind='synthetic'))
        return path

    def replay(self):
        self.controller.load_demo()
        self.controller.evaluate({'direction':'BULLISH','strategy':'LONG_OPTION','horizon_minutes':15,
            'capital':'100000','max_risk_fraction':'.01','max_contracts':1,'total_fees':'2'})
        return self.controller.replay(manual_latency_seconds=5,fees='2',slippage_per_leg='.05')

    def test_archive_replay_assumptions_and_cache_survive_restore_without_source_writes(self):
        archive = self.archive()
        replay = self.replay()
        original = {p.relative_to(self.project).as_posix():sha256(p.read_bytes()).hexdigest()
                    for p in self.project.rglob('*') if p.is_file()}
        runs = read_research_runs(self.storage)
        target = self.root/'research.zip'
        result = backup_project(self.project,target)
        restored = self.root/'restored'
        self.assertEqual(rehearse_project_restore(target)['options_workspace'],result['options_workspace'])
        with ZipFile(target) as zipped: zipped.extractall(restored)
        destination = restored/'data/options-research'
        self.assertEqual(read_research_runs(destination),runs)
        self.assertEqual(read_analysis_results(destination)['replay']['signal_fingerprint'],replay.signal_fingerprint)
        self.assertEqual(MarketArchive(destination/archive.name,read_only=True).inventory(),
                         MarketArchive(archive,read_only=True).inventory())
        self.assertEqual(original,{p.relative_to(self.project).as_posix():sha256(p.read_bytes()).hexdigest()
                    for p in self.project.rglob('*') if p.is_file()})

    def test_extra_archive_table_fails_without_saving_unrelated_content(self):
        path=self.archive()
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE unrelated_private_data (value TEXT)')
            db.execute("INSERT INTO unrelated_private_data VALUES ('DUMMY-PRIVATE-FIXTURE')")
            db.commit()
        target=self.root/'bad-archive.zip'
        with self.assertRaises(ValueError): backup_project(self.project,target)
        self.assertFalse(target.exists())

    def test_empty_ledger_extra_column_fails_without_packaging_private_schema(self):
        path = self.storage/'options-ledger.sqlite3'
        OptionsLedger(path)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("ALTER TABLE options_events ADD COLUMN unrelated_private TEXT DEFAULT 'DUMMY-PRIVATE-FIXTURE'")
        original = path.read_bytes()
        target = self.root/'bad-ledger-schema.zip'
        with self.assertRaisesRegex(ValueError, "fixed application schema"):
            backup_project(self.project, target)
        self.assertFalse(target.exists())
        self.assertEqual(path.read_bytes(), original)

    def test_data_junction_is_rejected_before_legacy_database_is_read(self):
        original_is_junction = Path.is_junction
        def marked(path):
            return path == self.project/'data' or original_is_junction(path)
        with patch.object(Path, 'is_junction', marked), patch('banknifty.project_backup.backup_database') as backup:
            with self.assertRaisesRegex(ValueError, "symlink or junction"):
                backup_project(self.project, self.root/'linked-data.zip')
            backup.assert_not_called()
        self.assertFalse((self.root/'linked-data.zip').exists())

    def test_crash_left_wal_is_read_without_changing_source_database(self):
        source = self.project/'data/market-data.sqlite3'
        with closing(sqlite3.connect(source)) as writer:
            self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            writer.execute('INSERT INTO fixture VALUES (42)')
            writer.commit()
            captured = {suffix: Path(str(source) + suffix).read_bytes()
                        for suffix in ('', '-wal', '-shm') if Path(str(source) + suffix).exists()}
        # Recreate the exact committed WAL state before normal close checkpointed
        # it. This is a fixture-only crash-state simulation, without a process.
        for suffix, content in captured.items():
            Path(str(source) + suffix).write_bytes(content)
        target = self.root/'wal-project.zip'
        backup_project(self.project, target)
        self.assertEqual(source.read_bytes(), captured[''])
        self.assertEqual(Path(str(source) + '-wal').read_bytes(), captured['-wal'])
        # SQLite may update transient SHM reader coordination even for a mode=ro
        # connection. The durable database and committed WAL must stay exact.
        with ZipFile(target) as zipped:
            restored = self.root/'restored-wal.sqlite3'
            restored.write_bytes(zipped.read('data/market-data.sqlite3'))
        with closing(sqlite3.connect(restored)) as db:
            self.assertEqual(db.execute('SELECT n FROM fixture').fetchall(), [(42,)])

    def test_changed_replay_and_malformed_cache_are_not_silently_omitted(self):
        self.replay()
        path=next((self.storage/'research-runs').glob('*.json'))
        original=path.read_bytes()
        path.write_bytes(original.replace(b'"replay"',b'"changed"',1))
        with self.assertRaises(ValueError): backup_project(self.project,self.root/'bad-run.zip')
        path.write_bytes(original)
        (self.storage/'analysis-results.json').write_text('{}')
        with self.assertRaises(ValueError): backup_project(self.project,self.root/'bad-cache.zip')

    def test_unrecognized_worker_state_fails_and_private_sidecars_are_excluded(self):
        self.controller.load_demo()
        (self.storage/'worker-state.json').write_text('{"unexpected":"DUMMY-PRIVATE-FIXTURE"}')
        with self.assertRaises(ValueError): backup_project(self.project,self.root/'bad-worker.zip')
        (self.storage/'worker-state.json').unlink()
        (self.storage/'worker-secrets.json').write_text('DUMMY-PRIVATE-FIXTURE')
        target=self.root/'approved.zip'
        backup_project(self.project,target)
        with ZipFile(target) as zipped:
            self.assertNotIn('data/options-research/worker-secrets.json',zipped.namelist())


if __name__=='__main__': unittest.main()

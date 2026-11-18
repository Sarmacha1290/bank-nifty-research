"""Bounded local worker tests; fixture archive only, no running broker/feed."""
import ast
from contextlib import closing, contextmanager, redirect_stdout
from dataclasses import replace
from datetime import timedelta
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from banknifty.market_archive import ImportManifest, MarketArchive, read_market_snapshot
from banknifty.options_ledger import OptionsLedger
from banknifty.options_pipeline import PipelinePolicy
from banknifty.options_strategy import ResearchPolicy
from banknifty.research_worker import (ArchiveResearchWorker, WorkerConfig, MAX_DURATION_SECONDS,
    read_worker_status, validate_worker_state, _worker_lease, main)
from tests.test_options_pipeline import NOW, option_fixture, underlying_fixture


class FakeClock:
    def __init__(self, wall=NOW+timedelta(seconds=1), monotonic=10):
        self.wall, self.value = wall, monotonic
    def now(self): return self.wall
    def mono(self): return self.value
    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds); self.value += seconds


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.clock = FakeClock()
        self.config = WorkerConfig(enabled=True, archive_kind="synthetic")
        self.controls()

    def controls(self, paused=False):
        (self.path/"workspace-state.json").write_text(json.dumps({"version":1,"kind":"empty","settings":{},"paused":paused}),encoding="utf-8")

    def corpus(self, *, cutoff=NOW, future=False):
        contracts, quotes = option_fixture(cutoff=cutoff)
        underlying = underlying_fixture(cutoff=cutoff)
        archive = MarketArchive(self.path/"synthetic-archive.sqlite3")
        archive.append_batch(contracts=contracts,quotes=quotes,underlying=underlying,
            manifest=ImportManifest("worker fixture",NOW,b"fixture batch",evidence_kind="synthetic"))
        if future:
            later = NOW+timedelta(minutes=1)
            cs,qs = option_fixture(cutoff=later)
            archive.append_batch(contracts=cs,quotes=qs,underlying=underlying_fixture(cutoff=later),
                manifest=ImportManifest("future worker fixture",later,b"future batch",evidence_kind="synthetic"))
        return archive

    def worker(self, config=None):
        return ArchiveResearchWorker(self.path,config or self.config,clock=self.clock.now,monotonic=self.clock.mono)

    def signals(self):
        return OptionsLedger(self.path/"options-ledger.sqlite3",read_only=True).list_signals()

    def test_disabled_is_inert_and_cli_does_not_autostart(self):
        self.corpus(); before=set(self.path.iterdir())
        self.assertEqual(self.worker(WorkerConfig()).step().status,"inactive")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--storage-dir",str(self.path)]),0)
        self.assertIn('inactive',output.getvalue())
        self.assertEqual(set(self.path.iterdir()),before)

    def test_frozen_current_boundary_once_and_restart_deduplication(self):
        self.corpus(); worker=self.worker()
        first=worker.step()
        self.assertEqual(first.status,"candidate")
        self.assertEqual(worker.step().status,"boundary_already_processed")
        self.assertEqual(self.worker().step().status,"boundary_already_processed")
        signals=self.signals();self.assertEqual(len(signals),1)
        signal=signals[0]
        self.assertEqual(signal['worker']['cutoff'],(NOW+timedelta(seconds=1)).isoformat())
        self.assertEqual(signal['worker']['config_fingerprint'],self.config.fingerprint)
        self.assertEqual(signal['worker']['archive_mode'],'observed')
        self.assertEqual(signal['worker']['actual_fills_created'],0)
        self.assertEqual(signal['signal_id'],first.attempt_id)

    def test_checkpoint_loss_recovers_immutable_attempt_from_journal(self):
        self.corpus();first=self.worker().step()
        (self.path/'worker-state.json').unlink()
        recovered=self.worker().step()
        self.assertEqual(recovered.status,'boundary_already_processed')
        self.assertEqual(recovered.signal_id,first.signal_id)
        self.assertEqual(len(self.signals()),1)

    def test_config_change_cannot_reopen_already_journaled_boundary(self):
        self.corpus();self.worker().step()
        (self.path/'worker-state.json').unlink()
        changed=replace(self.config,strategy='LONG_OPTION')
        self.assertEqual(self.worker(changed).step().status,'boundary_already_processed')
        self.assertEqual(len(self.signals()),1)

    def test_waits_for_explicit_delay_and_skips_missed_windows(self):
        self.corpus();self.clock.wall=NOW
        worker=self.worker();self.assertEqual(worker.step().status,'waiting_boundary')
        self.assertFalse((self.path/'options-ledger.sqlite3').exists())
        self.clock.advance(10)
        self.assertEqual(worker.step().status,'missed_window')
        self.assertEqual(self.signals()[0]['status'],'no_trade')
        self.assertEqual(self.signals()[0]['legs'],[])
        self.clock.advance(3*60)
        worker.step()
        self.assertEqual(len(self.signals()),2) # no backfill of intervening boundaries

    def test_pause_blocks_new_attempt_without_touching_existing_fills(self):
        self.corpus();worker=self.worker();first=worker.step()
        ledger=OptionsLedger(self.path/'options-ledger.sqlite3');signal=ledger.list_signals()[0]
        leg=next(item for item in signal['legs'] if item['side']=='BUY')
        ledger.record_fill('manual-fixture',first.signal_id,leg['contract']['contract_id'],'BUY',25,leg['price'],
                           self.clock.wall,self.clock.wall,fees='0')
        fills=[event for event in ledger.events() if event['kind']=='fill']
        self.controls(True);self.clock.advance(60)
        self.assertEqual(worker.step().status,'paused')
        self.assertEqual([event for event in ledger.events() if event['kind']=='fill'],fills)
        self.assertEqual(self.signals()[-1]['status'],'no_trade')
        self.controls(False)
        self.assertEqual(worker.step().status,'boundary_already_processed')

    def test_missing_or_malformed_workspace_controls_fail_closed(self):
        for raw in ('{', '{"version":1,"paused":"false"}', '{"version":1,"paused":false,"unknown":true}',
                    '{"version":1,"paused":false,"paused":false}',
                    '{"version":1,"kind":"unsupported","paused":false}',
                    '{"version":1,"kind":"synthetic","paused":false}',
                    '{"version":1,"kind":"empty","paused":false,"settings":{"archive_kind":"unknown"}}',
                    '[' * 2000 + '0' + ']' * 2000):
            with self.subTest(raw=raw):
                (self.path/'workspace-state.json').write_text(raw,encoding='utf-8')
                result=self.worker().step()
                self.assertEqual(result.status,'paused')
                self.assertIn('workspace_pause_state_missing_or_invalid',result.reasons)
                self.clock.advance(60)
        (self.path/'workspace-state.json').unlink()
        self.assertEqual(self.worker().step().status,'paused')

    def test_cancel_is_terminal_and_never_creates_fill_or_signal(self):
        self.corpus();worker=self.worker()
        self.assertEqual(worker.step(cancelled=True).status,'cancelled')
        self.assertEqual(worker.step().status,'cancelled')
        self.assertFalse((self.path/'options-ledger.sqlite3').exists())

    def test_missing_archive_is_explicit_no_trade_and_is_not_created(self):
        result=self.worker().step()
        self.assertEqual(result.status,'archive_unavailable')
        self.assertFalse((self.path/'synthetic-archive.sqlite3').exists())
        self.assertEqual(self.signals()[0]['status'],'no_trade')
        self.assertNotIn('underlying_price',self.signals()[0])

    def test_stale_or_incomplete_archive_never_substitutes_a_price(self):
        self.corpus(cutoff=NOW-timedelta(minutes=2))
        self.assertEqual(self.worker().step().status,'no_trade')
        saved=self.signals()[0]
        self.assertEqual(saved['legs'],[])
        self.assertNotIn('underlying_price',saved)

    def test_future_archive_imports_cannot_change_current_pipeline_inputs(self):
        self.corpus(future=True)
        snapshot=read_market_snapshot(self.path/'synthetic-archive.sqlite3',NOW+timedelta(seconds=1),mode='observed')
        self.assertTrue(all(row.received_at<=NOW+timedelta(seconds=1) for row in snapshot.underlying))
        result=self.worker().step()
        self.assertEqual(result.status,'candidate')
        self.assertEqual(len(self.signals()[0]['worker']['manifest_ids']),1)
        self.assertEqual(self.signals()[0]['as_of'],(NOW+timedelta(seconds=1)).isoformat())

    def test_wall_and_monotonic_regressions_stop_publication(self):
        self.corpus();worker=self.worker();worker.step()
        self.clock.wall-=timedelta(seconds=1)
        self.assertEqual(worker.step().status,'clock_regression')
        self.assertEqual(len(self.signals()),1)
        self.clock.wall+=timedelta(seconds=2)
        other=self.worker();other.step();self.clock.value-=1
        self.assertEqual(other.step().status,'clock_regression')

    def test_duration_has_hard_upper_bound_and_counts_pause_time(self):
        with self.assertRaises(ValueError): WorkerConfig(duration_seconds=MAX_DURATION_SECONDS+1)
        with self.assertRaises(ValueError): WorkerConfig(decision_delay_seconds=6)
        with self.assertRaises(ValueError): WorkerConfig(poll_interval_seconds=0)
        self.controls(True);worker=self.worker(replace(self.config,duration_seconds=2))
        self.assertEqual(worker.step().status,'paused')
        self.clock.advance(2)
        self.assertEqual(worker.step().status,'duration_complete')

    def test_run_loop_is_bounded_with_fake_wait_and_has_no_startup_registration(self):
        class Event:
            def is_set(inner): return False
            def wait(inner,seconds): self.clock.advance(seconds)
        result=self.worker(replace(self.config,duration_seconds=2)).run(Event())
        self.assertEqual(result.status,'duration_complete')
        self.assertEqual(self.clock.value,12)

    def test_poll_wait_cannot_extend_requested_duration(self):
        class Event:
            def is_set(inner): return False
            def wait(inner,seconds): self.clock.advance(seconds)
        result=self.worker(replace(self.config,duration_seconds=2,poll_interval_seconds=60)).run(Event())
        self.assertEqual(result.status,'duration_complete')
        self.assertEqual(self.clock.value,12)

    def test_invalid_checkpoint_is_preserved_and_blocks_worker(self):
        path=self.path/'worker-state.json';path.write_text('{"schema_version":1}',encoding='utf-8')
        before=path.read_bytes()
        self.assertEqual(self.worker().step().status,'invalid_checkpoint')
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse((self.path/'options-ledger.sqlite3').exists())
        self.assertEqual(read_worker_status(self.path)['status'],'invalid_checkpoint')

    def test_deeply_nested_checkpoint_fails_closed(self):
        path=self.path/'worker-state.json'
        path.write_text('[' * 2000 + '0' + ']' * 2000,encoding='utf-8')
        self.assertEqual(self.worker().step().status,'invalid_checkpoint')
        self.assertEqual(read_worker_status(self.path)['status'],'invalid_checkpoint')

    def test_state_hash_validation_and_status_heartbeat_are_read_only(self):
        self.corpus();self.worker().step()
        path=self.path/'worker-state.json';raw=path.read_bytes();envelope=json.loads(raw)
        self.assertEqual(validate_worker_state(envelope)['config_fingerprint'],self.config.fingerprint)
        status=read_worker_status(self.path,as_of=self.clock.wall)
        self.assertTrue(status['heartbeat_fresh'])
        self.assertFalse(read_worker_status(self.path,as_of=self.clock.wall+timedelta(seconds=30))['heartbeat_fresh'])
        self.assertEqual(path.read_bytes(),raw)
        envelope['payload']['last_status']='candidate-changed'
        with self.assertRaises(ValueError): validate_worker_state(envelope)

    def test_explicit_expiry_policy_roundtrips_and_restarts(self):
        self.corpus()
        contracts,_=option_fixture(cutoff=NOW)
        config=replace(self.config,research_policy=ResearchPolicy(expiry_at=contracts[0].expiry_at))
        self.assertEqual(self.worker(config).step().status,'candidate')
        self.assertEqual(read_worker_status(self.path,as_of=self.clock.wall)['config_fingerprint'],config.fingerprint)
        self.assertEqual(self.worker(config).step().status,'boundary_already_processed')

    def test_pause_or_cancel_during_analysis_blocks_commit(self):
        self.corpus()
        original=read_market_snapshot
        def pausing(*args,**kwargs):
            result=original(*args,**kwargs);self.controls(True);return result
        with patch('banknifty.research_worker.read_market_snapshot',side_effect=pausing):
            self.assertEqual(self.worker().step().status,'paused')
        self.assertEqual(self.signals()[0]['status'],'no_trade')
        self.assertEqual(self.signals()[0]['legs'],[])

    def test_cancel_during_analysis_prevents_candidate_journal_commit(self):
        self.corpus();original=read_market_snapshot;cancelled=[]
        def cancelling(*args,**kwargs):
            result=original(*args,**kwargs);cancelled.append(True);return result
        with patch('banknifty.research_worker.read_market_snapshot',side_effect=cancelling):
            self.assertEqual(self.worker().step(cancel_check=lambda: bool(cancelled)).status,'cancelled')
        self.assertFalse((self.path/'options-ledger.sqlite3').exists())

    def test_published_archive_conflict_reaches_pipeline_and_frozen_audit(self):
        archive = self.corpus()
        row = underlying_fixture()[-1]
        conflict = replace(row, value=row.value + 1)
        result = archive.append_batch(underlying=(conflict,), manifest=ImportManifest(
            'conflicting worker fixture', NOW, b'conflicting fixture batch', evidence_kind='synthetic'))
        self.assertEqual(result['status'], 'quarantined')
        self.assertEqual(self.worker().step().status, 'no_trade')
        signal = self.signals()[0]
        self.assertEqual(signal['legs'], [])
        pipeline = signal['worker']['pipeline']
        self.assertFalse(pipeline['data_gate_passed'])
        self.assertIn('published_archive_identity_conflict', pipeline['reasons'])
        self.assertEqual(len(pipeline['conflict_events']), 1)
        self.assertEqual(pipeline['conflict_events'][0]['manifest_id'], result['manifest_id'])

    def test_slow_analysis_cannot_publish_late_candidate(self):
        self.corpus();original=read_market_snapshot
        def slow(*args,**kwargs):
            result=original(*args,**kwargs);self.clock.advance(10);return result
        with patch('banknifty.research_worker.read_market_snapshot',side_effect=slow):
            self.assertEqual(self.worker().step().status,'missed_window')
        self.assertEqual(self.signals()[0]['status'],'no_trade')

    def ledger_preparation_race(self, change, *, cancelled=None):
        self.corpus();original=OptionsLedger;called=[]
        def prepare(*args,**kwargs):
            ledger=original(*args,**kwargs)
            if not called:
                called.append(True);change()
            return ledger
        with patch('banknifty.research_worker.OptionsLedger',side_effect=prepare):
            return self.worker().step(cancel_check=cancelled)

    def test_pause_during_ledger_preparation_cannot_publish_candidate(self):
        result=self.ledger_preparation_race(lambda:self.controls(True))
        self.assertEqual(result.status,'paused')
        self.assertEqual(self.signals()[0]['status'],'no_trade')
        self.assertEqual(self.signals()[0]['legs'],[])

    def test_cancel_during_ledger_preparation_cannot_publish_candidate(self):
        cancelled=[]
        result=self.ledger_preparation_race(lambda:cancelled.append(True),cancelled=lambda:bool(cancelled))
        self.assertEqual(result.status,'cancelled')
        self.assertEqual(self.signals(),[])

    def test_deadline_during_ledger_preparation_cannot_publish_candidate(self):
        result=self.ledger_preparation_race(lambda:self.clock.advance(10))
        self.assertEqual(result.status,'missed_window')
        self.assertEqual(self.signals()[0]['status'],'no_trade')

    def test_clock_regression_during_ledger_preparation_cannot_publish_candidate(self):
        result=self.ledger_preparation_race(lambda:self.clock.advance(-1))
        self.assertEqual(result.status,'clock_regression')
        self.assertEqual(self.signals(),[])

    def test_clock_regression_after_commit_guard_stops_next_step(self):
        self.corpus();original=OptionsLedger;worker=self.worker()
        def prepare(*args,**kwargs):
            ledger=original(*args,**kwargs);self.clock.value=12;return ledger
        with patch('banknifty.research_worker.OptionsLedger',side_effect=prepare):
            self.assertEqual(worker.step().status,'candidate')
        self.clock.value=11
        self.assertEqual(worker.step().status,'clock_regression')
        self.assertEqual(len(self.signals()),1)

    def reader_lock_race(self, change, *, cancelled=None):
        """Hold a real rollback-journal reader throughout the eligibility change."""
        self.corpus()
        worker = self.worker()
        ledger_path = self.path / 'options-ledger.sqlite3'
        OptionsLedger(ledger_path)
        waiting, finished = threading.Event(), threading.Event()
        output, errors, statements = [], [], []
        original = OptionsLedger._connect
        @contextmanager
        def traced(ledger):
            with original(ledger) as db:
                def trace(sql):
                    if sql in {'BEGIN IMMEDIATE', 'BEGIN EXCLUSIVE', 'COMMIT', 'ROLLBACK'}:
                        statements.append(sql)
                    if sql in {'BEGIN IMMEDIATE', 'BEGIN EXCLUSIVE'}:
                        waiting.set()
                db.set_trace_callback(trace)
                yield db
        def run():
            try:
                output.append(worker.step(cancel_check=cancelled))
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()
        with closing(sqlite3.connect(ledger_path)) as reader:
            self.assertEqual(reader.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM options_events').fetchall()
            with patch.object(OptionsLedger, '_connect', traced):
                thread = threading.Thread(target=run)
                thread.start()
                try:
                    self.assertTrue(waiting.wait(5), 'Writer did not reach transaction acquisition')
                    self.assertFalse(finished.wait(0.1), 'Reader failed to block the writer')
                    change()
                finally:
                    reader.rollback()
                    thread.join(15)
        self.assertFalse(thread.is_alive(), 'Writer failed to finish after reader release')
        if errors:
            raise errors[0]
        self.assertEqual(len(output), 1)
        return output[0], statements

    def test_real_reader_pause_before_commit_prevents_candidate(self):
        result, _ = self.reader_lock_race(lambda: self.controls(True))
        self.assertEqual(result.status, 'paused')
        self.assertEqual([signal['status'] for signal in self.signals()], ['no_trade'])
        self.assertEqual(self.signals()[0]['legs'], [])

    def test_real_reader_cancellation_before_commit_prevents_candidate(self):
        cancelled = threading.Event()
        result, _ = self.reader_lock_race(cancelled.set, cancelled=cancelled.is_set)
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(self.signals(), [])

    def test_real_reader_deadline_before_commit_prevents_candidate(self):
        result, _ = self.reader_lock_race(lambda: self.clock.advance(10))
        self.assertEqual(result.status, 'missed_window')
        self.assertEqual([signal['status'] for signal in self.signals()], ['no_trade'])
        event = OptionsLedger(self.path / 'options-ledger.sqlite3', read_only=True).events()[0]
        self.assertEqual(event['recorded_at'], self.clock.wall.isoformat(timespec='microseconds'))

    def test_real_reader_wait_records_post_lock_time_and_frozen_cutoff(self):
        result, statements = self.reader_lock_race(lambda: self.clock.advance(2))
        self.assertEqual(result.status, 'candidate')
        self.assertIn('BEGIN EXCLUSIVE', statements)
        event = OptionsLedger(self.path / 'options-ledger.sqlite3', read_only=True).events()[0]
        self.assertEqual(event['recorded_at'], self.clock.wall.isoformat(timespec='microseconds'))
        signal = event['payload']['signal']
        cutoff = (NOW + timedelta(seconds=1)).isoformat()
        self.assertEqual(signal['as_of'], cutoff)
        self.assertEqual(signal['worker']['cutoff'], cutoff)
        for leg in signal['legs']:
            self.assertEqual(leg['quote']['received_at'], NOW.isoformat())
            self.assertEqual(leg['quote']['source_at'], (NOW - timedelta(seconds=1)).isoformat())
        self.assertEqual(signal['worker']['actual_fills_created'], 0)
        self.assertEqual(len(self.signals()), 1)

    def test_real_reader_duration_limit_prevents_candidate(self):
        self.config = replace(self.config, duration_seconds=2)
        result, _ = self.reader_lock_race(lambda: self.clock.advance(2))
        self.assertEqual(result.status, 'missed_window')
        self.assertEqual(self.signals()[0]['status'], 'no_trade')

    def test_real_reader_clock_regression_prevents_candidate(self):
        result, _ = self.reader_lock_race(lambda: self.clock.advance(-1))
        self.assertEqual(result.status, 'clock_regression')
        self.assertEqual(self.signals(), [])

    def insertion_race(self, change, *, cancelled=None):
        self.corpus()
        original = OptionsLedger._connect
        changed = []
        @contextmanager
        def traced(ledger):
            with original(ledger) as db:
                def trace(sql):
                    if sql.startswith('INSERT INTO options_events') and not changed:
                        changed.append(True)
                        change()
                db.set_trace_callback(trace)
                yield db
        with patch.object(OptionsLedger, '_connect', traced):
            result = self.worker().step(cancel_check=cancelled)
        self.assertEqual(changed, [True])
        return result

    def test_pause_after_insert_rolls_back_candidate_before_commit(self):
        self.assertEqual(self.insertion_race(lambda: self.controls(True)).status, 'paused')
        self.assertEqual([signal['status'] for signal in self.signals()], ['no_trade'])

    def test_cancellation_after_insert_rolls_back_candidate_before_commit(self):
        cancelled = threading.Event()
        self.assertEqual(self.insertion_race(cancelled.set, cancelled=cancelled.is_set).status, 'cancelled')
        self.assertEqual(self.signals(), [])

    def test_deadline_after_insert_rolls_back_candidate_before_commit(self):
        self.assertEqual(self.insertion_race(lambda: self.clock.advance(10)).status, 'missed_window')
        self.assertEqual([signal['status'] for signal in self.signals()], ['no_trade'])

    def test_final_pause_control_read_delay_cannot_use_an_old_clock_sample(self):
        original = ArchiveResearchWorker._paused
        inserted, delayed = [], []
        def slow_pause_read(worker):
            result = original(worker)
            if inserted and not delayed:
                delayed.append(True)
                self.clock.advance(10)
            return result
        with patch.object(ArchiveResearchWorker, '_paused', slow_pause_read):
            result = self.insertion_race(lambda: inserted.append(True))
        self.assertEqual(delayed, [True])
        self.assertEqual(result.status, 'missed_window')
        self.assertEqual([signal['status'] for signal in self.signals()], ['no_trade'])

    def test_single_worker_process_lease_rejects_second_writer(self):
        with _worker_lease(self.path):
            with self.assertRaises(ValueError):
                with _worker_lease(self.path): pass
        with _worker_lease(self.path): pass # lease released, no service persists

    def test_no_broker_network_credential_or_fill_methods_in_worker(self):
        path=Path(__file__).resolve().parents[1]/'banknifty'/'research_worker.py'
        tree=ast.parse(path.read_text(encoding='utf-8'))
        forbidden={'requests','urllib','socket','growwapi','subprocess','groww','connect_groww'}
        for node in ast.walk(tree):
            if isinstance(node,ast.Import): self.assertTrue(all(n.name.split('.')[0] not in forbidden for n in node.names))
            if isinstance(node,ast.ImportFrom): self.assertNotIn(node.module.split('.')[0],forbidden)
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute):
                self.assertNotIn(node.func.attr,{'record_fill','correct_fill','place_order','modify_order','cancel_order'})


if __name__=='__main__': unittest.main()

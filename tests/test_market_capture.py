"""Fake-source bridge checks: no credential, network or account calls."""
import ast
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from banknifty.market_archive import MarketArchive, read_market_snapshot
from banknifty.market_capture import (CaptureConfig, MarketCaptureSession, ProviderObservation, QueueObservationSource, capture_readiness)
from banknifty.options_pipeline import run_pipeline
from banknifty.options_data import QualityPolicy
from tests.test_options_pipeline import NOW, underlying_fixture

EXPIRY = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
KNOWN = NOW-timedelta(minutes=11)
HEADER = 'exchange,exchange_token,trading_symbol,groww_symbol,instrument_type,segment,underlying_symbol,expiry_date,strike_price,lot_size,tick_size'
INDEX = 'NSE,26009,BANKNIFTY,NSE-BANKNIFTY,IDX,CASH,,,,,'
SYMBOLS = tuple(f'BANKNIFTY26OCT{strike}CE' for strike in range(54900,55500,100))
CATALOGUE = ('\n'.join([HEADER,INDEX]+[
    f'NSE,{60000+i},{symbol},NSE-BANKNIFTY-22Oct26-{54900+100*i}-CE,CE,FNO,BANKNIFTY,2026-10-22,{54900+100*i},25,0.05'
    for i,symbol in enumerate(SYMBOLS)])+'\n').encode()


def raw(value):return json.dumps(value,separators=(',',':')).encode()
def ms(value):return int(value.timestamp()*1000)
def catalogue(at=KNOWN, payload=CATALOGUE):return ProviderObservation('catalogue',payload,at)
def index(at=NOW, *, source_at=None, value='55180'):
    stamp=at if source_at is None else source_at
    return ProviderObservation('index_feed',raw({'NSE':{'CASH':{'26009':{'tsInMillis':ms(stamp),'value':value}}}}),at,'BANKNIFTY')
def depth(at=NOW, *, source_at=None, symbol=SYMBOLS[0], bid='99',ask='100'):
    token=str(60000+SYMBOLS.index(symbol));stamp=at if source_at is None else source_at
    return ProviderObservation('option_depth',raw({'NSE':{'FNO':{token:{'tsInMillis':ms(stamp),
        'buyBook':{'1':{'price':bid,'qty':1000}},'sellBook':{'1':{'price':ask,'qty':1000}}}}}}),at,symbol)


class FakeClock:
    def __init__(self):self.wall=KNOWN;self.mono=0
    def set(self,stamp):self.mono+=(stamp-self.wall).total_seconds();self.wall=stamp
    def now(self):return self.wall
    def monotonic(self):return self.mono
    def advance(self,seconds):self.set(self.wall+timedelta(seconds=seconds))


class FakeSource:
    def __init__(self,messages,clock):self.messages=iter(messages);self.clock=clock;self.calls=[]
    def read_next(self,*,timeout_seconds):
        self.calls.append(timeout_seconds)
        message=next(self.messages,None)
        if message is not None:
            # Recorded synthetic timestamps advance the fake wall clock;
            # delivery itself consumes one millisecond of fake elapsed time.
            before=self.clock.mono;self.clock.set(message.received_at);self.clock.mono=before+.001
        return message


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'archive.sqlite3';self.clock=FakeClock()
        self.config=CaptureConfig(enabled=True,duration_seconds=1200,evidence_kind='synthetic')

    def session(self,config=None):
        return MarketCaptureSession(self.path,config or self.config,reviewed_expiries={s:EXPIRY for s in SYMBOLS},
            clock=self.clock.now,monotonic=self.clock.monotonic)

    def capture(self,messages,config=None,**kwargs):
        return self.session(config).run(FakeSource(messages,self.clock),**kwargs)

    def snapshot(self,at=NOW):return read_market_snapshot(self.path,at,mode='observed')

    def test_disabled_never_reads_or_creates_archive(self):
        source=FakeSource([catalogue()],self.clock)
        self.assertEqual(self.session(CaptureConfig()).run(source).status,'inactive')
        self.assertEqual(source.calls,[]);self.assertFalse(self.path.exists())
        self.assertFalse(capture_readiness()['enabled_by_default'])

    def test_documented_payloads_archive_and_drive_shared_pipeline(self):
        messages=[catalogue()]
        messages.extend(index(row.received_at,value=str(row.value)) for row in underlying_fixture())
        messages.extend(depth(symbol=s,bid=str(99-i*10),ask=str(100-i*10),source_at=NOW-timedelta(seconds=1)) for i,s in enumerate(SYMBOLS))
        result=self.capture(messages)
        self.assertEqual(result.status,'completed');self.assertEqual(result.messages,127)
        self.assertFalse(result.live_verified);self.assertEqual(result.label,'SYNTHETIC FIXTURE CAPTURE')
        snapshot=self.snapshot();self.assertEqual((len(snapshot.contracts),len(snapshot.underlying),len(snapshot.quotes)),(6,120,6))
        decision=run_pipeline(snapshot.underlying,snapshot.contracts,snapshot.quotes,as_of=NOW,strategy='DEBIT_SPREAD')
        self.assertEqual(decision.status,'candidate');self.assertEqual(decision.label,'synthetic')
        self.assertFalse(decision.signal.trading_allowed)

    def test_exact_raw_lineage_and_actual_import_clock_are_separate(self):
        observation=index(NOW)
        source=FakeSource([catalogue(),observation],self.clock)
        original=source.read_next
        def delayed(**kwargs):
            message=original(**kwargs)
            if message is observation:self.clock.advance(2)
            return message
        source.read_next=delayed
        result=self.session().run(source)
        self.assertEqual(result.events[-1].received_at,NOW)
        self.assertEqual(result.events[-1].imported_at,NOW+timedelta(seconds=2))
        self.assertEqual(len(self.snapshot(NOW+timedelta(seconds=1)).underlying),0)
        with closing(sqlite3.connect(self.path)) as db:
            record=db.execute('SELECT raw_payload,header FROM market_manifests WHERE manifest_id=?',(result.events[-1].manifest_id,)).fetchone()
        self.assertEqual(record[0],observation.raw_payload)
        self.assertIn('receipt='+NOW.isoformat(),json.loads(record[1])['provider_reference'])

    def test_receipt_only_rest_quote_and_index_remain_blocked(self):
        quote=ProviderObservation('option_quote',raw({'status':'SUCCESS','payload':{'bid_price':99,'offer_price':100,
            'bid_quantity':1000,'offer_quantity':1000,'last_trade_time':ms(NOW),'open_interest':0}}),NOW,SYMBOLS[0])
        ltp=ProviderObservation('index_ltp',raw({'status':'SUCCESS','payload':{'NSE_BANKNIFTY':55180}}),NOW,'BANKNIFTY')
        result=self.capture([catalogue(),quote,ltp]);snapshot=self.snapshot()
        self.assertIsNone(snapshot.quotes[0].source_at);self.assertIsNone(snapshot.quotes[0].oi_source_at)
        self.assertEqual(snapshot.quotes[0].open_interest,0);self.assertIsNone(snapshot.underlying[0].source_at)
        self.assertFalse(result.events[-1].quality_accepted);self.assertFalse(result.events[-2].quality_accepted)
        self.assertEqual(run_pipeline(snapshot.underlying,snapshot.contracts,snapshot.quotes,as_of=NOW,strategy='DEBIT_SPREAD').status,'no_trade')

    def test_chain_mapping_never_invents_book_or_field_timestamps(self):
        message=ProviderObservation('option_chain',raw({'status':'SUCCESS','payload':{'underlying_ltp':55180,
            'strikes':{'54900':{'CE':{'trading_symbol':SYMBOLS[0],'ltp':100,'open_interest':200,'volume':20}}}}}),
            NOW,expiry_date=EXPIRY.date())
        result=self.capture([catalogue(),message]);snapshot=self.snapshot()
        self.assertEqual(result.status,'completed');self.assertIsNone(snapshot.quotes[0].bid)
        self.assertIsNone(snapshot.quotes[0].source_at);self.assertIsNone(snapshot.quotes[0].oi_source_at)
        self.assertFalse(result.events[-1].quality_accepted)

    def test_reconnect_duplicates_preserve_original_receipt_and_metadata(self):
        first=self.capture([catalogue(),depth(source_at=NOW-timedelta(seconds=1))]);old=self.snapshot()
        later=NOW+timedelta(seconds=1)
        second=self.capture([catalogue(later),depth(later,source_at=NOW-timedelta(seconds=1))])
        snapshot=self.snapshot(later)
        self.assertEqual(second.duplicates,7);self.assertEqual(second.inserted,0)
        self.assertEqual(snapshot.contracts,old.contracts)
        self.assertEqual(snapshot.quotes[0].received_at,NOW)
        self.assertEqual(len(snapshot.manifest_ids),len(old.manifest_ids)+2)

    def test_changed_catalogue_terms_stop_without_reinterpreting_old_quote(self):
        result=self.capture([catalogue(),catalogue(NOW,CATALOGUE.replace(b',25,0.05',b',35,0.05'))])
        self.assertEqual(result.status,'metadata_review_required')
        self.assertTrue(all(item.lot_size==25 for item in self.snapshot().contracts))
        self.assertEqual(len(self.snapshot().manifest_ids),1)

    def test_catalogue_and_explicit_expiry_are_required(self):
        self.assertEqual(self.capture([depth()]).status,'metadata_review_required');self.assertFalse(self.path.exists())
        self.clock=FakeClock()
        session=MarketCaptureSession(self.path,self.config,clock=self.clock.now,monotonic=self.clock.monotonic)
        self.assertEqual(session.run(FakeSource([catalogue()],self.clock)).status,'metadata_review_required')

    def test_source_conflict_is_quarantined_and_stops(self):
        result=self.capture([catalogue(),depth(),depth(NOW+timedelta(seconds=1),source_at=NOW,bid='98')])
        self.assertEqual(result.status,'quarantined')
        self.assertFalse(result.events[-1].quality_accepted)
        self.assertEqual(len(self.snapshot(NOW+timedelta(seconds=1)).quotes),1)
        self.assertEqual(self.snapshot(NOW).conflicts, ())
        self.assertEqual(len(self.snapshot(NOW+timedelta(seconds=1)).conflicts), 1)

    def test_review_conflicting_book_blocks_archive_pipeline_after_availability(self):
        messages = [catalogue()]
        messages.extend(index(row.received_at, value=str(row.value)) for row in underlying_fixture())
        messages.extend((depth(symbol=SYMBOLS[3], bid='69', ask='70', source_at=NOW-timedelta(seconds=1)),
                         depth(symbol=SYMBOLS[4], bid='59', ask='60', source_at=NOW-timedelta(seconds=1))))
        self.capture(messages)
        before = self.snapshot(NOW)
        decision = run_pipeline(before.underlying, before.contracts, before.quotes, as_of=NOW,
                                strategy='DEBIT_SPREAD', conflict_events=before.conflicts)
        self.assertEqual(decision.status, 'candidate')
        result = self.capture([catalogue(NOW+timedelta(seconds=1)),
            depth(NOW+timedelta(seconds=1), symbol=SYMBOLS[3], bid='68', ask='70', source_at=NOW-timedelta(seconds=1))])
        self.assertEqual(result.status, 'quarantined')
        self.assertEqual(self.snapshot(NOW).fingerprint, before.fingerprint)
        current = self.snapshot(NOW+timedelta(seconds=1))
        blocked = run_pipeline(current.underlying, current.contracts, current.quotes, as_of=NOW+timedelta(seconds=1),
                               strategy='DEBIT_SPREAD', conflict_events=current.conflicts)
        self.assertEqual(blocked.status, 'no_trade')
        self.assertTrue(any('conflict' in reason for reason in blocked.reasons))

    def _seal_lock_probe(self, lock_kind, *, stop=None):
        MarketArchive(self.path)
        ready, release, entering = threading.Event(), threading.Event(), threading.Event()
        outcomes = []
        cancellation = []
        original_seal, original_connect = MarketArchive.seal_batch, MarketArchive._connect
        def seal(archive, *args, **kwargs):
            ready.set()
            if not release.wait(5):
                raise RuntimeError('Fixture seal gate timed out')
            return original_seal(archive, *args, **kwargs)
        @contextmanager
        def traced(archive):
            with original_connect(archive) as db:
                db.set_trace_callback(lambda sql: entering.set() if sql == 'BEGIN EXCLUSIVE' else None)
                yield db
        def capture():
            try:
                outcomes.append(self.capture([catalogue()], cancelled=lambda: bool(cancellation)))
            except BaseException as error:
                outcomes.append(error)
        blocker = sqlite3.connect(self.path)
        thread = threading.Thread(target=capture)
        try:
            with patch.object(MarketArchive, 'seal_batch', seal), patch.object(MarketArchive, '_connect', traced):
                thread.start()
                self.assertTrue(ready.wait(5))
                if lock_kind == 'writer':
                    blocker.execute('BEGIN IMMEDIATE')
                else:
                    blocker.execute('BEGIN')
                    blocker.execute('SELECT * FROM market_records').fetchall()
                # Read before requesting EXCLUSIVE: once SQLite puts that
                # writer into PENDING, new readers correctly have to wait.
                cutoff = KNOWN + timedelta(seconds=1)
                before = self.snapshot(cutoff)
                self.assertEqual(before.contracts, ())
                release.set()
                self.assertTrue(entering.wait(5))
                self.assertTrue(thread.is_alive())
                self.clock.advance(1201 if stop == 'deadline' else 2)
                if stop == 'cancel':
                    cancellation.append(True)
                blocker.rollback()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcomes), 1)
            if isinstance(outcomes[0], BaseException):
                raise outcomes[0]
            result = outcomes[0]
            self.assertEqual(self.snapshot(cutoff).fingerprint, before.fingerprint)
            self.assertEqual(self.snapshot(cutoff).contracts, ())
            if stop is None:
                self.assertEqual(result.status, 'completed')
                self.assertEqual(result.events[0].imported_at, KNOWN + timedelta(seconds=2))
                self.assertEqual(len(self.snapshot(KNOWN + timedelta(seconds=2)).contracts), 6)
            else:
                self.assertEqual(result.status, 'cancelled' if stop == 'cancel' else 'duration_complete')
                self.assertEqual(self.snapshot(self.clock.now()).contracts, ())
        finally:
            release.set()
            blocker.rollback(); blocker.close()
            if thread.is_alive():
                thread.join(5)

    def test_review_publication_writer_lock_wait_does_not_backdate_availability(self):
        self._seal_lock_probe('writer')

    def test_publication_reader_lock_wait_does_not_backdate_availability(self):
        self._seal_lock_probe('reader')

    def test_publication_lock_wait_cancellation_keeps_data_unsealed(self):
        self._seal_lock_probe('reader', stop='cancel')

    def test_publication_lock_wait_deadline_keeps_data_unsealed(self):
        self._seal_lock_probe('writer', stop='deadline')

    def test_slow_cancellation_check_cannot_hide_expired_capture(self):
        session = self.session(replace(self.config, duration_seconds=5))
        session._gate(None)
        def slow_control():
            self.clock.advance(6)
            return False
        result = session.run(FakeSource([catalogue()], self.clock), cancelled=slow_control)
        self.assertEqual(result.status, 'duration_complete')
        self.assertFalse(self.path.exists())

    def test_stale_crossed_and_out_of_order_packets_never_report_quality_pass(self):
        result=self.capture([catalogue(),depth(),depth(NOW+timedelta(seconds=1),source_at=NOW-timedelta(seconds=1)),
            depth(NOW+timedelta(seconds=2),source_at=NOW-timedelta(minutes=2)),
            depth(NOW+timedelta(seconds=3),bid='101',ask='100')])
        self.assertEqual(result.status,'completed')
        self.assertIn('out_of_order_source',result.events[2].reasons)
        self.assertIn('stale_source',result.events[3].reasons)
        self.assertIn('crossed_book',result.events[4].reasons)
        self.assertTrue(all(not item.quality_accepted for item in result.events[2:]))

    def test_future_source_and_future_receipt_are_rejected(self):
        result=self.capture([catalogue(),depth(source_at=NOW+timedelta(milliseconds=500))])
        self.assertEqual(result.events[-1].status,'rejected');self.assertEqual(len(self.snapshot().quotes),0)
        self.clock=FakeClock();self.path=Path(self.tmp.name)/'future.sqlite3'
        class Source:
            def read_next(inner,**kwargs):return catalogue(NOW)
        result=self.session(replace(self.config,max_messages=1)).run(Source())
        self.assertEqual(result.events[0].status,'rejected');self.assertFalse(self.path.exists())

    def test_malformed_secret_and_oversized_payloads_are_not_archived(self):
        messages=[catalogue(),ProviderObservation('index_feed',b'{',NOW,'BANKNIFTY'),
            ProviderObservation('index_feed',b'{"api_key":"FAKE_TEST"}',NOW,'BANKNIFTY')]
        result=self.capture(messages)
        self.assertEqual([e.status for e in result.events],['accepted','rejected','rejected'])
        self.assertEqual(len(self.snapshot().manifest_ids),1)
        self.clock=FakeClock();self.path=Path(self.tmp.name)/'small.sqlite3'
        result=self.capture([catalogue()],replace(self.config,max_payload_bytes=5))
        self.assertEqual(result.events[0].status,'rejected');self.assertFalse(self.path.exists())

    def test_count_limits_and_no_automatic_session_restart(self):
        source=FakeSource([catalogue(),index()],self.clock);session=self.session(replace(self.config,max_messages=1))
        result=session.run(source);self.assertEqual(result.status,'message_limit')
        self.assertIs(session.run(source),result);self.assertEqual(len(source.calls),1)
        self.clock=FakeClock();self.path=Path(self.tmp.name)/'reads.sqlite3'
        self.assertEqual(self.capture([catalogue(),index()],replace(self.config,max_reads=1)).status,'read_limit')
        self.clock=FakeClock();self.path=Path(self.tmp.name)/'records.sqlite3'
        self.assertEqual(self.capture([catalogue()],replace(self.config,max_records=5)).status,'record_limit')
        self.assertFalse(self.path.exists())

    def test_cancel_and_late_source_response_never_append(self):
        stopped=[]
        class Source:
            def read_next(inner,**kwargs):stopped.append(True);return catalogue()
        result=self.session().run(Source(),cancelled=lambda:bool(stopped))
        self.assertEqual(result.status,'cancelled');self.assertFalse(self.path.exists())
        class Slow:
            def read_next(inner,**kwargs):self.clock.advance(6);return catalogue()
        self.assertEqual(self.session(replace(self.config,duration_seconds=5)).run(Slow()).status,'duration_complete')
        self.assertFalse(self.path.exists())

    def test_source_exceeding_its_requested_read_timeout_is_discarded(self):
        class Slow:
            def read_next(inner,**kwargs):self.clock.advance(2);return catalogue()
        result=self.session(replace(self.config,read_timeout_seconds=1)).run(Slow())
        self.assertEqual(result.status,'source_timeout');self.assertFalse(self.path.exists())

    def test_archive_preparation_cancel_or_deadline_blocks_insert(self):
        original=MarketArchive;cancelled=[]
        def preparing(*args,**kwargs):
            archive=original(*args,**kwargs);cancelled.append(True);return archive
        with patch('banknifty.market_capture.MarketArchive',side_effect=preparing):
            result=self.capture([catalogue()],cancelled=lambda:bool(cancelled))
        self.assertEqual(result.status,'cancelled')
        self.assertEqual(len(self.snapshot().manifest_ids),0)
        self.clock=FakeClock();self.path=Path(self.tmp.name)/'late.sqlite3'
        def slow(*args,**kwargs):
            archive=original(*args,**kwargs);self.clock.advance(6);return archive
        with patch('banknifty.market_capture.MarketArchive',side_effect=slow):
            result=self.capture([catalogue()],replace(self.config,duration_seconds=5))
        self.assertEqual(result.status,'duration_complete');self.assertEqual(len(self.snapshot().manifest_ids),0)

    def test_clock_regressions_and_source_errors_are_terminal_sanitized(self):
        class Regress:
            def read_next(inner,**kwargs):self.clock.mono-=1;return catalogue()
        self.assertEqual(self.session().run(Regress()).status,'clock_regression');self.assertFalse(self.path.exists())
        self.clock=FakeClock()
        class Fails:
            def read_next(inner,**kwargs):raise RuntimeError('FAKE_CREDENTIAL_DETAIL')
        result=self.session().run(Fails())
        self.assertEqual(result.status,'source_error');self.assertNotIn('FAKE',repr(result))
        self.assertFalse(self.path.exists())

    def test_no_transport_order_credentials_or_startup_interface(self):
        path=Path(__file__).resolve().parents[1]/'banknifty/market_capture.py'
        tree=ast.parse(path.read_text())
        forbidden={'requests','socket','urllib','http','growwapi','subprocess','os','keyring','groww'}
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):self.assertTrue(all(n.name.split('.')[0] not in forbidden for n in node.names))
            if isinstance(node,ast.ImportFrom):self.assertNotIn(node.module.split('.')[0],forbidden)
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute):
                self.assertNotIn(node.func.attr,{'place_order','modify_order','cancel_order','record_fill','getenv'})
        with self.assertRaises(ValueError):CaptureConfig(evidence_kind='live_verified')
        with self.assertRaises(ValueError):CaptureConfig(duration_seconds=28801)

    def test_callback_queue_is_bounded_by_count_and_bytes_and_stops_on_overflow(self):
        source=QueueObservationSource(maxsize=1,clock=self.clock.now)
        self.assertTrue(source.submit(catalogue()))
        self.assertFalse(source.submit(catalogue()))
        with self.assertRaises(BufferError):source.read_next(timeout_seconds=.01)
        source=QueueObservationSource(max_queued_bytes=5,clock=self.clock.now)
        self.assertFalse(source.submit(catalogue()))
        self.assertEqual(self.session().run(source).status,'source_error')
        self.assertFalse(self.path.exists())

    def test_callback_queue_explicit_close_and_timed_read(self):
        source=QueueObservationSource(clock=self.clock.now)
        with self.assertRaises(TimeoutError):source.read_next(timeout_seconds=.001)
        source.close();self.assertIsNone(source.read_next(timeout_seconds=.001))
        self.assertFalse(source.submit(catalogue()))
        with self.assertRaises(ValueError):source.read_next(timeout_seconds=31)

    def test_callback_snapshot_origin_is_explicit_and_receipt_comes_from_local_clock(self):
        source=QueueObservationSource(clock=self.clock.now)
        source.submit_raw('catalogue',CATALOGUE)
        self.clock.set(NOW)
        source.submit_mapping('index_feed',{'NSE':{'CASH':{'26009':{'tsInMillis':ms(NOW),'value':55180}}}},instrument_id='BANKNIFTY')
        source.close()
        result=self.session().run(source)
        self.assertEqual(result.status,'completed')
        self.assertEqual(result.events[-1].received_at,NOW)
        with closing(sqlite3.connect(self.path)) as db:
            header=json.loads(db.execute('SELECT header FROM market_manifests WHERE manifest_id=?',(result.events[-1].manifest_id,)).fetchone()[0])
        self.assertTrue(header['format'].endswith('callback_json'))
        with self.assertRaises(ValueError):
            QueueObservationSource().submit_mapping('index_feed',{'api_key':'FAKE_TEST'},instrument_id='BANKNIFTY')

    def test_import_timestamp_is_sampled_after_archive_preparation(self):
        original=MarketArchive
        def slow(*args,**kwargs):
            archive=original(*args,**kwargs);self.clock.advance(2);return archive
        with patch('banknifty.market_capture.MarketArchive',side_effect=slow):
            result=self.capture([catalogue()])
        self.assertEqual(result.events[0].imported_at,KNOWN+timedelta(seconds=2))
        self.assertEqual(len(self.snapshot(KNOWN+timedelta(seconds=1)).contracts),0)
        self.assertEqual(len(self.snapshot(KNOWN+timedelta(seconds=2)).contracts),6)

    def test_quality_is_rechecked_after_slow_archive_preparation(self):
        original=MarketArchive
        def slow(*args,**kwargs):
            archive=original(*args,**kwargs);self.clock.advance(6);return archive
        with patch('banknifty.market_capture.MarketArchive',side_effect=slow):
            result=self.capture([catalogue(),depth()])
        self.assertEqual(result.status,'completed')
        self.assertFalse(result.events[-1].quality_accepted)
        self.assertIn('stale_source',result.events[-1].reasons)
        self.assertEqual(result.events[-1].imported_at,NOW+timedelta(seconds=6))

    def test_slow_insert_rolls_back_before_commit(self):
        original=MarketArchive._connect;changed=[]
        @contextmanager
        def delayed(archive):
            with original(archive) as db:
                def trace(sql):
                    if sql.startswith('INSERT INTO market_manifests') and not changed:
                        changed.append(True);self.clock.advance(6)
                db.set_trace_callback(trace)
                yield db
        with patch.object(MarketArchive,'_connect',delayed):
            result=self.capture([catalogue()],replace(self.config,duration_seconds=5))
        self.assertEqual(result.status,'duration_complete');self.assertEqual(result.inserted,0)
        self.assertEqual(len(self.snapshot().manifest_ids),0)

    def test_failed_seal_is_invisible_until_a_later_successful_retry(self):
        with patch.object(MarketArchive,'seal_batch',side_effect=ValueError('fixture seal failure')):
            result=self.capture([catalogue()])
        self.assertEqual(result.status,'archive_error')
        self.assertEqual(result.events[-1].status,'unsealed');self.assertIsNone(result.events[-1].imported_at)
        self.assertEqual(len(self.snapshot().contracts),0)
        self.clock.set(KNOWN+timedelta(seconds=2))
        class Retry:
            def __init__(inner):inner.items=iter([catalogue(),None])
            def read_next(inner,**kwargs):return next(inner.items)
        recovered=self.session().run(Retry())
        self.assertEqual(recovered.status,'completed');self.assertEqual(recovered.duplicates,6)
        self.assertEqual(len(self.snapshot(KNOWN+timedelta(seconds=1)).contracts),0)
        self.assertEqual(len(self.snapshot(KNOWN+timedelta(seconds=2)).contracts),6)

    def test_completed_marker_prevents_backdating_during_slow_data_insert(self):
        original=MarketArchive._connect;changed=[]
        @contextmanager
        def delayed(archive):
            with original(archive) as db:
                def trace(sql):
                    if sql.startswith('INSERT INTO market_manifests') and not changed:
                        changed.append(True);self.clock.advance(2)
                db.set_trace_callback(trace)
                yield db
        with patch.object(MarketArchive,'_connect',delayed):result=self.capture([catalogue()])
        self.assertEqual(result.status,'completed')
        self.assertEqual(result.events[0].admitted_at,KNOWN)
        self.assertEqual(result.events[0].imported_at,KNOWN+timedelta(seconds=2))
        self.assertEqual(len(self.snapshot(KNOWN+timedelta(seconds=1)).contracts),0)
        self.assertEqual(len(self.snapshot(KNOWN+timedelta(seconds=2)).contracts),6)


if __name__=='__main__':unittest.main()

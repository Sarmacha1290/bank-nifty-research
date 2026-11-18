"""Synthetic archive and true read-only access acceptance tests."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from banknifty.market_archive import (MarketArchive, ImportManifest, ArchiveConflict, ArchiveError,
    ArchiveIntegrityError, read_market_snapshot, validate_archive)
from banknifty.options_data import ContractMetadata, OptionQuote, RawProvenance
from banknifty.options_pipeline import UnderlyingObservation
from banknifty.options_ledger import OptionsLedger, LedgerError, read_ledger

T = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)


def provenance(at=T, raw=b'{"market":"fixture"}', kind="replay_quote"):
    return RawProvenance("synthetic-test-source", raw, at, kind)


def contract():
    known = T - timedelta(days=1)
    return ContractMetadata("TEST-CALL", "TEST-INDEX", "TEST", "CALL", Decimal(1000),
        T + timedelta(days=7), 10, Decimal("0.05"), known, None, known, True, provenance(known))


def quote(at=T, **changes):
    receipt = at + timedelta(seconds=1)
    return replace(OptionQuote("TEST-CALL", at, receipt, Decimal(10), Decimal("10.05"),
        provenance(receipt), 100, 100, Decimal(10), None, None, 1), **changes)


def underlying(at=T, **changes):
    receipt = at + timedelta(seconds=1)
    return replace(UnderlyingObservation("TEST-INDEX", Decimal(1000), at, receipt, provenance(receipt), 1), **changes)


def manifest(at=None, raw=b'fixture manifest'):
    return ImportManifest("fixture-file", at or T + timedelta(seconds=10), raw)


class MarketArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "archive.sqlite3"
        self.archive = MarketArchive(self.path)

    def add(self, **overrides):
        values = dict(manifest=manifest(), contracts=(contract(),), quotes=(quote(),), underlying=(underlying(),))
        values.update(overrides)
        return self.archive.append_batch(**values)

    def test_typed_roundtrip_preserves_source_receipt_oi_and_raw_payload(self):
        self.assertEqual(self.add()["inserted"], 3)
        snapshot = self.archive.query(T + timedelta(seconds=10))
        self.assertEqual(snapshot.contracts, (contract(),))
        self.assertEqual(snapshot.quotes, (quote(),))
        self.assertEqual(snapshot.underlying, (underlying(),))
        self.assertIsNone(snapshot.quotes[0].open_interest)
        self.assertNotEqual(snapshot.quotes[0].source_at, snapshot.quotes[0].received_at)
        self.assertEqual(snapshot.quotes[0].provenance.payload, provenance().payload)

    def test_final_guard_holds_transaction_and_rejection_preserves_empty_archive(self):
        class StopCapture(Exception):
            pass
        calls = []
        def guard():
            with closing(sqlite3.connect(self.path, timeout=0)) as contender:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    contender.execute("BEGIN IMMEDIATE")
            calls.append("checked")
            raise StopCapture()
        with self.assertRaises(StopCapture):
            self.add(pre_commit=guard)
        self.assertEqual(calls, ["checked"])
        self.assertEqual(validate_archive(self.path)["records"], 0)
        self.assertEqual(self.archive.inventory()["manifest_count"], 0)
        self.assertEqual(self.add()["inserted"], 3)

    def test_final_guard_only_runs_for_new_import_including_quarantine(self):
        calls = []
        guard = lambda: calls.append("checked")
        self.add(pre_commit=guard)
        self.assertTrue(self.add(pre_commit=guard)["repeated_manifest"])
        self.assertEqual(calls, ["checked", "checked"])
        def reject():
            raise RuntimeError("Synthetic capture cancellation")
        with self.assertRaises(RuntimeError):
            self.add(manifest=manifest(T + timedelta(seconds=20), b'conflict'),
                     quotes=(quote(bid=Decimal(20)),), pre_commit=reject)
        self.assertEqual(self.archive.inventory()["manifest_count"], 1)
        self.assertEqual(validate_archive(self.path)["records"], 3)
        with self.assertRaisesRegex(ArchiveError, "callable"):
            self.add(pre_commit=False)

    def test_deadline_during_inserts_rolls_back_before_commit(self):
        expired = []
        original_connect = sqlite3.connect
        def connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            db.set_trace_callback(lambda sql: expired.append(True) if sql.startswith("INSERT INTO market_manifests") else None)
            return db
        def guard():
            if expired:
                raise RuntimeError("Synthetic capture deadline passed during INSERT")
        with patch("banknifty.market_archive.sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(RuntimeError, "deadline"):
                self.add(pre_commit=guard)
        self.assertTrue(expired)
        self.assertEqual(validate_archive(self.path)["records"], 0)
        self.assertEqual(self.archive.inventory()["manifest_count"], 0)

    def test_local_import_clock_is_sampled_after_transaction_lock(self):
        captured = T + timedelta(seconds=30)
        def clock():
            with closing(sqlite3.connect(self.path, timeout=0)) as contender:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    contender.execute("BEGIN IMMEDIATE")
            return captured
        result = self.add(imported_at_clock=clock)
        self.assertEqual(self.archive.query(T + timedelta(seconds=20)).quotes, ())
        self.assertEqual(self.archive.query(captured + timedelta(hours=1)).quotes, ())
        completed = captured + timedelta(seconds=3)
        self.archive.seal_batch(result["manifest_id"], completed_at=completed)
        self.assertEqual(self.archive.query(completed - timedelta(microseconds=1)).quotes, ())
        observed = self.archive.query(completed)
        self.assertEqual(observed.availability_by_fingerprint[quote().fingerprint], completed)
        self.assertEqual(datetime.fromisoformat(self.archive.inventory()["latest_imported_at"]), captured)
        self.assertEqual(validate_archive(self.path)["records"], 3)

    def test_delayed_data_insertion_needs_later_completion_marker(self):
        admission = T + timedelta(seconds=10)
        clock = [admission]
        original_connect = sqlite3.connect
        def connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            def trace(sql):
                if sql.startswith("INSERT INTO market_manifests"):
                    clock[0] = admission + timedelta(seconds=3)
            db.set_trace_callback(trace)
            return db
        with patch("banknifty.market_archive.sqlite3.connect", side_effect=connect):
            result = self.add(imported_at_clock=lambda: clock[0])
        self.assertEqual(self.archive.query(clock[0]).quotes, ())
        self.archive.seal_batch(result["manifest_id"], completed_at=clock[0])
        self.assertEqual(self.archive.query(admission + timedelta(seconds=1)).quotes, ())
        self.assertEqual(self.archive.query(clock[0]).availability_by_fingerprint[quote().fingerprint], clock[0])

    def test_failed_final_seal_guard_is_recoverable_and_repeat_cannot_refresh(self):
        result = self.add(imported_at_clock=lambda: T + timedelta(seconds=10))
        calls = []
        def fail_after_insert():
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError("Synthetic interruption before publication commit")
        with self.assertRaises(RuntimeError):
            self.archive.seal_batch(result["manifest_id"], completed_at=T + timedelta(seconds=20), pre_commit=fail_after_insert)
        self.assertEqual(validate_archive(self.path)["publication_markers"], 0)
        self.assertEqual(self.archive.query(T + timedelta(hours=1)).quotes, ())
        retry = self.add(imported_at_clock=lambda: T + timedelta(seconds=10))
        self.assertTrue(retry["repeated_manifest"])
        sealed = self.archive.seal_batch(retry["manifest_id"], completed_at=T + timedelta(seconds=30))
        repeated = self.archive.seal_batch(retry["manifest_id"], completed_at=T + timedelta(seconds=40))
        self.assertTrue(repeated["repeated_publication"])
        self.assertEqual(repeated["completed_at"], sealed["completed_at"])
        self.assertEqual(self.archive.query(T + timedelta(seconds=29)).quotes, ())
        self.assertEqual(self.archive.query(T + timedelta(seconds=40)).availability_by_fingerprint[quote().fingerprint], T + timedelta(seconds=30))

    def test_capture_duplicate_keeps_earliest_sealed_availability_and_marker_integrity(self):
        first = self.add(imported_at_clock=lambda: T + timedelta(seconds=10))
        self.archive.seal_batch(first["manifest_id"], completed_at=T + timedelta(seconds=12))
        second = self.add(imported_at_clock=lambda: T + timedelta(seconds=20))
        self.assertEqual(second["inserted"], 0)
        with self.assertRaises(ArchiveError):
            self.archive.seal_batch(second["manifest_id"], completed_at=T + timedelta(seconds=19))
        self.archive.seal_batch(second["manifest_id"], completed_at=T + timedelta(seconds=22))
        self.assertEqual(self.archive.query(T + timedelta(seconds=30)).availability_by_fingerprint[quote().fingerprint], T + timedelta(seconds=12))
        self.assertEqual(validate_archive(self.path)["publication_markers"], 2)
        with closing(sqlite3.connect(self.path)) as db, db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM market_publications")
            db.execute("DROP TRIGGER market_publications_no_update")
            db.execute("UPDATE market_publications SET sha256=?", ('0' * 64,))
            db.execute("CREATE TRIGGER market_publications_no_update BEFORE UPDATE ON market_publications BEGIN SELECT RAISE(ABORT, 'append-only market archive'); END")
        with self.assertRaises(ArchiveIntegrityError):
            validate_archive(self.path)
        with self.assertRaises(ArchiveIntegrityError):
            self.archive.query(T + timedelta(seconds=30))

    def test_import_clock_rejects_non_datetime_naive_and_backdated_values(self):
        self.add()
        for value in (T.isoformat(), T.replace(tzinfo=None), T):
            with self.subTest(value=value), self.assertRaises(ArchiveError):
                self.add(imported_at_clock=lambda: value)
        self.assertEqual(self.archive.inventory()["manifest_count"], 1)

    def test_observed_requires_actual_capture_reconstructed_is_explicit(self):
        self.add()
        observed = self.archive.query(T + timedelta(seconds=2))
        replay = self.archive.query(T + timedelta(seconds=2), mode="reconstructed")
        self.assertEqual(observed.quotes, ())
        self.assertEqual(len(replay.quotes), 1)
        self.assertIn("UNVERIFIED", replay.label)
        self.assertEqual(replay.mode, "reconstructed")
        self.assertEqual(replay.availability_by_fingerprint[quote().fingerprint], T + timedelta(seconds=10))
        with self.assertRaises(TypeError):
            replay.availability_by_fingerprint[quote().fingerprint] = T

    def test_future_source_receipt_capture_and_oi_fields_do_not_leak(self):
        for changes in ({"source_at": T + timedelta(seconds=3)},
                        {"received_at": T + timedelta(seconds=3)},
                        {"provenance": provenance(T + timedelta(seconds=3))},
                        {"oi_source_at": T + timedelta(seconds=3), "open_interest": 0}):
            with self.subTest(changes=changes):
                path = self.root / (str(len(list(self.root.iterdir()))) + ".sqlite3")
                archive = MarketArchive(path)
                archive.append_batch(manifest=manifest(), quotes=(quote(**changes),))
                self.assertEqual(archive.query(T + timedelta(seconds=2), mode="reconstructed").quotes, ())

    def test_missing_source_timestamp_remains_none_and_oi_zero_distinct(self):
        self.add(quotes=(quote(source_at=None, open_interest=0),))
        result = self.archive.query(T + timedelta(seconds=10))
        self.assertIsNone(result.quotes[0].source_at)
        self.assertEqual(result.quotes[0].open_interest, 0)
        self.assertNotIn("VERIFIED LIVE", result.label)

    def test_reconstructed_views_cannot_carry_verified_live_classification(self):
        records = (contract(), quote(), underlying())
        records = tuple(replace(item, provenance=replace(item.provenance, evidence_kind="live_verified")) for item in records)
        self.add(manifest=replace(manifest(), evidence_kind="live_verified"), contracts=records[:1], quotes=records[1:2], underlying=records[2:])
        snapshot = self.archive.query(T + timedelta(seconds=2), mode="reconstructed")
        for original, view in zip(records, snapshot.contracts + snapshot.quotes + snapshot.underlying):
            self.assertEqual(view.provenance.evidence_kind, "replay_quote")
            self.assertEqual(view.provenance.payload, original.provenance.payload)
            self.assertIn(view.fingerprint, snapshot.availability_by_fingerprint)
        observed = self.archive.query(T + timedelta(seconds=10))
        self.assertEqual(observed.quotes[0].provenance.evidence_kind, "live_verified")

    def test_repeat_manifest_idempotent_and_repackaging_separate_lineage(self):
        first = self.add()
        repeated = self.add()
        self.assertTrue(repeated["repeated_manifest"])
        second = self.add(manifest=manifest(T + timedelta(seconds=11), b'other source file packaging'))
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["duplicates"], 3)
        self.assertNotEqual(first["manifest_id"], second["manifest_id"])
        self.assertEqual(self.archive.inventory()["counts"]["option"], 1)
        sources = self.archive.manifests(include_raw=True)
        self.assertEqual(sources[1]["raw_payload"], b'other source file packaging')
        self.assertEqual(sources[0]["semantic_records"], sources[1]["semantic_records"])

    def test_duplicate_source_update_does_not_refresh_receipt_or_capture(self):
        self.add()
        later = quote(received_at=T + timedelta(seconds=7), provenance=provenance(T + timedelta(seconds=7)))
        result = self.add(manifest=manifest(T + timedelta(seconds=12), b'later duplicate'), quotes=(later,))
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(self.archive.query(T + timedelta(seconds=20)).quotes[0].received_at, quote().received_at)

    def test_conflicting_update_quarantines_whole_batch_without_rewrite(self):
        self.add()
        result = self.add(manifest=manifest(T + timedelta(seconds=12), b'conflicting raw'),
                          quotes=(quote(bid=Decimal(11)), quote(T + timedelta(seconds=5))))
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(self.archive.query(T + timedelta(seconds=20)).quotes, (quote(),))
        self.assertEqual(self.archive.inventory()["quarantined_manifests"], 1)
        self.assertEqual(self.archive.manifests(include_raw=True)[-1]["raw_payload"], b'conflicting raw')

    def test_conflicting_rows_inside_one_batch_are_quarantined(self):
        result = self.add(quotes=(quote(), quote(bid=Decimal(9))))
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(self.archive.inventory()["counts"], {"catalogue": 0, "option": 0, "underlying": 0})

    def test_conflict_fact_arrives_at_cutoff_without_rewriting_original_or_earlier_snapshot(self):
        self.add()
        before = self.archive.query(T + timedelta(seconds=19))
        conflict = quote(bid=Decimal(9), received_at=T + timedelta(seconds=20), provenance=provenance(T + timedelta(seconds=20)))
        self.add(manifest=manifest(T + timedelta(seconds=20), b'conflicting book'), quotes=(conflict,))
        self.assertEqual(self.archive.query(T + timedelta(seconds=19)).fingerprint, before.fingerprint)
        snapshot = self.archive.query(T + timedelta(seconds=20))
        self.assertEqual(snapshot.quotes, (quote(),))
        self.assertEqual(len(snapshot.conflicts), 1)
        fact = snapshot.conflicts[0]
        self.assertTrue(fact.matches(quote()))
        self.assertFalse(fact.matches(quote(T + timedelta(seconds=1))))
        self.assertEqual(fact.available_at, T + timedelta(seconds=20))
        self.assertEqual(ArchiveConflict.from_dict(fact.to_dict()), fact)
        self.assertNotEqual(snapshot.fingerprint, before.fingerprint)
        self.assertEqual(validate_archive(self.path)["quarantined_manifests"], 1)

    def test_capture_quarantine_requires_conflict_publication_and_never_admits_price(self):
        self.add()
        result = self.add(imported_at_clock=lambda: T + timedelta(seconds=20), quotes=(quote(bid=Decimal(9)),))
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(self.archive.query(T + timedelta(seconds=30)).conflicts, ())
        self.archive.seal_batch(result["manifest_id"], completed_at_clock=lambda: T + timedelta(seconds=32))
        self.assertEqual(self.archive.query(T + timedelta(seconds=31)).conflicts, ())
        snapshot = self.archive.query(T + timedelta(seconds=32))
        self.assertTrue(snapshot.conflicts[0].matches(quote()))
        self.assertEqual(snapshot.conflicts[0].available_at, T + timedelta(seconds=32))
        self.assertEqual(snapshot.quotes, (quote(),))
        self.assertEqual(validate_archive(self.path)["publication_markers"], 1)

    def test_intrabatch_and_reconstructed_conflicts_keep_claimed_chronology(self):
        self.add(quotes=(quote(), quote(bid=Decimal(9))))
        snapshot = self.archive.query(T + timedelta(seconds=1), mode="reconstructed")
        self.assertEqual(snapshot.quotes, ())
        self.assertEqual(len(snapshot.conflicts), 1)
        self.assertEqual(snapshot.conflicts[0].available_at, T + timedelta(seconds=1))
        self.assertEqual(self.archive.query(T + timedelta(seconds=1)).conflicts, ())

    def test_conflicts_cover_underlying_and_catalogue_identities(self):
        self.add()
        changed_contract = replace(contract(), lot_size=20)
        changed_underlying = replace(underlying(), value=Decimal(999))
        self.add(manifest=manifest(T + timedelta(seconds=20), b'conflicting metadata and index'),
                 contracts=(changed_contract,), underlying=(changed_underlying,))
        facts = self.archive.query(T + timedelta(seconds=20)).conflicts
        self.assertEqual({fact.kind for fact in facts}, {"catalogue", "underlying"})
        self.assertTrue(any(fact.matches(contract()) for fact in facts))
        self.assertTrue(any(fact.matches(underlying()) for fact in facts))

    def test_future_append_does_not_change_earlier_snapshot_fingerprint(self):
        self.add()
        before = self.archive.query(T + timedelta(seconds=10))
        self.add(manifest=manifest(T + timedelta(minutes=10), b'future'),
                 contracts=(replace(contract(), lot_size=20, known_at=T + timedelta(minutes=5),
                     effective_from=T + timedelta(minutes=5), provenance=provenance(T + timedelta(minutes=5))),),
                 quotes=(quote(T + timedelta(minutes=5)),), underlying=(underlying(T + timedelta(minutes=5)),))
        after = self.archive.query(T + timedelta(seconds=10))
        self.assertEqual(before, after)
        self.assertEqual(before.fingerprint, after.fingerprint)

    def test_date_versions_retained_without_retroactive_metadata_rewrite(self):
        first = contract()
        later = replace(first, lot_size=20, effective_from=T + timedelta(days=1), known_at=T + timedelta(hours=1),
                        provenance=provenance(T + timedelta(hours=1)))
        self.add()
        self.add(manifest=manifest(T + timedelta(hours=2), b'new lot catalogue'), contracts=(later,), quotes=(), underlying=())
        earlier = self.archive.query(T + timedelta(seconds=10))
        future = self.archive.query(T + timedelta(days=2))
        self.assertEqual(earlier.contracts, (first,))
        self.assertEqual([item.lot_size for item in future.contracts], [10, 20])
        self.assertIsNone(future.contracts[0].effective_until)

    def test_start_window_preserves_older_known_metadata(self):
        self.add()
        result = self.archive.query(T + timedelta(seconds=10), start_at=T)
        self.assertEqual(len(result.contracts), 1)
        self.assertEqual(len(result.quotes), 1)
        self.assertEqual(self.archive.dates(), ("2026-01-05",))

    def test_backdated_actual_import_rejected(self):
        self.add()
        with self.assertRaisesRegex(ArchiveError, "backdated"):
            self.add(manifest=manifest(T, b'backdated'))

    def test_readonly_queries_cannot_create_or_write_any_schema(self):
        self.add()
        before, names = self.path.read_bytes(), sorted(item.name for item in self.root.iterdir())
        with patch("banknifty.market_archive.sqlite3.connect", wraps=sqlite3.connect) as connect:
            archive = MarketArchive(self.path, read_only=True)
            self.assertEqual(len(archive.query(T + timedelta(seconds=10)).quotes), 1)
            self.assertTrue(all(call.kwargs.get("uri") for call in connect.call_args_list))
            self.assertTrue(all("mode=ro" in call.args[0] for call in connect.call_args_list))
        with self.assertRaisesRegex(ArchiveError, "Read-only"):
            archive.append_batch(manifest=manifest(), quotes=(quote(),))
        missing = self.root / "absent" / "archive.sqlite3"
        with self.assertRaises(ArchiveError):
            MarketArchive(missing, read_only=True)
        self.assertFalse(missing.parent.exists())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), names)

    def test_append_only_database_triggers_and_tamper_detection(self):
        self.add()
        with closing(sqlite3.connect(self.path)) as db, db:
            for table in ("market_records", "market_manifests", "market_lineage"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("DELETE FROM " + table)
            db.execute("DROP TRIGGER market_records_no_update")
            db.execute("UPDATE market_records SET received_at='2000-01-01T00:00:00.000000+00:00' WHERE kind='option'")
        with self.assertRaises(ArchiveIntegrityError):
            read_market_snapshot(self.path, T + timedelta(seconds=20))

    def test_full_raw_manifest_tamper_is_detected(self):
        self.add()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TRIGGER market_manifests_no_update")
            db.execute("UPDATE market_manifests SET raw_payload=x'00'")
        with self.assertRaises(ArchiveIntegrityError):
            self.archive.query(T + timedelta(seconds=20))

    def test_large_batch_checks_each_full_manifest_once_per_query(self):
        self.add(quotes=tuple(quote(T + timedelta(seconds=i)) for i in range(30)))
        with patch.object(self.archive, "_verify_manifest", wraps=self.archive._verify_manifest) as verify:
            self.archive.query(T + timedelta(seconds=40))
            self.assertEqual(verify.call_count, 1)

    def test_query_limit_is_applied_before_hydration(self):
        self.add(quotes=tuple(quote(T + timedelta(seconds=i)) for i in range(5)))
        statements, connect = [], sqlite3.connect
        def traced(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch("banknifty.market_archive.MAX_RECORDS", 3), patch("banknifty.market_archive.sqlite3.connect", side_effect=traced), patch.object(self.archive, "_verify_record") as verify:
            with self.assertRaisesRegex(ArchiveError, "smaller interval"):
                self.archive.query(T + timedelta(seconds=20))
            verify.assert_not_called()
        self.assertTrue(any("FROM market_records WHERE" in sql and "LIMIT 4" in sql for sql in statements))

    def test_full_backup_validation_includes_quarantine_and_exact_schema(self):
        self.add()
        self.add(manifest=manifest(T + timedelta(seconds=20), b'quarantined'), quotes=(quote(bid=Decimal(20)),))
        before = self.path.read_bytes()
        result = validate_archive(self.path)
        self.assertTrue(result["valid"])
        self.assertEqual((result["records"], result["manifests"], result["quarantined_manifests"]), (3, 2, 1))
        self.assertEqual(self.path.read_bytes(), before)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE unrelated_private_fixture(value)")
        with self.assertRaises(ArchiveIntegrityError):
            validate_archive(self.path)

    def test_full_backup_validation_rejects_extra_view_and_missing_trigger(self):
        self.add()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE VIEW extra_fixture AS SELECT 1")
        with self.assertRaisesRegex(ArchiveIntegrityError, "schema"):
            validate_archive(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP VIEW extra_fixture")
            db.execute("DROP TRIGGER market_lineage_no_delete")
        with self.assertRaises(ArchiveIntegrityError):
            validate_archive(self.path)

    def test_quarantined_raw_tamper_is_rejected(self):
        self.add()
        self.add(manifest=manifest(T + timedelta(seconds=20), b'quarantined'), quotes=(quote(bid=Decimal(20)),))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TRIGGER market_manifests_no_update")
            db.execute("UPDATE market_manifests SET raw_payload=x'00' WHERE status='quarantined'")
            db.execute("CREATE TRIGGER market_manifests_no_update BEFORE UPDATE ON market_manifests BEGIN SELECT RAISE(ABORT, 'append-only market archive'); END")
        with self.assertRaises(ArchiveIntegrityError):
            validate_archive(self.path)

    def test_missing_lineage_is_rejected_after_schema_is_restored(self):
        self.add()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TRIGGER market_lineage_no_delete")
            db.execute("DELETE FROM market_lineage WHERE record_id=(SELECT record_id FROM market_lineage LIMIT 1)")
            db.execute("CREATE TRIGGER market_lineage_no_delete BEFORE DELETE ON market_lineage BEGIN SELECT RAISE(ABORT, 'append-only market archive'); END")
        with self.assertRaisesRegex(ArchiveIntegrityError, "missing declared records/lineage"):
            validate_archive(self.path)

    def test_backup_validation_enforces_file_and_record_bounds(self):
        self.add()
        with patch("banknifty.market_archive.MAX_BACKUP_BYTES", 1):
            with self.assertRaisesRegex(ArchiveError, "64 MiB"):
                validate_archive(self.path)
        with patch("banknifty.market_archive.MAX_RECORDS", 1):
            with self.assertRaisesRegex(ArchiveError, "limits"):
                validate_archive(self.path)

    def test_normalized_v2_import_underlying_options_and_catalogue(self):
        from banknifty.options_workspace import sample_import_document
        document = sample_import_document(T)
        document["schema_version"] = 2
        document["underlying"] = [{"symbol": "SYNTH-BANKNIFTY", "value": "50000", "source_at": T.isoformat(),
                                    "received_at": T.isoformat(), "sequence": 1}]
        source = self.root / "v2.json"
        source.write_text(json.dumps(document), encoding="utf-8")
        result = self.archive.import_file(source, imported_at=T + timedelta(seconds=10))
        self.assertEqual(result["status"], "accepted")
        snapshot = self.archive.query(T + timedelta(seconds=10))
        self.assertEqual((len(snapshot.contracts), len(snapshot.quotes), len(snapshot.underlying)), (1, 1, 1))
        self.assertTrue(all(item.provenance.evidence_kind == "replay_quote" for item in snapshot.quotes))

    def test_normalized_v2_underlying_only_and_schema1_supported(self):
        source = self.root / "underlying.json"
        source.write_text(json.dumps({"schema_version": 2, "underlying": [{"symbol": "X", "value": "1", "source_at": None,
            "received_at": T.isoformat()}]}))
        self.assertEqual(self.archive.import_file(source, imported_at=T)["inserted"], 1)
        from banknifty.options_workspace import sample_import_document
        source.write_text(json.dumps(sample_import_document(T)))
        self.assertEqual(self.archive.import_file(source, imported_at=T)["status"], "accepted")

    def test_malformed_credentials_oversized_and_live_promotion_rejected(self):
        for raw in (b'{"token":"fixture-only"}', b'access_token,value\nx,1'):
            with self.assertRaises(ArchiveError):
                manifest(raw=raw)
        with self.assertRaises(ArchiveError):
            self.add(quotes=(quote(provenance=provenance(kind="live_verified")),))
        source = self.root / "bad.json"
        for raw in ('[]', '{"schema_version":2,"schema_version":2}', '{"schema_version":2,"underlying":[{"value":"NaN"}]}'):
            source.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.archive.import_file(source, imported_at=T)
        self.assertEqual(self.archive.inventory()["manifest_count"], 0)

    def test_unrelated_database_not_modified(self):
        target = self.root / "other.sqlite3"
        with closing(sqlite3.connect(target)) as db, db:
            db.execute("CREATE TABLE unrelated(value)")
        before = target.read_bytes()
        with self.assertRaises(ArchiveError):
            MarketArchive(target)
        self.assertEqual(target.read_bytes(), before)


class ReadOnlyLedgerTests(unittest.TestCase):
    def test_invalid_database_is_reported_without_writing_or_empty_snapshot(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "invalid.sqlite3"
            path.write_bytes(b"invalid synthetic database fixture")
            before = path.read_bytes()
            with self.assertRaises(LedgerError):
                read_ledger(path)
            self.assertEqual(path.read_bytes(), before)

    def test_read_ledger_missing_path_creates_nothing(self):
        with TemporaryDirectory() as folder:
            missing = Path(folder) / "missing" / "ledger.sqlite3"
            self.assertFalse(read_ledger(missing)["available"])
            self.assertFalse(missing.parent.exists())

    def test_read_ledger_uses_readonly_uri_and_returns_validated_snapshot(self):
        from tests.test_options_ledger import candidate, T0
        with TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.sqlite3"
            ledger = OptionsLedger(path)
            ledger.append_signal("s1", candidate(), recorded_at=T0)
            before = path.read_bytes()
            with patch("banknifty.options_ledger.sqlite3.connect", wraps=sqlite3.connect) as connect:
                result = read_ledger(path)
                self.assertTrue(all(call.kwargs.get("uri") and "mode=ro" in call.args[0] for call in connect.call_args_list))
            self.assertEqual(result["exposures"]["s1"]["status"], "unfilled")
            self.assertEqual(result["last_sha256"], result["events"][-1]["sha256"])
            self.assertEqual(path.read_bytes(), before)
            readonly = OptionsLedger(path, read_only=True)
            with self.assertRaisesRegex(LedgerError, "Read-only"):
                readonly.append_signal("s1", candidate(), recorded_at=T0)


if __name__ == "__main__":
    unittest.main()

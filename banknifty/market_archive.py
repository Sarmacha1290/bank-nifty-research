"""Immutable normalized market corpus with explicit observation/replay cutoffs.

No transport, credential, broker or order dependency is accepted. Full source
bytes and actual import times belong to immutable manifests. Semantic record
identities are separate: repackaging a file cannot rewrite earlier observations.
An explicit reconstructed query trusts claimed historical chronology only for
unverified replay; default observed queries also enforce actual local import time.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Any

from .options_data import ContractMetadata, OptionQuote, RawProvenance, EVIDENCE_KINDS

SCHEMA_VERSION = 1
MAX_BYTES = 20 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_BACKUP_BYTES = 64 * 1024 * 1024
MAX_MANIFESTS = 1000
_TABLES = {"market_manifests", "market_records", "market_lineage", "market_publications"}
_SECRET_KEYS = {"token", "access_token", "api_key", "api_secret", "password", "authorization", "credentials", "secret", "private_key"}


class ArchiveError(ValueError):
    pass


class ArchiveIntegrityError(ArchiveError):
    pass


def _time(value: Any, *, optional=False) -> str | None:
    if value is None and optional:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, TypeError, AttributeError, OverflowError) as error:
        raise ArchiveError("Market timestamps must include a timezone") from error


def _dt(value):
    return None if value is None else datetime.fromisoformat(value)


def _text(value, *, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ArchiveError("Invalid bounded market identifier")
    return value


def _decimal(value, *, optional=False, positive=False):
    if value is None and optional:
        return None
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if (not result.is_finite() or result < 0 or (positive and result == 0) or result > Decimal("1e15")
                or len(result.as_tuple().digits) > 50 or abs(result.as_tuple().exponent) > 50):
            raise ValueError
        return result
    except (ValueError, TypeError, InvalidOperation) as error:
        raise ArchiveError("Invalid finite market decimal") from error


def _integer(value, *, optional=False, positive=False):
    if value is None and optional:
        return None
    if type(value) is not int or value < (1 if positive else 0) or value > 10**15:
        raise ArchiveError("Invalid market integer")
    return value


def _jsonable(value):
    if isinstance(value, datetime):
        return _time(value)
    if isinstance(value, Decimal):
        number = _decimal(value)
        if number == 0:
            return "0"
        text = format(number, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, bytes):
        return {"encoding": "hex", "data": value.hex()}
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        if any(not isinstance(key, str) or key.lower().replace("-", "_") in _SECRET_KEYS for key in value):
            raise ArchiveError("Credential-labelled fields are forbidden in market archives")
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    raise ArchiveError("Only finite normalized market payloads are accepted")


def _canonical(value):
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _hash(value):
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _check_raw(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ArchiveError("Raw market material must be bytes at most 20 MiB")
    # Guard labelled credentials in JSON and CSV headers; not a secret scanner.
    try:
        parsed = json.loads(raw.decode("utf-8-sig"), parse_float=str)
    except RecursionError as error:
        raise ArchiveError("Market material nesting exceeds limits") from error
    except (UnicodeError, ValueError):
        header = raw.splitlines()[0].decode("utf-8", errors="ignore") if raw else ""
        if any(field.strip().strip('"').lower() in _SECRET_KEYS for field in header.split(",")):
            raise ArchiveError("Credential columns are forbidden in market archives")
    else:
        _jsonable(parsed)


@dataclass(frozen=True)
class ImportManifest:
    source: str
    imported_at: datetime
    raw_payload: bytes
    format: str = "normalized-json"
    schema_version: int = 1
    evidence_kind: str = "replay_quote"
    provider_reference: str | None = None

    def __post_init__(self):
        _text(self.source)
        _time(self.imported_at)
        _text(self.format)
        _check_raw(self.raw_payload)
        if self.schema_version != 1 or type(self.schema_version) is not int:
            raise ArchiveError("Unsupported import manifest schema version")
        if self.evidence_kind not in EVIDENCE_KINDS:
            raise ArchiveError("Unsupported manifest evidence kind")
        if self.provider_reference is not None:
            _text(self.provider_reference, limit=512)


@dataclass(frozen=True)
class ArchiveConflict:
    """Immutable contradiction, never an executable price or replacement row."""
    kind: str
    symbol: str
    source: str
    evidence_kind: str
    identity_key: str
    source_at: datetime | None
    received_at: datetime
    sequence: int | None
    available_at: datetime
    manifest_id: str
    existing_record_id: str
    incoming_record_id: str
    effective_from: datetime | None = None
    known_at: datetime | None = None

    def __post_init__(self):
        if self.kind not in {"catalogue", "option", "underlying"} or self.evidence_kind not in EVIDENCE_KINDS:
            raise ArchiveError("Invalid conflict kind/evidence")
        _text(self.symbol); _text(self.source)
        for name in ("identity_key", "manifest_id", "existing_record_id", "incoming_record_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ArchiveError("Invalid conflict identity")
        for name in ("received_at", "available_at"):
            _time(getattr(self, name))
        for name in ("source_at", "effective_from", "known_at"):
            _time(getattr(self, name), optional=True)
        _integer(self.sequence, optional=True)

    @property
    def fingerprint(self):
        return _hash(self)

    def to_dict(self):
        return _jsonable(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            raise ArchiveError("Invalid conflict schema")
        data = dict(value)
        for name in ("source_at", "received_at", "available_at", "effective_from", "known_at"):
            data[name] = _dt(_time(data[name], optional=name not in {"received_at", "available_at"}))
        return cls(**data)

    def matches(self, record):
        kind = "catalogue" if isinstance(record, ContractMetadata) else "option" if isinstance(record, OptionQuote) else "underlying"
        try:
            evidence = record.provenance.evidence_kind
            evidence_matches = evidence == self.evidence_kind or (evidence == "replay_quote" and self.evidence_kind.startswith("live_"))
            if (kind != self.kind or getattr(record, "contract_id", getattr(record, "symbol", None)) != self.symbol
                    or record.provenance.source != self.source or not evidence_matches):
                return False
            if kind == "catalogue":
                return record.effective_from == self.effective_from and record.known_at == self.known_at
            return (record.source_at == self.source_at and record.sequence == self.sequence
                    and (self.source_at is not None or record.received_at == self.received_at))
        except AttributeError:
            return False


@dataclass(frozen=True)
class ArchiveSnapshot:
    contracts: tuple
    quotes: tuple
    underlying: tuple
    manifest_ids: tuple[str, ...]
    label: str
    as_of: datetime
    mode: str
    record_fingerprints: tuple[str, ...]
    availability: tuple[tuple[str, datetime], ...] = ()
    conflicts: tuple[ArchiveConflict, ...] = ()

    @property
    def availability_by_fingerprint(self):
        """Actual first local import times, distinct from claimed row receipt."""
        return MappingProxyType(dict(self.availability))

    @property
    def fingerprint(self):
        return _hash({"records": self.record_fingerprints, "as_of": self.as_of, "mode": self.mode,
                      "actual_availability": self.availability if self.mode == "observed" else (),
                      "conflicts": tuple(item.fingerprint for item in self.conflicts)})


def _record(item):
    from .options_pipeline import UnderlyingObservation
    if isinstance(item, ContractMetadata):
        kind, symbol = "catalogue", _text(item.contract_id)
    elif isinstance(item, OptionQuote):
        kind, symbol = "option", _text(item.contract_id)
    elif isinstance(item, UnderlyingObservation):
        kind, symbol = "underlying", _text(item.symbol)
    else:
        raise ArchiveError("Use typed catalogue, option quote or underlying observations")
    provenance = item.provenance
    if not isinstance(provenance, RawProvenance):
        raise ArchiveError("Each market record requires raw provenance")
    _check_raw(provenance.payload)
    source, captured = _text(provenance.source), _time(provenance.captured_at)
    payload = _jsonable(item)
    if kind == "catalogue":
        for field in ("underlying", "exchange"):
            _text(getattr(item, field))
        if item.option_type not in {"CALL", "PUT"} or type(item.listed) is not bool:
            raise ArchiveError("Invalid catalogue right/listing claim")
        _decimal(item.strike, positive=True); _decimal(item.tick_size, positive=True)
        _integer(item.lot_size, positive=True)
        known, effective = _time(item.known_at), _time(item.effective_from)
        expiry, until = _time(item.expiry_at), _time(item.effective_until, optional=True)
        if effective >= expiry or (until is not None and until <= effective):
            raise ArchiveError("Invalid catalogue validity interval")
        source_at, receipt = None, known
        identity = (kind, source, provenance.evidence_kind, symbol, effective, known)
    else:
        source_at, receipt = _time(item.source_at, optional=True), _time(item.received_at)
        known = receipt
        _integer(item.sequence, optional=True)
        if kind == "underlying":
            _decimal(item.value, positive=True)
        else:
            for field in ("bid", "ask", "last"):
                _decimal(getattr(item, field), optional=True)
            for field in ("bid_size", "ask_size", "open_interest"):
                _integer(getattr(item, field), optional=True)
            _time(item.oi_source_at, optional=True)
        identity = (kind, source, provenance.evidence_kind, symbol, source_at or receipt, item.sequence)
    semantic = {key: value for key, value in payload.items() if key != "provenance"}
    # Same source update received again is a duplicate, not a fresh observation.
    # Keep its original archived receipt/capture; later manifests retain new raw
    # file provenance without replacing the frozen observation.
    if kind != "catalogue" and source_at is not None:
        semantic.pop("received_at", None)
    semantic = dict(semantic, _source=source, _evidence_kind=provenance.evidence_kind)
    return {"record_id": _hash(semantic), "identity_key": _hash(identity), "kind": kind,
            "symbol": symbol, "source_at": source_at, "received_at": receipt,
            "known_at": known, "captured_at": captured, "source": source,
            "evidence_kind": provenance.evidence_kind, "payload": _canonical(payload),
            "payload_sha256": _hash(payload)}


def _hydrate(kind, raw):
    data = json.loads(raw)
    provenance = data.pop("provenance")
    content = provenance["payload"]
    if content.get("encoding") != "hex":
        raise ArchiveIntegrityError("Unsupported raw provenance encoding")
    data["provenance"] = RawProvenance(provenance["source"], bytes.fromhex(content["data"]),
        _dt(provenance["captured_at"]), provenance["evidence_kind"])
    if kind == "catalogue":
        for field in ("expiry_at", "effective_from", "effective_until", "known_at"):
            data[field] = _dt(data[field])
        for field in ("strike", "tick_size"):
            data[field] = Decimal(data[field])
        return ContractMetadata(**data)
    for field in ("source_at", "received_at"):
        data[field] = _dt(data[field])
    if kind == "option":
        data["oi_source_at"] = _dt(data["oi_source_at"])
        for field in ("bid", "ask", "last"):
            data[field] = None if data[field] is None else Decimal(data[field])
        return OptionQuote(**data)
    if kind == "underlying":
        from .options_pipeline import UnderlyingObservation
        data["value"] = Decimal(data["value"])
        return UnderlyingObservation(**data)
    raise ArchiveIntegrityError("Unsupported archived record kind")


class MarketArchive:
    """Append-only SQLite corpus. read_only=True never creates or changes schema."""
    def __init__(self, path, *, read_only=False):
        if type(read_only) is not bool:
            raise ArchiveError("read_only must be boolean")
        self.path, self.read_only = Path(path).resolve(), read_only
        if read_only:
            if not self.path.is_file():
                raise ArchiveError("Market archive does not exist")
            with self._connect() as db:
                self._check_schema(db)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if existing and existing not in (_TABLES, _TABLES - {"market_publications"}):
                raise ArchiveError("Use a separate market archive file")
            db.execute("""CREATE TABLE IF NOT EXISTS market_manifests (
                manifest_id TEXT PRIMARY KEY, header TEXT NOT NULL, raw_payload BLOB NOT NULL,
                raw_sha256 TEXT NOT NULL, imported_at TEXT NOT NULL, status TEXT NOT NULL,
                result_json TEXT NOT NULL, sha256 TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS market_records (
                record_id TEXT PRIMARY KEY, identity_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, symbol TEXT NOT NULL, source_at TEXT, received_at TEXT NOT NULL,
                known_at TEXT NOT NULL, captured_at TEXT NOT NULL, source TEXT NOT NULL,
                evidence_kind TEXT NOT NULL, payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                first_imported_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS market_lineage (
                manifest_id TEXT NOT NULL, record_id TEXT NOT NULL, PRIMARY KEY(manifest_id, record_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS market_publications (
                manifest_id TEXT PRIMARY KEY, completed_at TEXT NOT NULL, sha256 TEXT NOT NULL)""")
            for name in _TABLES:
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS {name}_no_{operation.lower()} BEFORE {operation} ON {name} BEGIN SELECT RAISE(ABORT, 'append-only market archive'); END")
            db.execute("CREATE INDEX IF NOT EXISTS market_receipt_window ON market_records(kind, received_at, symbol)")
            db.execute("CREATE INDEX IF NOT EXISTS market_known_window ON market_records(kind, known_at, symbol)")
            db.execute("CREATE INDEX IF NOT EXISTS market_lineage_record ON market_lineage(record_id)")
            # A covering index avoids repeatedly traversing large raw/header BLOB
            # overflow pages merely to obtain a manifest's timestamp and status.
            db.execute("CREATE INDEX IF NOT EXISTS market_manifest_lookup ON market_manifests(manifest_id,imported_at,status)")

    @contextmanager
    def _connect(self):
        connection = None
        try:
            connection = (sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
                          if self.read_only else sqlite3.connect(self.path, timeout=10))
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection
        except sqlite3.DatabaseError as error:
            raise ArchiveIntegrityError("Market archive database could not be read or committed") from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _check_schema(db):
        found = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if found != _TABLES:
            raise ArchiveIntegrityError("Unsupported market archive schema")

    def append_batch(self, *, manifest: ImportManifest, contracts=(), quotes=(), underlying=(), pre_commit=None,
                     imported_at_clock=None):
        """Append one atomic import, with an optional final zero-argument guard.

        The guard runs after transaction acquisition, validation and manifest
        serialization, immediately before the first INSERT, and again after all
        INSERTs immediately before commit. It must tolerate repeated checks.
        Raising aborts the entire batch. Exact repeats need no INSERT or guard.
        A trusted local ``imported_at_clock`` may supply an aware timestamp after
        transaction acquisition. Such capture batches require ``seal_batch``
        after this transaction returns before observed queries may admit them.
        """
        if self.read_only:
            raise ArchiveError("Read-only archive cannot append")
        if pre_commit is not None and not callable(pre_commit):
            raise ArchiveError("pre_commit must be callable")
        if imported_at_clock is not None and not callable(imported_at_clock):
            raise ArchiveError("imported_at_clock must be callable")
        if not isinstance(manifest, ImportManifest):
            raise ArchiveError("An explicit import manifest is required")
        items = tuple(contracts) + tuple(quotes) + tuple(underlying)
        if not items or len(items) > MAX_RECORDS:
            raise ArchiveError("An archive batch requires 1-100000 normalized records")
        normalized = [_record(item) for item in items]
        normalized.sort(key=lambda row: (row["received_at"], row["captured_at"], row["identity_key"], row["record_id"]))
        if manifest.evidence_kind != "synthetic" and any(row["evidence_kind"] == "synthetic" for row in normalized):
            raise ArchiveError("Synthetic records require an explicitly synthetic manifest")
        if manifest.evidence_kind == "replay_quote" and any(row["evidence_kind"].startswith("live_") for row in normalized):
            raise ArchiveError("An imported replay file cannot authenticate live evidence")
        header = {"source": manifest.source, "imported_at": _time(manifest.imported_at),
                  "format": manifest.format, "schema_version": manifest.schema_version,
                  "evidence_kind": manifest.evidence_kind, "provider_reference": manifest.provider_reference,
                  "publication_required": imported_at_clock is not None,
                  "raw_sha256": sha256(manifest.raw_payload).hexdigest(),
                  "semantic_records": sorted({row["record_id"] for row in normalized})}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if imported_at_clock is not None:
                imported_at = imported_at_clock()
                if not isinstance(imported_at, datetime):
                    raise ArchiveError("imported_at_clock must return an aware datetime")
                header["imported_at"] = _time(imported_at)
            manifest_id = _hash(header)
            existing = db.execute("SELECT * FROM market_manifests WHERE manifest_id=?", (manifest_id,)).fetchone()
            if existing:
                self._verify_manifest(existing)
                return dict(json.loads(existing["result_json"]), repeated_manifest=True)
            latest = db.execute("SELECT MAX(imported_at) FROM market_manifests").fetchone()[0]
            if latest is not None and header["imported_at"] < latest:
                raise ArchiveError("Actual import times cannot be backdated relative to the archive")
            seen, new, duplicates, conflicts = {}, {}, 0, []
            for row in normalized:
                key = row["identity_key"]
                prior = seen.get(key)
                if prior is None:
                    prior_row = db.execute("SELECT record_id FROM market_records WHERE identity_key=?", (key,)).fetchone()
                    prior = None if prior_row is None else prior_row["record_id"]
                if prior is not None:
                    if prior != row["record_id"]:
                        conflicts.append({"kind": row["kind"], "symbol": row["symbol"], "identity_key": key,
                                          "existing_record": prior, "incoming_record": row["record_id"],
                                          "incoming_payload": row["payload"]})
                    else:
                        duplicates += 1
                else:
                    new[row["record_id"]] = row
                seen[key] = prior or row["record_id"]
            status = "quarantined" if conflicts else "accepted"
            result = {"manifest_id": manifest_id, "status": status, "inserted": 0 if conflicts else len(new),
                      "duplicates": duplicates, "conflicts": conflicts, "repeated_manifest": False}
            digest = _hash({"manifest_id": manifest_id, "header": header, "result": result})
            values = (manifest_id, _canonical(header), manifest.raw_payload, header["raw_sha256"],
                      header["imported_at"], status, _canonical(result), digest)
            if pre_commit is not None:
                pre_commit()
            db.execute("INSERT INTO market_manifests VALUES (?,?,?,?,?,?,?,?)", values)
            if not conflicts:
                for row in new.values():
                    db.execute("INSERT INTO market_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        tuple(row[field] for field in ("record_id", "identity_key", "kind", "symbol", "source_at",
                        "received_at", "known_at", "captured_at", "source", "evidence_kind", "payload", "payload_sha256"))
                        + (header["imported_at"],))
                for record_id in header["semantic_records"]:
                    db.execute("INSERT INTO market_lineage VALUES (?,?)", (manifest_id, record_id))
            if pre_commit is not None:
                pre_commit()
            return result

    @staticmethod
    def _verify_publication(row, manifest_row, header, summary):
        if (row["manifest_id"] != manifest_row["manifest_id"] or not header.get("publication_required", False)
                or summary["status"] not in {"accepted", "quarantined"} or _time(row["completed_at"]) != row["completed_at"]
                or row["completed_at"] < header["imported_at"]
                or row["sha256"] != _hash({"manifest_id": row["manifest_id"],
                    "manifest_sha256": manifest_row["sha256"], "completed_at": row["completed_at"]})):
            raise ArchiveIntegrityError("Capture publication marker failed integrity validation")
        return row["completed_at"]

    def seal_batch(self, manifest_id, *, completed_at=None, completed_at_clock=None, pre_commit=None):
        """Record completion after the separate data transaction has returned.

        Live capture supplies ``completed_at_clock``; it is sampled after the
        exclusive publication lock and validation, so lock waits cannot backdate
        availability. ``completed_at`` is retained for explicit supplied history.
        Neither is a nanosecond-exact API visibility claim. A missing marker leaves
        capture-only records unavailable to observed queries. Repeated sealing
        returns the original marker; it can never refresh observation time.
        """
        if self.read_only:
            raise ArchiveError("Read-only archive cannot seal capture batches")
        if pre_commit is not None and not callable(pre_commit):
            raise ArchiveError("pre_commit must be callable")
        if completed_at_clock is not None and (not callable(completed_at_clock) or completed_at is not None):
            raise ArchiveError("Supply a completion clock or a supplied timestamp, not both")
        if completed_at_clock is None and not isinstance(completed_at, datetime):
            raise ArchiveError("Capture completion requires an aware datetime")
        if completed_at_clock is None:
            _time(completed_at)
        with self._connect() as db:
            # In rollback journal mode this obtains the final publication lock
            # before sampling the clock; a later reader cannot stall COMMIT.
            db.execute("BEGIN EXCLUSIVE")
            manifest_row = db.execute("SELECT * FROM market_manifests WHERE manifest_id=?", (manifest_id,)).fetchone()
            if manifest_row is None:
                raise ArchiveError("Capture manifest does not exist")
            header, summary = self._verify_manifest(manifest_row)
            if not header.get("publication_required", False):
                raise ArchiveError("Only capture batches can be sealed")
            existing = db.execute("SELECT * FROM market_publications WHERE manifest_id=?", (manifest_id,)).fetchone()
            if existing is not None:
                self._verify_publication(existing, manifest_row, header, summary)
                return dict(existing, repeated_publication=True)
            rows = db.execute("SELECT r.* FROM market_records r JOIN market_lineage l ON r.record_id=l.record_id WHERE l.manifest_id=? LIMIT ?",
                              (manifest_id, MAX_RECORDS + 1)).fetchall()
            expected_records = set(header["semantic_records"]) if summary["status"] == "accepted" else set()
            if len(rows) > MAX_RECORDS or {row["record_id"] for row in rows} != expected_records:
                raise ArchiveIntegrityError("Capture cannot seal incomplete record lineage")
            for row in rows:
                self._verify_record(row)
            if pre_commit is not None:
                pre_commit()
            sampled = completed_at_clock() if completed_at_clock is not None else completed_at
            if not isinstance(sampled, datetime):
                raise ArchiveError("Completion clock must return an aware datetime")
            completed = _time(sampled)
            if completed < header["imported_at"]:
                raise ArchiveError("Capture completion cannot precede data admission")
            latest = db.execute("SELECT MAX(completed_at) FROM market_publications").fetchone()[0]
            if latest is not None and completed < latest:
                raise ArchiveError("Capture completion times cannot move backwards")
            digest = _hash({"manifest_id": manifest_id, "manifest_sha256": manifest_row["sha256"], "completed_at": completed})
            db.execute("INSERT INTO market_publications VALUES (?,?,?)", (manifest_id, completed, digest))
            if pre_commit is not None:
                pre_commit()
            return {"manifest_id": manifest_id, "completed_at": completed, "sha256": digest, "repeated_publication": False}

    @staticmethod
    def _verify_manifest(row):
        try:
            header, result = json.loads(row["header"]), json.loads(row["result_json"])
            base_fields = {"source", "imported_at", "format", "schema_version", "evidence_kind", "provider_reference", "raw_sha256", "semantic_records"}
            if (set(header) not in (base_fields, base_fields | {"publication_required"})
                    or type(header.get("publication_required", False)) is not bool
                    or set(result) != {"manifest_id", "status", "inserted", "duplicates", "conflicts", "repeated_manifest"}
                    or result["manifest_id"] != row["manifest_id"] or result["repeated_manifest"] is not False
                    or result["status"] not in {"accepted", "quarantined"}
                    or not isinstance(result["conflicts"], list)
                    or not isinstance(header["semantic_records"], list)
                    or not 1 <= len(header["semantic_records"]) <= MAX_RECORDS
                    or header["semantic_records"] != sorted(set(header["semantic_records"]))
                    or any(not isinstance(item, str) or len(item) != 64 or any(c not in "0123456789abcdef" for c in item) for item in header["semantic_records"])):
                raise ArchiveIntegrityError("Invalid manifest schema")
            _integer(result["inserted"]); _integer(result["duplicates"])
            if (result["status"] == "quarantined") != bool(result["conflicts"]) or (result["status"] == "quarantined" and result["inserted"] != 0):
                raise ArchiveIntegrityError("Invalid quarantine summary")
            if len(result["conflicts"]) > MAX_RECORDS:
                raise ArchiveIntegrityError("Too many conflict facts")
            for conflict in result["conflicts"]:
                required = {"kind", "symbol", "identity_key", "existing_record", "incoming_record"}
                if not isinstance(conflict, dict) or set(conflict) not in (required, required | {"incoming_payload"}):
                    raise ArchiveIntegrityError("Invalid conflict fact schema")
                if conflict["kind"] not in {"catalogue", "option", "underlying"}:
                    raise ArchiveIntegrityError("Invalid conflict record kind")
                _text(conflict["symbol"])
                for key in ("identity_key", "existing_record", "incoming_record"):
                    value = conflict[key]
                    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                        raise ArchiveIntegrityError("Invalid conflict record identity")
                if conflict["existing_record"] == conflict["incoming_record"] or conflict["incoming_record"] not in header["semantic_records"]:
                    raise ArchiveIntegrityError("Conflict does not contradict a declared incoming record")
                if "incoming_payload" in conflict:
                    normalized = _record(_hydrate(conflict["kind"], conflict["incoming_payload"]))
                    if any(normalized[field] != conflict[target] for field, target in
                           (("record_id", "incoming_record"), ("identity_key", "identity_key"), ("symbol", "symbol"))):
                        raise ArchiveIntegrityError("Conflict payload does not match its identity")
            ImportManifest(header["source"], _dt(header["imported_at"]), bytes(row["raw_payload"]), header["format"],
                           header["schema_version"], header["evidence_kind"], header["provider_reference"])
            if _time(header["imported_at"]) != header["imported_at"]:
                raise ArchiveIntegrityError("Actual import time must use the archive UTC normal form")
            if (_hash(header) != row["manifest_id"] or sha256(row["raw_payload"]).hexdigest() != row["raw_sha256"]
                    or row["raw_sha256"] != header["raw_sha256"] or row["imported_at"] != header["imported_at"]
                    or row["status"] != result["status"] or _hash({"manifest_id": row["manifest_id"], "header": header, "result": result}) != row["sha256"]):
                raise ArchiveIntegrityError("Import manifest hash validation failed")
            return header, result
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise ArchiveIntegrityError("Invalid import manifest") from error

    @staticmethod
    def _verify_record(row):
        try:
            item = _hydrate(row["kind"], row["payload"])
            normalized = _record(item)
            if any(normalized[field] != row[field] for field in normalized):
                raise ArchiveIntegrityError("Archived record hash or index fields changed")
            return item
        except (ValueError, TypeError, KeyError) as error:
            raise ArchiveIntegrityError("Invalid archived market record") from error

    def _query_conflicts(self, db, cutoff, mode):
        sql = "SELECT * FROM market_manifests WHERE status='quarantined'"
        params = []
        if mode == "observed":
            sql += " AND imported_at<=?"
            params.append(cutoff)
        sql += " ORDER BY imported_at,manifest_id LIMIT ?"
        rows = db.execute(sql, params + [MAX_MANIFESTS + 1]).fetchall()
        if len(rows) > MAX_MANIFESTS:
            raise ArchiveError("Conflict query exceeds the bounded manifest limit")
        facts = []
        for manifest_row in rows:
            header, result = self._verify_manifest(manifest_row)
            publication = db.execute("SELECT * FROM market_publications WHERE manifest_id=?", (manifest_row["manifest_id"],)).fetchone()
            if publication is not None:
                published = self._verify_publication(publication, manifest_row, header, result)
            else:
                published = None if header.get("publication_required", False) else header["imported_at"]
            # v0.98 quarantines had no normalized conflict payload and no sealing
            # path. Their recorded import time remains conservative negative
            # evidence; they must not silently turn into a clean price history.
            legacy = all("incoming_payload" not in fact for fact in result["conflicts"])
            if mode == "observed" and published is None:
                if not legacy:
                    continue
                published = header["imported_at"]
            for conflict in result["conflicts"]:
                if "incoming_payload" in conflict:
                    item = _hydrate(conflict["kind"], conflict["incoming_payload"])
                    record = _record(item)
                else:
                    record = db.execute("SELECT * FROM market_records WHERE record_id=?", (conflict["existing_record"],)).fetchone()
                    if record is None:
                        raise ArchiveIntegrityError("Legacy conflict identity cannot be reconstructed; reimport its source for review")
                    item = self._verify_record(record)
                    if record["identity_key"] != conflict["identity_key"]:
                        raise ArchiveIntegrityError("Legacy conflict identity differs from its original record")
                claimed = max(value for value in (record["source_at"], record["received_at"], record["captured_at"], record["known_at"]) if value is not None)
                available = max(claimed, published) if mode == "observed" else (max(claimed, header["imported_at"]) if legacy else claimed)
                if available > cutoff:
                    continue
                facts.append(ArchiveConflict(conflict["kind"], conflict["symbol"], record["source"], record["evidence_kind"],
                    conflict["identity_key"], _dt(record["source_at"]), _dt(record["received_at"]), getattr(item, "sequence", None),
                    _dt(available), manifest_row["manifest_id"], conflict["existing_record"], conflict["incoming_record"],
                    getattr(item, "effective_from", None), getattr(item, "known_at", None)))
                if len(facts) > MAX_RECORDS:
                    raise ArchiveError("Conflict query exceeds the bounded fact limit")
        return tuple(sorted(facts, key=lambda fact: (fact.available_at, fact.fingerprint)))

    def query(self, as_of, *, mode="observed", start_at=None):
        cutoff = _time(as_of)
        beginning = _time(start_at, optional=True)
        if mode not in {"observed", "reconstructed"} or (beginning is not None and beginning > cutoff):
            raise ArchiveError("Invalid archive query mode or interval")
        where = "known_at<=? AND received_at<=? AND captured_at<=? AND (source_at IS NULL OR source_at<=?)"
        params = [cutoff] * 4
        if mode == "observed":
            where += " AND first_imported_at<=?"
            params.append(cutoff)
        if beginning is not None:
            where += " AND (kind='catalogue' OR received_at>=?)"
            params.append(beginning)
        grouped = {"catalogue": [], "option": [], "underlying": []}
        with self._connect() as db:
            db.execute("BEGIN")
            self._check_schema(db)
            rows = db.execute("SELECT * FROM market_records WHERE " + where + " ORDER BY received_at, kind, symbol, record_id LIMIT ?", params + [MAX_RECORDS + 1]).fetchall()
            if len(rows) > MAX_RECORDS:
                raise ArchiveError("Query exceeds 100000 records; choose a smaller interval")
            covered = db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='market_manifest_lookup'").fetchone()
            manifest_table = "market_manifests m INDEXED BY market_manifest_lookup" if covered else "market_manifests m"
            lineage_query = ("SELECT m.manifest_id,m.imported_at,m.status FROM " + manifest_table +
                " JOIN market_lineage l ON m.manifest_id=l.manifest_id WHERE l.record_id=? AND m.status='accepted' ORDER BY m.imported_at,m.manifest_id")
            manifests, verified, visible_at, ids, availability = {}, {}, {}, [], []
            for row in rows:
                item = self._verify_record(row)
                if isinstance(item, OptionQuote) and item.oi_source_at is not None and _time(item.oi_source_at) > cutoff:
                    continue
                linked = db.execute(lineage_query, (row["record_id"],)).fetchall()
                if not linked or min(entry["imported_at"] for entry in linked) != row["first_imported_at"]:
                    raise ArchiveIntegrityError("Record lineage/first observation is inconsistent")
                for entry in linked:
                    identity = entry["manifest_id"]
                    if identity not in verified:
                        full = db.execute("SELECT * FROM market_manifests WHERE manifest_id=?", (identity,)).fetchone()
                        header, result = self._verify_manifest(full)
                        verified[identity] = set(header["semantic_records"])
                        publication = db.execute("SELECT * FROM market_publications WHERE manifest_id=?", (identity,)).fetchone()
                        if publication is not None:
                            visible_at[identity] = self._verify_publication(publication, full, header, result)
                        else:
                            visible_at[identity] = None if header.get("publication_required", False) else header["imported_at"]
                    if row["record_id"] not in verified[identity]:
                        raise ArchiveIntegrityError("Record is not declared by its manifest")
                    if mode == "reconstructed" or (visible_at[identity] is not None and visible_at[identity] <= cutoff):
                        manifests[entry["manifest_id"]] = True
                admissible_times = [visible_at[entry["manifest_id"]] for entry in linked if visible_at[entry["manifest_id"]] is not None]
                first_available = min(admissible_times) if admissible_times else None
                if mode == "observed" and (first_available is None or first_available > cutoff):
                    continue
                if mode == "reconstructed" and item.provenance.evidence_kind in {"live_verified", "live_unverified"}:
                    # A retrospective view must remain unverified even if the
                    # original source was once verified. Preserve original raw
                    # bytes/archive IDs; change only the returned evidence view.
                    item = replace(item, provenance=replace(item.provenance, evidence_kind="replay_quote"))
                grouped[row["kind"]].append(item)
                ids.append(row["record_id"])
                availability.append((item.fingerprint, _dt(first_available or row["first_imported_at"])))
            conflicts = self._query_conflicts(db, cutoff, mode)
            manifests.update((fact.manifest_id, True) for fact in conflicts)
        label = ("RECONSTRUCTED / UNVERIFIED HISTORICAL CHRONOLOGY" if mode == "reconstructed"
                 else "OBSERVED ARCHIVE / SOURCE VERIFICATION NOT IMPLIED")
        if any(item.provenance.evidence_kind == "synthetic" for group in grouped.values() for item in group):
            label += " / CONTAINS SYNTHETIC DATA"
        return ArchiveSnapshot(tuple(grouped["catalogue"]), tuple(grouped["option"]), tuple(grouped["underlying"]),
            tuple(sorted(manifests)), label, _dt(cutoff), mode, tuple(sorted(ids)), tuple(sorted(availability)), conflicts)

    def manifests(self, *, include_raw=False):
        with self._connect() as db:
            self._check_schema(db)
            rows = db.execute("SELECT * FROM market_manifests ORDER BY imported_at,manifest_id").fetchall()
            result = []
            for row in rows:
                header, summary = self._verify_manifest(row)
                entry = dict(header, manifest_id=row["manifest_id"], status=row["status"], result=summary)
                if include_raw:
                    entry["raw_payload"] = bytes(row["raw_payload"])
                result.append(entry)
            return tuple(result)

    def inventory(self):
        with self._connect() as db:
            self._check_schema(db)
            counts = {row["kind"]: row["n"] for row in db.execute("SELECT kind,COUNT(*) n FROM market_records GROUP BY kind")}
            latest = db.execute("""SELECT MAX(source_at) source_at,MAX(received_at) received_at,
                MAX(known_at) known_at,MAX(first_imported_at) imported_at FROM market_records""").fetchone()
            dates = tuple(row[0] for row in db.execute("SELECT DISTINCT substr(received_at,1,10) FROM market_records WHERE kind!='catalogue' ORDER BY 1"))
            totals = db.execute("SELECT COUNT(*) n,SUM(CASE WHEN status='quarantined' THEN 1 ELSE 0 END) quarantined,MAX(imported_at) latest_import FROM market_manifests").fetchone()
            publications = db.execute("SELECT COUNT(*) n,MAX(completed_at) latest FROM market_publications").fetchone()
        return {"available": True, "schema_version": SCHEMA_VERSION,
                "counts": {kind: counts.get(kind, 0) for kind in ("catalogue", "option", "underlying")},
                "dates": dates, "date_timezone": "UTC", "manifest_count": totals["n"],
                "quarantined_manifests": totals["quarantined"] or 0,
                "publication_markers": publications["n"], "latest_completed_at": publications["latest"],
                "integrity_scope": "Inventory indexes only; counts include unsealed capture rows; query validates visibility and raw lineage",
                "latest_source_at": latest["source_at"], "latest_received_at": latest["received_at"],
                "latest_known_at": latest["known_at"], "latest_imported_at": totals["latest_import"]}

    def dates(self):
        return self.inventory()["dates"]

    def import_file(self, path, *, imported_at=None, source="manual-normalized-import"):
        """Archive strict schema-v2 JSON, or the existing schema-v1 options JSON/CSV.

        Schema v2 extends the existing JSON shape with ``underlying`` records:
        symbol,value,source_at,received_at,optional sequence. Source timestamps
        remain unverified historical claims. No current lot/expiry is invented.
        """
        target = Path(path)
        if target.is_symlink() or not target.is_file() or target.suffix.lower() not in {".json", ".csv"}:
            raise ArchiveError("Choose a regular normalized market JSON or CSV file")
        with target.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        _check_raw(raw)
        stamp = imported_at or datetime.now(timezone.utc)
        from .options_import import parse_options_bytes
        if target.suffix.lower() == ".csv":
            parsed = parse_options_bytes(raw, format="csv", imported_at=stamp)
            contracts, quotes, underlying = parsed.contracts, parsed.quotes, ()
        else:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ArchiveError("Duplicate normalized JSON field")
                    result[key] = value
                return result
            try:
                document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique, parse_float=str,
                                      parse_constant=lambda _: (_ for _ in ()).throw(ArchiveError("Nonfinite JSON")))
                if not isinstance(document, dict):
                    raise ArchiveError("Normalized import must be an object")
                if document.get("schema_version") == 1:
                    parsed = parse_options_bytes(raw, imported_at=stamp)
                    contracts, quotes, underlying = parsed.contracts, parsed.quotes, ()
                elif document.get("schema_version") == 2:
                    if set(document) - {"schema_version", "contracts", "quotes", "underlying", "underlying_price"}:
                        raise ArchiveError("Unsupported normalized archive fields")
                    contracts, quotes = _parse_v2_options(document, stamp)
                    underlying = _parse_underlying(document.get("underlying", []))
                else:
                    raise ArchiveError("Unsupported normalized import version")
            except (UnicodeError, json.JSONDecodeError, TypeError, KeyError, RecursionError) as error:
                raise ArchiveError("Malformed normalized market file") from error
        return self.append_batch(manifest=ImportManifest(source, stamp, raw, format="normalized-" + target.suffix[1:].lower()),
                                 contracts=contracts, quotes=quotes, underlying=underlying)


def _parse_v2_options(document, imported_at):
    from .options_import import parse_options_bytes, CONTRACT_FIELDS, QUOTE_FIELDS
    # Reuse the established parser in bounded pages rather than weakening fields.
    catalogue, quotes = document.get("contracts", []), document.get("quotes", [])
    if not isinstance(catalogue, list) or not isinstance(quotes, list) or len(catalogue) + len(quotes) > MAX_RECORDS:
        raise ArchiveError("Invalid normalized option arrays")
    if not catalogue and not quotes:
        return (), ()
    # Catalogue-only chunks use an explicit normalizer; no quote is fabricated.
    contracts = []
    for row in catalogue:
        if not isinstance(row, dict) or set(row) - CONTRACT_FIELDS or not CONTRACT_FIELDS - {"effective_until"} <= set(row):
            raise ArchiveError("Invalid dated catalogue fields")
        normalized = dict(row, effective_until=row.get("effective_until"))
        for field in ("expiry_at", "effective_from", "effective_until", "known_at"):
            normalized[field] = _dt(_time(normalized[field], optional=field == "effective_until"))
        for field in ("strike", "tick_size"):
            normalized[field] = _decimal(normalized[field], positive=True)
        normalized["provenance"] = RawProvenance("manual-import", _canonical(row).encode(), normalized["known_at"], "replay_quote")
        contracts.append(ContractMetadata(**normalized))
    observations, ids = [], {item.contract_id for item in contracts}
    for row in quotes:
        if not isinstance(row, dict) or set(row) - QUOTE_FIELDS or not {"contract_id", "source_at", "received_at", "bid", "ask"} <= set(row):
            raise ArchiveError("Invalid normalized option quote fields")
        if row["contract_id"] not in ids:
            raise ArchiveError("Each option quote requires supplied dated catalogue metadata")
        normalized = {field: row.get(field) for field in QUOTE_FIELDS}
        for field in ("source_at", "received_at", "oi_source_at"):
            normalized[field] = _dt(_time(normalized[field], optional=field != "received_at"))
        for field in ("bid", "ask", "last"):
            normalized[field] = _decimal(normalized[field], optional=True)
        normalized["provenance"] = RawProvenance("manual-import", _canonical(row).encode(), normalized["received_at"], "replay_quote")
        observations.append(OptionQuote(**normalized))
    return tuple(contracts), tuple(observations)


def _parse_underlying(rows):
    from .options_pipeline import UnderlyingObservation
    if not isinstance(rows, list) or len(rows) > MAX_RECORDS:
        raise ArchiveError("Invalid normalized underlying array")
    result = []
    for row in rows:
        required = {"symbol", "value", "source_at", "received_at"}
        if not isinstance(row, dict) or not required <= set(row) or set(row) - required - {"sequence"}:
            raise ArchiveError("Invalid normalized underlying fields")
        receipt = _dt(_time(row["received_at"]))
        result.append(UnderlyingObservation(_text(row["symbol"]), _decimal(row["value"], positive=True),
            _dt(_time(row["source_at"], optional=True)), receipt,
            RawProvenance("manual-import", _canonical(row).encode(), receipt, "replay_quote"),
            _integer(row.get("sequence"), optional=True)))
    return tuple(result)


def read_market_snapshot(path, as_of, *, mode="observed", start_at=None):
    return MarketArchive(path, read_only=True).query(as_of, mode=mode, start_at=start_at)


def _strict_schema(db):
    """Exact application objects; reject unrelated tables, views and triggers."""
    table_sql = {
        "market_manifests": """CREATE TABLE market_manifests (
            manifest_id TEXT PRIMARY KEY, header TEXT NOT NULL, raw_payload BLOB NOT NULL,
            raw_sha256 TEXT NOT NULL, imported_at TEXT NOT NULL, status TEXT NOT NULL,
            result_json TEXT NOT NULL, sha256 TEXT NOT NULL)""",
        "market_records": """CREATE TABLE market_records (
            record_id TEXT PRIMARY KEY, identity_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL, symbol TEXT NOT NULL, source_at TEXT, received_at TEXT NOT NULL,
            known_at TEXT NOT NULL, captured_at TEXT NOT NULL, source TEXT NOT NULL,
            evidence_kind TEXT NOT NULL, payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
            first_imported_at TEXT NOT NULL)""",
        "market_lineage": """CREATE TABLE market_lineage (
            manifest_id TEXT NOT NULL, record_id TEXT NOT NULL, PRIMARY KEY(manifest_id, record_id))""",
        "market_publications": """CREATE TABLE market_publications (
            manifest_id TEXT PRIMARY KEY, completed_at TEXT NOT NULL, sha256 TEXT NOT NULL)""",
    }
    expected = {name: ("table", sql) for name, sql in table_sql.items()}
    indexes = {"market_receipt_window": ("market_records", "kind, received_at, symbol"),
               "market_known_window": ("market_records", "kind, known_at, symbol"),
               "market_lineage_record": ("market_lineage", "record_id"),
               "market_manifest_lookup": ("market_manifests", "manifest_id,imported_at,status")}
    for name, (table, columns) in indexes.items():
        expected[name] = ("index", f"CREATE INDEX {name} ON {table}({columns})")
    for table in _TABLES:
        expected[f"sqlite_autoindex_{table}_1"] = ("index", None)
        for operation in ("UPDATE", "DELETE"):
            name = f"{table}_no_{operation.lower()}"
            expected[name] = ("trigger", f"CREATE TRIGGER {name} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only market archive'); END")
    expected["sqlite_autoindex_market_records_2"] = ("index", None)
    rows = db.execute("SELECT name,type,sql FROM sqlite_master").fetchall()
    if {row["name"] for row in rows} != set(expected):
        raise ArchiveIntegrityError("Archive contains missing or unrelated schema objects")
    def normalized(sql):
        return None if sql is None else "".join(sql.lower().split()).replace("ifnotexists", "")
    for row in rows:
        kind, sql = expected[row["name"]]
        if row["type"] != kind or normalized(row["sql"]) != normalized(sql):
            raise ArchiveIntegrityError("Archive schema does not match the fixed application schema")


def validate_archive(path):
    """Bounded read-only full validation before local backup/export inclusion.

    This validates every accepted/quarantined manifest, every normalized record,
    exact schema objects and all lineage links. It cannot authenticate provider
    claims or detect a coordinated rewrite by the database owner without an
    external trust anchor. Bounds: 64 MiB, 100k records, 1000 manifests, 1m links.
    """
    target = Path(path)
    if target.is_symlink() or not target.is_file() or target.stat().st_size > MAX_BACKUP_BYTES:
        raise ArchiveError("Archive backup validation requires a regular file at most 64 MiB")
    archive = MarketArchive(target, read_only=True)
    with archive._connect() as db:
        db.execute("BEGIN")
        _strict_schema(db)
        counts = {name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] for name in _TABLES}
        if (counts["market_records"] > MAX_RECORDS or counts["market_manifests"] > MAX_MANIFESTS
                or counts["market_lineage"] > 1_000_000 or counts["market_publications"] > MAX_MANIFESTS):
            raise ArchiveError("Archive exceeds bounded full-validation limits")
        manifests = {}
        for row in db.execute("SELECT * FROM market_manifests ORDER BY imported_at,manifest_id"):
            header, result = archive._verify_manifest(row)
            manifests[row["manifest_id"]] = {"records": set(header["semantic_records"]),
                "status": result["status"], "imported_at": header["imported_at"], "linked": set(),
                "header": header, "summary": result, "manifest_sha256": row["sha256"]}
        for row in db.execute("SELECT * FROM market_publications"):
            manifest = manifests.get(row["manifest_id"])
            if manifest is None:
                raise ArchiveIntegrityError("Publication refers to a missing manifest")
            archive._verify_publication(row, {"manifest_id": row["manifest_id"], "sha256": manifest["manifest_sha256"]},
                                        manifest["header"], manifest["summary"])
        records = {}
        for row in db.execute("SELECT * FROM market_records"):
            archive._verify_record(row)
            if _time(row["first_imported_at"]) != row["first_imported_at"]:
                raise ArchiveIntegrityError("Invalid actual archive observation time")
            records[row["record_id"]] = {"first_import": row["first_imported_at"], "linked_imports": []}
        for row in db.execute("SELECT manifest_id,record_id FROM market_lineage"):
            manifest = manifests.get(row["manifest_id"])
            record = records.get(row["record_id"])
            if (manifest is None or record is None or manifest["status"] != "accepted"
                    or row["record_id"] not in manifest["records"]):
                raise ArchiveIntegrityError("Invalid or unrelated archive lineage")
            manifest["linked"].add(row["record_id"])
            record["linked_imports"].append(manifest["imported_at"])
        for manifest in manifests.values():
            if manifest["status"] == "accepted" and manifest["linked"] != manifest["records"]:
                raise ArchiveIntegrityError("Accepted import is missing declared records/lineage")
            if manifest["status"] == "quarantined" and manifest["linked"]:
                raise ArchiveIntegrityError("Quarantined import cannot publish normalized records")
        for record in records.values():
            if not record["linked_imports"] or min(record["linked_imports"]) != record["first_import"]:
                raise ArchiveIntegrityError("Record first observation is inconsistent with lineage")
        return {"valid": True, "schema_version": SCHEMA_VERSION,
                "records": counts["market_records"], "manifests": counts["market_manifests"],
                "lineage_links": counts["market_lineage"],
                "publication_markers": counts["market_publications"],
                "quarantined_manifests": sum(item["status"] == "quarantined" for item in manifests.values())}

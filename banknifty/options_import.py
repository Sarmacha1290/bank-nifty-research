"""Bounded, strict manual option imports. Imported chronology is unverified.

No provider SDK or transport belongs here. JSON and CSV use our documented
normal form, not an inferred Groww response shape. Candle files are rejected.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
import csv
import io
import json

from .options_data import ContractMetadata, OptionQuote, RawProvenance

MAX_BYTES = 2 * 1024 * 1024
MAX_QUOTES = 2000
MAX_CONTRACTS = 200
CONTRACT_FIELDS = frozenset({"contract_id", "underlying", "exchange", "option_type", "strike",
    "expiry_at", "lot_size", "tick_size", "effective_from", "effective_until", "known_at", "listed"})
QUOTE_FIELDS = frozenset({"contract_id", "source_at", "received_at", "bid", "ask", "bid_size",
    "ask_size", "last", "open_interest", "oi_source_at", "sequence"})
CSV_FIELDS = tuple(sorted(CONTRACT_FIELDS | QUOTE_FIELDS | {"underlying_price"}))


@dataclass(frozen=True)
class ImportedOptions:
    contracts: tuple[ContractMetadata, ...]
    quotes: tuple[OptionQuote, ...]
    imported_at: datetime
    file_sha256: str
    label: str = "REPLAY / UNVERIFIED IMPORT"
    underlying_price: Decimal | None = None


def _timestamp(value, *, optional=False):
    if optional and value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Expected an ISO timestamp with a timezone offset")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Invalid ISO timestamp") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamp timezone is required")
    return result


def _decimal(value, *, optional=False):
    if optional and value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("Expected a decimal number")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid decimal number") from None
    if not result.is_finite() or result < 0 or result > Decimal('1000000000000'):
        raise ValueError("Decimal number is outside the import bounds")
    return result


def _integer(value, *, optional=False):
    if optional and value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Expected a nonnegative integer")
    if isinstance(value, str) and (not value.isascii() or not value.isdigit()):
        raise ValueError("Expected a nonnegative integer")
    result = int(value)
    if result < 0 or result > 10**12:
        raise ValueError("Integer outside import bounds")
    return result


def _text(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 100
            or not value.isascii() or not all(c.isalnum() or c in '-_.:' for c in value)):
        raise ValueError("Identifiers must use bounded ASCII letters, digits, dash, dot, colon or underscore")
    return value


def _keys(row, allowed, required):
    if not isinstance(row, dict) or set(row) - allowed or not required <= set(row):
        raise ValueError("Import columns do not match the documented option schema")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys are not permitted")
        result[key] = value
    return result


def load_options_file(path, *, imported_at=None):
    """Read a normalized JSON/CSV file; preserve its hash, never authenticate it.

    A supplied source time remains a claim in reconstructed historical data.
    Missing source times stay None. All imported records are replay_quote;
    this parser cannot construct live_verified records.
    """
    path = Path(path)
    if path.is_symlink() or path.suffix.lower() not in {'.json', '.csv'}:
        raise ValueError("Choose a regular normalized JSON or CSV option file")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Option import exceeds the 2 MiB limit")
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Option import exceeds the 2 MiB limit")
    return parse_options_bytes(raw, format=path.suffix.lower()[1:], imported_at=imported_at)


def parse_options_bytes(raw, *, format='json', imported_at=None):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError("Expected bounded option file bytes")
    imported_at = imported_at or datetime.now(timezone.utc)
    if not isinstance(imported_at, datetime) or imported_at.tzinfo is None or imported_at.utcoffset() is None:
        raise ValueError("Import observation time must include a timezone")
    digest = sha256(raw).hexdigest()
    try:
        content = raw.decode('utf-8-sig')
        if format == 'json':
            document = json.loads(content, object_pairs_hook=_unique_object, parse_float=str,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON number')))
            _keys(document, {'schema_version', 'contracts', 'quotes', 'underlying_price'},
                  {'schema_version', 'contracts', 'quotes'})
            if type(document['schema_version']) is not int or document['schema_version'] != 1:
                raise ValueError('Unsupported option import schema version')
            metadata, observations = document['contracts'], document['quotes']
            underlying = _decimal(document.get('underlying_price'), optional=True)
        elif format == 'csv':
            reader = csv.DictReader(io.StringIO(content))
            if (not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames)
                    or set(reader.fieldnames) != set(CSV_FIELDS)):
                raise ValueError('CSV header must exactly match the documented schema')
            metadata, observations, versions, spots = [], [], {}, set()
            for row in reader:
                if len(observations) >= MAX_QUOTES or None in row or any(v is None for v in row.values()):
                    raise ValueError('Malformed or oversized option CSV')
                contract = {k: row[k] for k in CONTRACT_FIELDS}
                contract['listed'] = {'true': True, 'false': False}.get(row['listed'].lower())
                version = (contract['contract_id'], contract['effective_from'], contract['known_at'])
                if version in versions and versions[version] != contract:
                    raise ValueError('Conflicting catalogue versions in CSV')
                if version not in versions:
                    metadata.append(contract); versions[version] = contract
                observations.append({k: row[k] for k in QUOTE_FIELDS})
                if row['underlying_price']:
                    spots.add(_decimal(row['underlying_price']))
            if len(spots) > 1:
                raise ValueError('Import one decision spot price per bundle')
            underlying = next(iter(spots), None)
        else:
            raise ValueError('Only JSON and CSV option imports are supported')
        if (not isinstance(metadata, list) or not 1 <= len(metadata) <= MAX_CONTRACTS
                or not isinstance(observations, list) or not 1 <= len(observations) <= MAX_QUOTES):
            raise ValueError('Import requires 1–200 contract versions and 1–2000 quotes')
        def provenance(row, captured_at):
            # File hash/import observation belong to bundle lineage, not the
            # earlier decision identity: adding future rows must not rewrite it.
            payload = json.dumps({'record': row, 'chronology': 'reconstructed_unverified'},
                sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
            return RawProvenance('manual-import', payload, captured_at, 'replay_quote')
        contracts, quotes, versions = [], [], set()
        for row in metadata:
            _keys(row, CONTRACT_FIELDS, CONTRACT_FIELDS - {'effective_until'})
            known = _timestamp(row['known_at'])
            if type(row['listed']) is not bool:
                raise ValueError('listed must be an explicit boolean catalogue claim')
            item = ContractMetadata(_text(row['contract_id']), _text(row['underlying']),
                _text(row['exchange']), _text(row['option_type']), _decimal(row['strike']),
                _timestamp(row['expiry_at']), _integer(row['lot_size']), _decimal(row['tick_size']),
                _timestamp(row['effective_from']), _timestamp(row.get('effective_until'), optional=True),
                known, row['listed'], provenance(row, known))
            if item.option_type not in {'CALL', 'PUT'} or item.lot_size == 0 or item.tick_size == 0 or item.strike == 0:
                raise ValueError('Invalid contract right, lot, tick or strike')
            version = (item.contract_id, item.effective_from, item.known_at)
            if version in versions:
                raise ValueError('Duplicate contract metadata version')
            versions.add(version); contracts.append(item)
        ids = {item.contract_id for item in contracts}
        for row in observations:
            _keys(row, QUOTE_FIELDS, {'contract_id', 'source_at', 'received_at', 'bid', 'ask'})
            receipt = _timestamp(row['received_at'])
            identifier = _text(row['contract_id'])
            if identifier not in ids:
                raise ValueError('Quote has no supplied contract metadata')
            quotes.append(OptionQuote(identifier, _timestamp(row['source_at'], optional=True), receipt,
                _decimal(row['bid'], optional=True), _decimal(row['ask'], optional=True), provenance(row, receipt),
                bid_size=_integer(row.get('bid_size'), optional=True), ask_size=_integer(row.get('ask_size'), optional=True),
                last=_decimal(row.get('last'), optional=True), open_interest=_integer(row.get('open_interest'), optional=True),
                oi_source_at=_timestamp(row.get('oi_source_at'), optional=True), sequence=_integer(row.get('sequence'), optional=True)))
        return ImportedOptions(tuple(contracts), tuple(quotes), imported_at, digest, underlying_price=underlying)
    except (UnicodeError, json.JSONDecodeError, csv.Error, RecursionError, KeyError, TypeError, OverflowError):
        raise ValueError('Malformed option import; check the documented schema') from None

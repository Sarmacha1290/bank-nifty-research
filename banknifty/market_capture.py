"""Explicit, bounded provider-payload to archive bridge with no transport.

The caller supplies a read-only source and reviewed expiry times. This module
does not discover accounts, read credentials, construct clients, or trade.
Source timeout cooperation is required; late results are never committed.
"""
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import math
from pathlib import Path
import queue
import threading
import time
from typing import Protocol

from .market_archive import ImportManifest, MarketArchive, read_market_snapshot
from .options_data import QualityPolicy, canonical_fingerprint, validate_contract, validate_quote
from .provider_adapters import (adapt_index_feed, adapt_rest_ltp, adapt_option_quote,
    adapt_option_depth_feed, adapt_option_ltp_feed, adapt_option_chain,
    decode_provider_json, instrument_contract, parse_groww_catalogue)

KINDS = frozenset({"catalogue", "index_feed", "index_ltp", "option_depth", "option_ltp", "option_quote", "option_chain"})


def _now():
    return datetime.now(timezone.utc)


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Capture timestamps must include a timezone")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ProviderObservation:
    kind: str
    raw_payload: bytes
    received_at: datetime
    instrument_id: str | None = None
    expiry_date: date | None = None
    payload_origin: str = "provider_bytes"

    def __post_init__(self):
        if self.kind not in KINDS or not isinstance(self.raw_payload, bytes) or not 1 <= len(self.raw_payload) <= 16_000_000:
            raise ValueError("Expected a supported bounded raw provider observation")
        _aware(self.received_at)
        if self.instrument_id is not None and (not isinstance(self.instrument_id, str) or not 1 <= len(self.instrument_id) <= 100
                or any(not (c.isascii() and (c.isalnum() or c in "-_.")) for c in self.instrument_id)):
            raise ValueError("Invalid capture instrument identity")
        if self.expiry_date is not None and type(self.expiry_date) is not date:
            raise ValueError("Explicit option-chain expiry date required")
        if self.payload_origin not in {"provider_bytes", "callback_json"}:
            raise ValueError("Explicit provider-byte or decoded-callback origin required")


class ReadOnlyObservationSource(Protocol):
    def read_next(self, *, timeout_seconds: float) -> ProviderObservation | None:
        """One authorized read; honor timeout, return None when source ends."""
        ...


class QueueObservationSource:
    """Bounded SDK-callback handoff. No socket, authentication or subscription.

    Queue overflow stops the consumer instead of silently hiding a data gap.
    Decoded SDK dictionaries are explicitly labeled callback_json; they cannot
    recover original wire bytes or decimal lexemes that the SDK discarded.
    """
    def __init__(self, *, maxsize=256, max_queued_bytes=16_000_000, clock=_now):
        if type(maxsize) is not int or not 1 <= maxsize <= 10000 or type(max_queued_bytes) is not int or not 1 <= max_queued_bytes <= 64_000_000:
            raise ValueError("Callback queue requires bounded count and bytes")
        self._queue, self._lock = queue.Queue(maxsize=maxsize), threading.Lock()
        self.clock, self._bytes, self._closed, self._overflowed = clock, 0, False, False
        self._max_queued_bytes = max_queued_bytes

    def submit(self, observation):
        if not isinstance(observation, ProviderObservation):
            raise TypeError("Callback queue accepts typed observations only")
        with self._lock:
            if self._closed or self._overflowed:
                return False
            if self._bytes + len(observation.raw_payload) > self.max_queued_bytes or self._queue.full():
                self._overflowed = True
                return False
            self._queue.put_nowait(observation)
            self._bytes += len(observation.raw_payload)
            return True

    @property
    def max_queued_bytes(self):
        return self._max_queued_bytes

    def submit_raw(self, kind, raw_payload, *, instrument_id=None, expiry_date=None):
        return self.submit(ProviderObservation(kind, raw_payload, _aware(self.clock()), instrument_id, expiry_date))

    def submit_mapping(self, kind, payload, *, instrument_id=None, expiry_date=None):
        def decimal_text(value):
            if isinstance(value, Decimal) and value.is_finite():
                return str(value)
            raise ValueError("Callback snapshot contains an unsupported value")
        raw = json.dumps(payload, separators=(",", ":"), allow_nan=False, default=decimal_text).encode("utf-8")
        decode_provider_json(raw)  # Bound/credential-key validation before queuing.
        return self.submit(ProviderObservation(kind, raw, _aware(self.clock()), instrument_id, expiry_date, "callback_json"))

    def close(self):
        with self._lock:
            self._closed = True

    def read_next(self, *, timeout_seconds):
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("Callback read timeout must be bounded")
        with self._lock:
            if self._overflowed:
                raise BufferError("Callback queue overflow; capture continuity is unknown")
            if self._closed and self._queue.empty():
                return None
        try:
            observation = self._queue.get(timeout=timeout_seconds)
        except queue.Empty:
            raise TimeoutError("No callback observation before read timeout") from None
        with self._lock:
            self._bytes -= len(observation.raw_payload)
            if self._overflowed:
                raise BufferError("Callback queue overflow; capture continuity is unknown")
        return observation


@dataclass(frozen=True)
class CaptureConfig:
    enabled: bool = False
    duration_seconds: float = 300
    read_timeout_seconds: float = 5
    max_reads: int = 5000
    max_messages: int = 2000
    max_records: int = 25000
    max_payload_bytes: int = 16_000_000
    evidence_kind: str = "live_unverified"
    quality_policy: QualityPolicy = QualityPolicy()

    def __post_init__(self):
        if type(self.enabled) is not bool or self.evidence_kind not in {"live_unverified", "synthetic"}:
            raise ValueError("Explicit enabled flag and unverified/synthetic classification required")
        for name, upper in (("duration_seconds", 28800), ("read_timeout_seconds", 30)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= upper:
                raise ValueError("Capture duration/read timeout must be finite and bounded")
        for name, upper in (("max_reads", 100000), ("max_messages", 100000), ("max_records", 100000), ("max_payload_bytes", 16_000_000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError("Capture count/size limits must be positive and bounded")
        if not isinstance(self.quality_policy, QualityPolicy):
            raise ValueError("Typed quote quality policy required")


@dataclass(frozen=True)
class CaptureEvent:
    kind: str
    status: str
    received_at: datetime
    imported_at: datetime | None
    raw_sha256: str
    manifest_id: str | None = None
    inserted: int = 0
    duplicates: int = 0
    quality_accepted: bool = False
    reasons: tuple[str, ...] = ()
    admitted_at: datetime | None = None


@dataclass(frozen=True)
class CaptureResult:
    status: str
    config_fingerprint: str
    reads: int
    messages: int
    inserted: int
    duplicates: int
    events: tuple[CaptureEvent, ...]
    label: str
    reasons: tuple[str, ...] = ()
    orders_supported: bool = False
    live_verified: bool = False


class _CaptureStopped(Exception):
    def __init__(self, status):
        self.status = status


class _CatalogueReview(ValueError):
    pass


class MarketCaptureSession:
    """One explicit bounded session. A stopped instance cannot restart itself."""
    def __init__(self, archive_path, config=CaptureConfig(), *, reviewed_expiries=None, clock=_now, monotonic=time.monotonic):
        if not isinstance(config, CaptureConfig):
            raise TypeError("Expected CaptureConfig")
        self.path, self.config = Path(archive_path), config
        self.clock, self.monotonic = clock, monotonic
        self.expiries = dict(reviewed_expiries or {})
        if any(not isinstance(k, str) or not k for k in self.expiries):
            raise ValueError("Expiry review must name exact catalogue contracts")
        for value in self.expiries.values():
            _aware(value)
        self.fingerprint = canonical_fingerprint("provider-archive-bridge-v1", asdict(config), self.expiries)
        self.instruments, self.contracts = {}, {}
        self._prior_contracts = None
        self._source_latest = {}
        self._started = self._last_mono = self._last_wall = None
        self._result = None
        self._events = []
        self._reads = self._messages = self._records = self._inserted = self._duplicates = 0

    def _gate(self, cancelled):
        # A caller's cancellation check can block; sample eligibility time only
        # after it returns so that control latency cannot hide an expired run.
        if cancelled is not None and cancelled():
            raise _CaptureStopped("cancelled")
        wall, mono = _aware(self.clock()), self.monotonic()
        if type(mono) not in (int, float) or not math.isfinite(mono):
            raise _CaptureStopped("clock_regression")
        if self._started is None:
            self._started = mono
        if ((self._last_mono is not None and mono < self._last_mono)
                or (self._last_wall is not None and wall < self._last_wall)):
            raise _CaptureStopped("clock_regression")
        self._last_wall, self._last_mono = wall, mono
        if mono - self._started >= self.config.duration_seconds:
            raise _CaptureStopped("duration_complete")
        return wall

    def _classified(self, item):
        return replace(item, provenance=replace(item.provenance, evidence_kind=self.config.evidence_kind))

    def _catalogue(self, message, now):
        parsed = parse_groww_catalogue(message.raw_payload.decode("utf-8"), message.received_at)
        if self._prior_contracts is None:
            self._prior_contracts = {}
            if self.path.exists():
                for item in read_market_snapshot(self.path, now, mode="observed").contracts:
                    if item.contract_id in self._prior_contracts and self._prior_contracts[item.contract_id] != item:
                        raise _CatalogueReview("Existing metadata has multiple versions; explicit review required")
                    self._prior_contracts[item.contract_id] = item
        instruments, contracts = {}, {}
        for original in parsed.instruments:
            item = self._classified(original)
            if item.instrument_type == "IDX":
                prior = self.instruments.get(item.trading_symbol)
                if prior is not None:
                    if (prior.exchange_token, prior.groww_symbol) != (item.exchange_token, item.groww_symbol):
                        raise _CatalogueReview("Index catalogue identity changed; explicit review required")
                    item = prior
                instruments[item.trading_symbol] = item
                continue
            expiry = self.expiries.get(item.contract_id)
            if expiry is None:
                continue  # Unreviewed contracts remain unavailable, never invented.
            new_contract = self._classified(instrument_contract(item, expiry_at=expiry))
            previous = self.contracts.get(item.contract_id) or self._prior_contracts.get(item.contract_id)
            if previous is not None:
                if previous.provenance.source != new_contract.provenance.source or previous.provenance.evidence_kind != self.config.evidence_kind:
                    raise _CatalogueReview("Existing catalogue provenance differs; explicit review required")
                old_row = decode_provider_json(previous.provenance.payload)
                incoming_row = decode_provider_json(item.provenance.payload)
                if not isinstance(old_row, dict) or not isinstance(incoming_row, dict):
                    raise _CatalogueReview("Existing metadata source row is ambiguous")
                identity = ("exchange", "segment", "exchange_token", "trading_symbol", "groww_symbol", "instrument_type", "underlying_symbol")
                terms = ("underlying", "exchange", "option_type", "strike", "expiry_at", "lot_size", "tick_size")
                if (any(old_row.get(k) != incoming_row.get(k) for k in identity)
                        or any(getattr(previous, k) != getattr(new_contract, k) for k in terms)):
                    raise _CatalogueReview("Catalogue trading terms changed; explicit effective-version review required")
                new_contract = previous
                item = replace(item, observed_at=previous.known_at, provenance=previous.provenance)
            instruments[item.trading_symbol] = item
            contracts[item.contract_id] = new_contract
        if "BANKNIFTY" not in instruments or not contracts:
            raise _CatalogueReview("Observed index and at least one explicitly reviewed option expiry required")
        self.instruments, self.contracts = instruments, contracts
        return tuple(contracts.values()), (), (), (), True

    def _map(self, message, now):
        if message.kind == "catalogue":
            return self._catalogue(message, now)
        if not self.instruments:
            raise _CatalogueReview("Observe and review a catalogue before market payloads")
        data = decode_provider_json(message.raw_payload)
        instrument = self.instruments.get(message.instrument_id)
        options, underlying, reasons = [], [], []
        if message.kind == "option_chain":
            result = adapt_option_chain(data, tuple(item for item in self.instruments.values() if item.instrument_type != "IDX"),
                message.received_at, underlying_instrument=self.instruments["BANKNIFTY"], expiry_date=message.expiry_date)
            options.extend(result.options);reasons.extend(result.issues)
            if result.underlying is not None:
                underlying.append(result.underlying)
        else:
            if instrument is None:
                raise _CatalogueReview("Payload needs one previously observed exact instrument identity")
            if message.kind in {"index_feed", "index_ltp"}:
                function = adapt_index_feed if message.kind == "index_feed" else adapt_rest_ltp
                item = function(data, instrument, message.received_at)
                if item is not None:
                    underlying.append(item)
            else:
                function = {"option_quote": adapt_option_quote, "option_depth": adapt_option_depth_feed,
                            "option_ltp": adapt_option_ltp_feed}[message.kind]
                item = function(data, instrument, message.received_at)
                if item is not None:
                    options.append(item)
        quotes = tuple(self._classified(item.quote) for item in options)
        underlying = tuple(self._classified(item) for item in underlying)
        for item in options:
            reasons.extend(item.issues)
        for quote in quotes:
            quality = validate_quote(quote, self.contracts[quote.contract_id], now, self.config.quality_policy)
            reasons.extend(quality.reasons)
            if quote.source_at is not None and (quote.source_at > quote.received_at or quote.source_at > now):
                raise ValueError("Future-source quote cannot be archived as eligible evidence")
        for item in underlying:
            if item.source_at is None:
                reasons.append("underlying_source_timestamp_missing")
            elif item.source_at > item.received_at or item.source_at > now:
                raise ValueError("Future-source underlying cannot be archived as eligible evidence")
            elif (now - item.source_at).total_seconds() > self.config.quality_policy.max_source_age_seconds:
                reasons.append("underlying_source_stale")
            if (now - item.received_at).total_seconds() > self.config.quality_policy.max_receipt_age_seconds:
                reasons.append("underlying_receipt_stale")
        for item in (*quotes, *underlying):
            key = (getattr(item, "contract_id", getattr(item, "symbol", "")), item.provenance.source)
            source = item.source_at
            if source is not None:
                previous = self._source_latest.get(key)
                if previous is not None and source < previous:
                    reasons.append("out_of_order_source")
                self._source_latest[key] = max(previous, source) if previous is not None else source
        return (), quotes, underlying, tuple(dict.fromkeys(reasons)), not reasons

    def _finish(self, status, *reasons):
        self._result = CaptureResult(status, self.fingerprint, self._reads, self._messages, self._inserted,
            self._duplicates, tuple(self._events), "SYNTHETIC FIXTURE CAPTURE" if self.config.evidence_kind == "synthetic"
            else "PROVIDER CAPTURE UNVERIFIED", tuple(reasons))
        return self._result

    def _final_quality(self, contracts, quotes, underlying, now):
        reasons = []
        for contract in contracts:
            reasons.extend(validate_contract(contract, now))
        for quote in quotes:
            reasons.extend(validate_quote(quote, self.contracts[quote.contract_id], now, self.config.quality_policy).reasons)
        for item in underlying:
            if item.source_at is None:
                reasons.append("underlying_source_timestamp_missing")
            elif (now-item.source_at).total_seconds() > self.config.quality_policy.max_source_age_seconds:
                reasons.append("underlying_source_stale")
            if (now-item.received_at).total_seconds() > self.config.quality_policy.max_receipt_age_seconds:
                reasons.append("underlying_receipt_stale")
            if self.config.quality_policy.require_live_verified:
                reasons.append("underlying_live_unverified")
        return tuple(dict.fromkeys(reasons))

    def run(self, source: ReadOnlyObservationSource, *, cancelled=None):
        if not self.config.enabled:
            return self._finish("inactive", "Explicit enable flag required; no source reads or archive writes")
        if self._result is not None:
            return self._result
        try:
            while self._reads < self.config.max_reads and self._messages < self.config.max_messages:
                self._gate(cancelled)
                remaining = self.config.duration_seconds - (self._last_mono - self._started)
                read_started = self._last_mono
                read_timeout = min(self.config.read_timeout_seconds, remaining)
                self._reads += 1
                try:
                    message = source.read_next(timeout_seconds=read_timeout)
                except StopIteration:
                    message = None
                except TimeoutError:
                    return self._finish("source_timeout", "No observation within the bounded source timeout; no retry")
                except Exception:
                    return self._finish("source_error", "Read-only source failed; provider details withheld; no automatic retry")
                now = self._gate(cancelled)
                if self._last_mono-read_started > read_timeout:
                    return self._finish("source_timeout", "Source exceeded its requested read deadline; returned payload discarded")
                if message is None:
                    return self._finish("completed")
                self._messages += 1
                if not isinstance(message, ProviderObservation):
                    return self._finish("invalid_observation", "Source must return a typed raw observation")
                digest = sha256(message.raw_payload).hexdigest()
                try:
                    if len(message.raw_payload) > self.config.max_payload_bytes or message.received_at > now:
                        raise ValueError("Observation size/receipt exceeds capture bounds")
                    contracts, quotes, underlying, reasons, accepted = self._map(message, now)
                except _CatalogueReview:
                    self._events.append(CaptureEvent(message.kind, "metadata_review_required", message.received_at, None, digest,
                        reasons=("Catalogue identity, expiry or effective version needs explicit review",)))
                    return self._finish("metadata_review_required", "Capture stopped; no ambiguous metadata was used")
                except (ValueError, TypeError, KeyError, OverflowError, UnicodeError, RecursionError):
                    self._events.append(CaptureEvent(message.kind, "rejected", message.received_at, None, digest,
                        reasons=("Malformed, unsupported, oversized or future-received payload",)))
                    continue
                count = len(contracts) + len(quotes) + len(underlying)
                if not count:
                    self._events.append(CaptureEvent(message.kind, "empty", message.received_at, None, digest,
                        reasons=("No mapped observations; no substitute price",)))
                    continue
                if self._records + count > self.config.max_records:
                    return self._finish("record_limit", "Capture record budget reached before append")
                imported_at = self._gate(cancelled)
                manifest = ImportManifest("Groww " + message.kind, imported_at, message.raw_payload,
                    format="groww-" + message.kind + "-" + message.payload_origin, evidence_kind=self.config.evidence_kind,
                    provider_reference="receipt=" + message.received_at.isoformat() + ";config=" + self.fingerprint)
                archive_time = [imported_at]
                def imported_clock():
                    archive_time[0] = self._gate(cancelled)
                    return archive_time[0]
                try:
                    archive = MarketArchive(self.path)
                    result = archive.append_batch(manifest=manifest, contracts=contracts, quotes=quotes, underlying=underlying,
                        imported_at_clock=imported_clock, pre_commit=lambda: self._gate(cancelled))
                except _CaptureStopped:
                    raise
                except (ValueError, OSError):
                    return self._finish("archive_error", "Archive append failed; no automatic retry or substitute data")
                self._records += count
                self._inserted += result["inserted"];self._duplicates += result["duplicates"]
                completed_at = None
                if result["status"] in {"accepted", "quarantined"}:
                    try:
                        publication = archive.seal_batch(result["manifest_id"], completed_at_clock=lambda: self._gate(cancelled),
                                                        pre_commit=lambda: self._gate(cancelled))
                        completed_at = _aware(datetime.fromisoformat(publication["completed_at"]))
                    except _CaptureStopped:
                        self._events.append(CaptureEvent(message.kind, "unsealed", message.received_at, None,
                            digest, result["manifest_id"], result["inserted"], result["duplicates"], False,
                            ("Data committed but publication marker missing; unavailable to observed analysis",), archive_time[0]))
                        raise
                    except (ValueError, OSError):
                        self._events.append(CaptureEvent(message.kind, "unsealed", message.received_at, None,
                            digest, result["manifest_id"], result["inserted"], result["duplicates"], False,
                            ("Publication failed; data remains unavailable to observed analysis",), archive_time[0]))
                        return self._finish("archive_error", "Publication marker failed; no automatic retry")
                final_reasons = self._final_quality(contracts, quotes, underlying, self._last_wall)
                reasons = tuple(dict.fromkeys((*reasons, *final_reasons)))
                accepted = accepted and not final_reasons
                if result["status"] != "accepted":
                    accepted = False;reasons += ("archive_identity_conflict_quarantined",)
                self._events.append(CaptureEvent(message.kind, result["status"], message.received_at, completed_at,
                    digest, result["manifest_id"], result["inserted"], result["duplicates"], accepted, reasons, archive_time[0]))
                if result["status"] != "accepted":
                    return self._finish("quarantined", "Conflicting source observation retained as quarantined raw evidence")
            return self._finish("message_limit" if self._messages >= self.config.max_messages else "read_limit")
        except _CaptureStopped as stopped:
            return self._finish(stopped.status, "Session stopped before the next archive insertion")


def capture_readiness():
    return {"status": "bridge_implemented_unconnected", "enabled_by_default": False, "orders_supported": False,
            "credential_lookup": False, "source_timeout": "cooperative; late results discarded before commit",
            "provider_evidence": "live_unverified", "requires": ["explicit authorized read-only source",
            "observed catalogue", "reviewed exact contract expiry times", "market-session timestamp and quantity checks"]}

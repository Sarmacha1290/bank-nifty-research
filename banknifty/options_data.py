"""Immutable option market observations and point-in-time quality checks.

This module has no transport, broker client, credentials or order interface.
``live_verified`` is an explicit upstream evidence classification, never inferred
from a receipt time. These structural checks cannot authenticate a provider.

Default limits (5 s quote age, 1 s leg skew and 30 s OI age) are configurable
operational rejection limits for research, not a claim of suitable trading
latency. Source clocks must be reviewed before a live adapter can be approved.
Historical candles cannot supply a quote, a spread, OI freshness or Greeks.
"""

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from hashlib import sha256
import json
import math
from typing import Iterable, Literal


EvidenceKind = Literal["synthetic", "replay_quote", "live_unverified", "live_verified", "historical_candle"]
EVIDENCE_KINDS = frozenset(("synthetic", "replay_quote", "live_unverified", "live_verified", "historical_candle"))


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _canonical(value):
    if is_dataclass(value):
        return {field.name: _canonical(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Decimal):
        if not value.is_finite():
            return {"decimal": str(value)}
        sign, digits, exponent = value.as_tuple()
        digits = list(digits)
        while len(digits) > 1 and digits[-1] == 0:
            digits.pop()
            exponent += 1
        if not any(digits):
            sign, exponent = 0, 0
        return {"decimal": [sign, "".join(str(digit) for digit in digits), exponent]}
    if isinstance(value, datetime):
        if not _aware(value):
            raise ValueError("Fingerprint timestamps must be timezone aware")
        return {"datetime": value.astimezone(timezone.utc).isoformat()}
    if isinstance(value, bytes):
        return {"bytes_sha256": sha256(value).hexdigest(), "length": len(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Fingerprint mappings require string keys")
        return {key: _canonical(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("Unsupported fingerprint input")


def canonical_fingerprint(*values: object) -> str:
    """Stable SHA-256 for frozen decision inputs; this does not prove authenticity."""
    content = json.dumps(_canonical(values), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RawProvenance:
    """Raw source material with its observation/capture time.

    For reconstructed imports the adapter must label evidence ``replay_quote``
    and retain actual import time separately in the raw wrapper. The supplied
    historical capture time remains an unauthenticated replay claim.
    """

    source: str
    payload: bytes = field(repr=False)
    captured_at: datetime
    evidence_kind: EvidenceKind

    def __post_init__(self):
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("A source identifier is required")
        if not isinstance(self.payload, bytes):
            raise TypeError("Raw payload must be immutable bytes")
        if not _aware(self.captured_at):
            raise ValueError("Provenance capture time must be timezone aware")
        if self.evidence_kind not in EVIDENCE_KINDS:
            raise ValueError("Unsupported evidence kind")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self)

    @property
    def payload_sha256(self) -> str:
        return sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class ContractMetadata:
    """A supplied catalogue version, valid on [effective_from, effective_until).

    ``known_at`` is when this version became available to the researcher. Both
    validity and knowledge times must pass the decision cutoff. Expiry, lot and
    tick values are required for each version; none is inferred from today's
    contract calendar. ``listed`` must describe an actual catalogue entry.
    """

    contract_id: str
    underlying: str
    exchange: str
    option_type: Literal["CALL", "PUT"]
    strike: Decimal
    expiry_at: datetime
    lot_size: int
    tick_size: Decimal
    effective_from: datetime
    effective_until: datetime | None
    known_at: datetime
    listed: bool
    provenance: RawProvenance

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self)


@dataclass(frozen=True)
class OptionQuote:
    """An observed option book top, not a fill or an inferred candle price.

    ``source_at`` is the documented quote timestamp, never last-trade time or
    local receipt substituted for it. OI has its own timestamp. ``None`` OI
    means missing; integer zero is a valid observed value. Sizes are units.
    """

    contract_id: str
    source_at: datetime | None
    received_at: datetime
    bid: Decimal | None
    ask: Decimal | None
    provenance: RawProvenance
    bid_size: int | None = None
    ask_size: int | None = None
    last: Decimal | None = None
    open_interest: int | None = None
    oi_source_at: datetime | None = None
    sequence: int | None = None

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self)


@dataclass(frozen=True)
class QualityPolicy:
    max_source_age_seconds: float = 5
    max_receipt_age_seconds: float = 5
    max_future_skew_seconds: float = 1
    max_leg_skew_seconds: float = 1
    max_oi_age_seconds: float = 30
    require_live_verified: bool = False
    require_fresh_oi: bool = False
    require_sizes: bool = False

    def __post_init__(self):
        for name in ("max_source_age_seconds", "max_receipt_age_seconds", "max_future_skew_seconds",
                     "max_leg_skew_seconds", "max_oi_age_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative number")
        for name in ("require_live_verified", "require_fresh_oi", "require_sizes"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")


@dataclass(frozen=True)
class QualityResult:
    accepted: bool
    reasons: tuple[str, ...]
    source_age_seconds: float | None = None
    receipt_age_seconds: float | None = None
    oi_freshness: str = "missing"
    warnings: tuple[str, ...] = ()
    verified_live: bool = False
    input_fingerprint: str | None = None


def _positive_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def _integer(value: object, *, positive: bool = False) -> bool:
    return type(value) is int and value >= (1 if positive else 0)


def _on_tick(value: Decimal, tick: Decimal) -> bool:
    try:
        return value % tick == 0
    except DecimalException:
        return False


def validate_contract(contract: ContractMetadata, as_of: datetime) -> tuple[str, ...]:
    """Return rejection codes for a supplied catalogue version at a cutoff."""
    if not isinstance(contract, ContractMetadata):
        raise TypeError("Expected ContractMetadata")
    if not _aware(as_of):
        raise ValueError("Decision cutoff must be timezone aware")
    reasons = []
    for name in ("contract_id", "underlying", "exchange"):
        value = getattr(contract, name)
        if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
            reasons.append(f"invalid_{name}")
    if contract.option_type not in ("CALL", "PUT"):
        reasons.append("invalid_option_type")
    if not _positive_decimal(contract.strike):
        reasons.append("invalid_strike")
    if not _positive_decimal(contract.tick_size):
        reasons.append("invalid_tick_size")
    if not _integer(contract.lot_size, positive=True):
        reasons.append("invalid_lot_size")
    if contract.listed is not True:
        reasons.append("contract_not_listed")
    for name in ("expiry_at", "effective_from", "known_at"):
        if not _aware(getattr(contract, name)):
            reasons.append(f"invalid_{name}")
    if contract.effective_until is not None and not _aware(contract.effective_until):
        reasons.append("invalid_effective_until")
    if not isinstance(contract.provenance, RawProvenance):
        reasons.append("missing_contract_provenance")
    else:
        if contract.provenance.captured_at > as_of:
            reasons.append("contract_provenance_after_cutoff")
        if contract.provenance.evidence_kind == "historical_candle":
            reasons.append("candle_is_not_contract_catalogue")
    if _aware(contract.expiry_at) and contract.expiry_at <= as_of:
        reasons.append("contract_expired")
    if _aware(contract.known_at) and contract.known_at > as_of:
        reasons.append("metadata_not_known_at_cutoff")
    if _aware(contract.effective_from):
        if contract.effective_from > as_of:
            reasons.append("metadata_not_yet_effective")
        if _aware(contract.effective_until):
            if contract.effective_until <= contract.effective_from:
                reasons.append("invalid_metadata_interval")
            if as_of >= contract.effective_until:
                reasons.append("metadata_no_longer_effective")
        if _aware(contract.expiry_at) and contract.effective_from >= contract.expiry_at:
            reasons.append("metadata_effective_after_expiry")
    return tuple(reasons)


def select_contract_version(versions: Iterable[ContractMetadata], contract_id: str,
                            as_of: datetime) -> ContractMetadata | None:
    """Resolve exactly one usable version, blocking conflicting catalogue rows."""
    matches = [item for item in versions if item.contract_id == contract_id and not validate_contract(item, as_of)]
    unique = {item.fingerprint: item for item in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def validate_quote(quote: OptionQuote, contract: ContractMetadata, as_of: datetime,
                   policy: QualityPolicy = QualityPolicy()) -> QualityResult:
    """Validate observed prices and timestamp lineage without modifying inputs."""
    if not isinstance(quote, OptionQuote) or not isinstance(policy, QualityPolicy):
        raise TypeError("Expected OptionQuote and QualityPolicy")
    reasons = list(validate_contract(contract, as_of))
    warnings = []
    if quote.contract_id != contract.contract_id:
        reasons.append("contract_identity_mismatch")
    if not _aware(quote.received_at):
        reasons.append("invalid_receipt_timestamp")
    if quote.source_at is None:
        reasons.append("unknown_source_freshness")
    elif not _aware(quote.source_at):
        reasons.append("invalid_source_timestamp")
    if not isinstance(quote.provenance, RawProvenance):
        reasons.append("missing_quote_provenance")
    else:
        if quote.provenance.evidence_kind == "historical_candle":
            reasons.append("historical_candle_is_not_quote")
        if policy.require_live_verified and quote.provenance.evidence_kind != "live_verified":
            reasons.append("live_evidence_unverified")
        if quote.provenance.captured_at > as_of:
            reasons.append("quote_provenance_after_cutoff")
    if policy.require_live_verified and (not isinstance(contract.provenance, RawProvenance)
                                         or contract.provenance.evidence_kind != "live_verified"):
        reasons.append("live_metadata_unverified")
    source_age = (as_of - quote.source_at).total_seconds() if _aware(quote.source_at) else None
    receipt_age = (as_of - quote.received_at).total_seconds() if _aware(quote.received_at) else None
    if receipt_age is not None:
        if receipt_age < 0:
            reasons.append("receipt_after_cutoff")
        if receipt_age > policy.max_receipt_age_seconds:
            reasons.append("stale_receipt")
    if source_age is not None:
        if source_age < -policy.max_future_skew_seconds:
            reasons.append("source_timestamp_in_future")
        if source_age > policy.max_source_age_seconds:
            reasons.append("stale_source")
        if _aware(quote.received_at) and (quote.source_at - quote.received_at).total_seconds() > policy.max_future_skew_seconds:
            reasons.append("source_after_receipt")
    # A version selected at the decision cutoff cannot reinterpret an older
    # observation using changed lot/tick/expiry terms. OI units also belong to
    # the metadata interval in which they were observed.
    for name in ("source_at", "received_at", "oi_source_at"):
        stamp = getattr(quote, name)
        if _aware(stamp) and _aware(contract.effective_from):
            if stamp < contract.effective_from or (_aware(contract.effective_until) and stamp >= contract.effective_until):
                reasons.append(f"{name}_outside_metadata_interval")
    for name in ("bid", "ask"):
        value = getattr(quote, name)
        if value is None:
            reasons.append(f"missing_{name}")
        elif not _positive_decimal(value):
            reasons.append(f"invalid_{name}")
        elif _positive_decimal(contract.tick_size) and not _on_tick(value, contract.tick_size):
            reasons.append(f"{name}_off_tick")
    if _positive_decimal(quote.bid) and _positive_decimal(quote.ask) and quote.bid > quote.ask:
        reasons.append("crossed_book")
    if quote.last is not None and not _positive_decimal(quote.last):
        reasons.append("invalid_last")
    for name in ("bid_size", "ask_size"):
        value = getattr(quote, name)
        if value is not None and not _integer(value):
            reasons.append(f"invalid_{name}")
        if policy.require_sizes and not _integer(value, positive=True):
            reasons.append(f"unavailable_{name}")
    if quote.sequence is not None and not _integer(quote.sequence):
        reasons.append("invalid_sequence")
    oi_freshness = "missing"
    if quote.open_interest is not None:
        oi_freshness = "unknown"
        if not _integer(quote.open_interest):
            reasons.append("invalid_open_interest")
        if quote.oi_source_at is not None and not _aware(quote.oi_source_at):
            reasons.append("invalid_oi_timestamp")
        elif _aware(quote.oi_source_at):
            oi_age = (as_of - quote.oi_source_at).total_seconds()
            # Clock-skew tolerance is not evidence that a field from after the
            # decision cutoff was already known. OI uses an exact cutoff.
            if oi_age < 0:
                oi_freshness = "future"
                reasons.append("oi_timestamp_in_future")
            elif _aware(quote.received_at) and quote.oi_source_at > quote.received_at:
                oi_freshness = "future"
                reasons.append("oi_after_receipt")
            else:
                oi_freshness = "stale" if oi_age > policy.max_oi_age_seconds else "fresh"
    elif quote.oi_source_at is not None:
        reasons.append("oi_timestamp_without_value")
    if oi_freshness != "fresh":
        (reasons if policy.require_fresh_oi else warnings).append(f"oi_{oi_freshness}")
    verified = (not reasons and isinstance(quote.provenance, RawProvenance)
                and quote.provenance.evidence_kind == "live_verified"
                and isinstance(contract.provenance, RawProvenance)
                and contract.provenance.evidence_kind == "live_verified")
    try:
        fingerprint = canonical_fingerprint(quote, contract, as_of, policy)
    except (TypeError, ValueError):
        fingerprint = None
    return QualityResult(not reasons, tuple(reasons), source_age, receipt_age, oi_freshness,
                         tuple(warnings), verified, fingerprint)


def validate_legs(quotes: Iterable[OptionQuote], contracts: Iterable[ContractMetadata],
                  as_of: datetime, policy: QualityPolicy = QualityPolicy()) -> QualityResult:
    """Require one quote per contract, compatible units and synchronized legs.

    This is a data gate, not a strategy payoff or margin calculation. Different
    expiries, exchanges, underlyings, lot sizes or evidence kinds cannot be
    combined by this first supported spread/research interface.
    """
    rows, metadata = tuple(quotes), tuple(contracts)
    if not isinstance(policy, QualityPolicy):
        raise TypeError("Expected QualityPolicy")
    if not _aware(as_of):
        raise ValueError("Decision cutoff must be timezone aware")
    if not rows or not metadata:
        return QualityResult(False, ("missing_legs",))
    if any(not isinstance(item, OptionQuote) for item in rows) or any(not isinstance(item, ContractMetadata) for item in metadata):
        raise TypeError("Expected option quote and contract sequences")
    ids = [item.contract_id for item in rows]
    contract_ids = [item.contract_id for item in metadata]
    if any(not isinstance(value, str) or not value.strip() for value in ids + contract_ids):
        return QualityResult(False, ("invalid_leg_identity",))
    reasons = []
    if len(set(ids)) != len(ids) or len(set(contract_ids)) != len(contract_ids):
        reasons.append("duplicate_leg_identity")
    if set(ids) != set(contract_ids):
        reasons.append("missing_or_extra_leg_metadata")
    lookup = {item.contract_id: item for item in metadata}
    results = [validate_quote(item, lookup[item.contract_id], as_of, policy)
               for item in rows if item.contract_id in lookup]
    for item, result in zip((row for row in rows if row.contract_id in lookup), results):
        reasons.extend(f"{item.contract_id}:{reason}" for reason in result.reasons)
    for name in ("underlying", "exchange", "expiry_at", "lot_size"):
        if any(getattr(item, name) != getattr(metadata[0], name) for item in metadata[1:]):
            reasons.append(f"mismatched_leg_{name}")
    provenances = [item.provenance for item in rows if isinstance(item.provenance, RawProvenance)]
    if len({item.evidence_kind for item in provenances}) != 1:
        reasons.append("mixed_leg_evidence")
    if len({item.source for item in provenances}) != 1:
        reasons.append("mixed_leg_sources")
    for name in ("source_at", "received_at"):
        stamps = [getattr(item, name) for item in rows if _aware(getattr(item, name))]
        if len(stamps) == len(rows) and (max(stamps) - min(stamps)).total_seconds() > policy.max_leg_skew_seconds:
            reasons.append(f"unsynchronized_leg_{name}")
    source_ages = [item.source_age_seconds for item in results if item.source_age_seconds is not None]
    receipt_ages = [item.receipt_age_seconds for item in results if item.receipt_age_seconds is not None]
    warnings = tuple(dict.fromkeys(reason for result in results for reason in result.warnings))
    oi_states = {item.oi_freshness for item in results}
    try:
        fingerprint = canonical_fingerprint(rows, metadata, as_of, policy)
    except (TypeError, ValueError):
        fingerprint = None
    return QualityResult(not reasons, tuple(reasons), max(source_ages, default=None),
                         max(receipt_ages, default=None), next(iter(oi_states)) if len(oi_states) == 1 else "mixed",
                         warnings, bool(not reasons and results and all(item.verified_live for item in results)), fingerprint)


@dataclass(frozen=True)
class IngestionResult:
    status: Literal["accepted", "duplicate", "out_of_order", "rejected"]
    quality: QualityResult
    quote: OptionQuote
    contract_fingerprint: str | None


def _observation_fingerprint(quote: OptionQuote) -> str:
    """Exclude receipt/raw wrapper so duplicates never make old data fresh."""
    return canonical_fingerprint(quote.contract_id, quote.source_at, quote.bid, quote.ask,
                                 quote.bid_size, quote.ask_size, quote.last, quote.open_interest,
                                 quote.oi_source_at, quote.sequence, quote.provenance.source,
                                 quote.provenance.evidence_kind)


class QuoteBook:
    """In-memory accepted state plus all immutable ingestion results.

    Instantiate one book per bounded session. Source switches require a fresh
    book, preventing sequence numbers or clock domains from being combined.
    ``latest`` returns the observation unchanged; consumers must revalidate at
    each new decision cutoff. Rejected data remains available in ``events``.
    """

    def __init__(self):
        self._latest: dict[str, OptionQuote] = {}
        self._events: list[IngestionResult] = []

    @property
    def events(self) -> tuple[IngestionResult, ...]:
        return tuple(self._events)

    def latest(self, contract_id: str) -> OptionQuote | None:
        return self._latest.get(contract_id)

    def ingest(self, quote: OptionQuote, contract: ContractMetadata, as_of: datetime,
               policy: QualityPolicy = QualityPolicy()) -> IngestionResult:
        quality = validate_quote(quote, contract, as_of, policy)
        previous = self._latest.get(quote.contract_id) if isinstance(quote.contract_id, str) else None
        status = "accepted" if quality.accepted else "rejected"
        if quality.accepted and previous is not None:
            code = None
            if previous.provenance.source != quote.provenance.source:
                code = "source_changed_requires_new_book"
            elif previous.provenance.evidence_kind != quote.provenance.evidence_kind:
                code = "evidence_changed_requires_new_book"
            elif _observation_fingerprint(previous) == _observation_fingerprint(quote):
                status = "duplicate"
            elif quote.source_at < previous.source_at:
                status, code = "out_of_order", "source_timestamp_regressed"
            elif quote.received_at < previous.received_at:
                status, code = "out_of_order", "receipt_timestamp_regressed"
            elif previous.sequence is not None and quote.sequence is not None and quote.sequence < previous.sequence:
                status, code = "out_of_order", "sequence_regressed"
            elif previous.sequence is not None and quote.sequence is None:
                code = "sequence_disappeared"
            elif previous.sequence is not None and quote.sequence == previous.sequence:
                code = "conflicting_sequence"
            elif quote.source_at == previous.source_at and (quote.sequence is None or previous.sequence is None):
                code = "conflicting_source_timestamp"
            if code is not None:
                if status != "out_of_order":
                    status = "rejected"
                quality = QualityResult(False, (code,), quality.source_age_seconds, quality.receipt_age_seconds,
                                        quality.oi_freshness, quality.warnings, False, quality.input_fingerprint)
        if status == "accepted":
            self._latest[quote.contract_id] = quote
        try:
            metadata_fingerprint = contract.fingerprint
        except (ValueError, TypeError):
            metadata_fingerprint = None
        result = IngestionResult(status, quality, quote, metadata_fingerprint)
        self._events.append(result)
        return result

"""Documented Groww market-data mappings; no transport/account capability.

Adapters map observations, never certify a live session. Every result is
live_unverified. Unknown fields are retained in raw provenance but cannot
populate timestamps. SDK depth's documented milliseconds are never guessed
from magnitude; the inconsistent seconds-sized documentation example is old
when interpreted literally and fails the downstream freshness gate.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import csv
import io
import json
from typing import Iterable

from .options_data import ContractMetadata, OptionQuote, RawProvenance
from .options_pipeline import UnderlyingObservation

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CATALOGUE_BYTES = 32_000_000
MAX_CHAIN_CONTRACTS = 2000
IST = timezone(timedelta(hours=5, minutes=30))
FEED_DOC = "https://groww.in/trade-api/docs/python-sdk/feed"
LIVE_DOC = "https://groww.in/trade-api/docs/curl/live-data"
CATALOGUE_DOC = "https://groww.in/trade-api/docs/curl/instruments"
_SECRET_KEYS = frozenset({"access_token", "api_key", "authorization", "password", "secret", "client_secret", "refresh_token"})


def _aware(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


def _text(value, name):
    if (not isinstance(value, str) or not 1 <= len(value) <= 120 or not value.isascii()
            or not all(c.isalnum() or c in "-_.:" for c in value)):
        raise ValueError(f"Invalid {name}")
    return value


def _decimal(value, name, *, optional=False, signed=False, maximum=Decimal("1e12")):
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"Invalid {name}")
    if len(str(value)) > 100:
        raise ValueError(f"Invalid {name}")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"Invalid {name}") from None
    if not result.is_finite() or result.copy_abs() > maximum or (not signed and result < 0):
        raise ValueError(f"Invalid {name}")
    return result


def _integer(value, name, *, optional=False):
    number = _decimal(value, name, optional=optional)
    if number is None:
        return None
    if number != number.to_integral_value():
        raise ValueError(f"Invalid {name}")
    return int(number)


def _check_raw(value, depth=0):
    if depth > 30:
        raise ValueError("Provider payload nesting exceeds limit")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Provider keys must be strings")
        if any(key.lower() in _SECRET_KEYS for key in value):
            raise ValueError("Credential fields do not belong in market-data payloads")
        for item in value.values():
            _check_raw(item, depth + 1)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _check_raw(item, depth + 1)
    elif not isinstance(value, (str, int, float, Decimal, bool, type(None))):
        raise ValueError("Unsupported provider payload value")


def _bytes(value):
    _check_raw(value)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                             default=lambda x: str(x) if isinstance(x, Decimal) else None).encode("utf-8")
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Invalid provider payload") from None
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ValueError("Provider response exceeds size limit")
    return encoded


def decode_provider_json(raw: bytes) -> dict:
    """Decode exact decimal lexemes; reject duplicate keys and credential data."""
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Expected bounded provider JSON bytes")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate provider JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8-sig"), parse_float=Decimal, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite provider number")))
        if not isinstance(value, dict):
            raise ValueError("Expected provider response object")
        _bytes(value)
        return value
    except (UnicodeError, RecursionError, json.JSONDecodeError):
        raise ValueError("Malformed provider JSON") from None


def _provenance(payload, received_at, kind):
    _aware(received_at, "received_at")
    return RawProvenance("Groww " + kind, _bytes(payload), received_at, "live_unverified")


def _milliseconds(value):
    if value is None:
        return None
    number = _decimal(value, "epoch milliseconds", maximum=Decimal("253402300799999"))
    if number <= 0 or number != number.to_integral_value():
        raise ValueError("Invalid epoch milliseconds")
    stamp = int(number)
    try:
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=stamp)
    except (OverflowError, ValueError):
        raise ValueError("Epoch milliseconds outside supported range") from None


@dataclass(frozen=True)
class ProviderInstrument:
    exchange: str
    segment: str
    exchange_token: str
    trading_symbol: str
    groww_symbol: str
    instrument_type: str
    underlying_symbol: str
    expiry_date: date | None
    strike_price: Decimal | None
    lot_size: int | None
    tick_size: Decimal | None
    observed_at: datetime
    provenance: RawProvenance

    @property
    def contract_id(self):
        return self.trading_symbol


@dataclass(frozen=True)
class CatalogueSnapshot:
    instruments: tuple[ProviderInstrument, ...]
    observed_at: datetime
    raw: bytes = field(repr=False)
    file_sha256: str


@dataclass(frozen=True)
class ProviderField:
    name: str
    value: Decimal | int | None
    source_at: datetime | None
    received_at: datetime
    provenance: RawProvenance


@dataclass(frozen=True)
class MappedOption:
    quote: OptionQuote
    fields: tuple[ProviderField, ...]
    issues: tuple[str, ...]


@dataclass(frozen=True)
class MappedChain:
    options: tuple[MappedOption, ...]
    underlying: UnderlyingObservation | None
    issues: tuple[str, ...]


@dataclass(frozen=True)
class MarketDataRead:
    """An inert allowlisted request plan. It carries no credentials or client."""
    path: str
    params: tuple[tuple[str, str], ...]
    method: str = field(default="GET", init=False)


def plan_option_quote(instrument: ProviderInstrument) -> MarketDataRead:
    _option(instrument)
    return MarketDataRead("/live-data/quote", (("exchange", instrument.exchange),
                           ("segment", instrument.segment), ("trading_symbol", instrument.trading_symbol)))


def plan_index_ltp(instrument: ProviderInstrument) -> MarketDataRead:
    _index(instrument)
    return MarketDataRead("/live-data/ltp", (("segment", "CASH"),
                           ("exchange_symbols", instrument.exchange + "_" + instrument.trading_symbol)))


def plan_option_chain(expiry_date: date) -> MarketDataRead:
    if type(expiry_date) is not date:
        raise ValueError("Explicit expiry date required")
    return MarketDataRead("/option-chain/exchange/NSE/underlying/BANKNIFTY", (("expiry_date", expiry_date.isoformat()),))


def parse_groww_catalogue(csv_text: str, observed_at: datetime) -> CatalogueSnapshot:
    """Keep observed NSE BANKNIFTY index/options, never a reconstructed listing.

    Unknown CSV columns survive per-record provenance. There is no effective
    timestamp in this format: first local observation is the earliest usable
    catalogue time, and expired rows are retained for explicit diagnostics.
    """
    _aware(observed_at, "observed_at")
    if not isinstance(csv_text, str) or len(csv_text.encode("utf-8")) > MAX_CATALOGUE_BYTES:
        raise ValueError("Catalogue must be bounded CSV text")
    raw = csv_text.encode("utf-8")
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    required = {"exchange", "segment", "exchange_token", "trading_symbol", "groww_symbol", "instrument_type",
                "underlying_symbol", "expiry_date", "strike_price", "lot_size", "tick_size"}
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or not required <= set(reader.fieldnames):
        raise ValueError("Catalogue columns do not match documented Groww fields")
    if any(name.lower() in _SECRET_KEYS for name in reader.fieldnames):
        raise ValueError("Credential columns do not belong in instrument catalogues")
    records, identities, tokens = [], {}, {}
    for index, row in enumerate(reader):
        if index >= 200000 or None in row or any(value is None for value in row.values()):
            raise ValueError("Malformed or oversized catalogue")
        selected_index = (row["exchange"], row["segment"], row["instrument_type"], row["trading_symbol"]) == ("NSE", "CASH", "IDX", "BANKNIFTY")
        selected_option = (row["exchange"], row["segment"], row["underlying_symbol"]) == ("NSE", "FNO", "BANKNIFTY") and row["instrument_type"] in {"CE", "PE"}
        if not (selected_index or selected_option):
            continue
        identity = _text(row["trading_symbol"], "trading symbol")
        token = _text(row["exchange_token"], "exchange token")
        groww_symbol = _text(row["groww_symbol"], "Groww symbol")
        if selected_index and groww_symbol != "NSE-BANKNIFTY":
            raise ValueError("Catalogue index identity mismatch")
        try:
            expiry = date.fromisoformat(row["expiry_date"]) if selected_option else None
        except ValueError:
            raise ValueError("Catalogue option expiry date is invalid") from None
        strike = _decimal(row["strike_price"], "strike") if selected_option else None
        lot = _integer(row["lot_size"], "lot size") if selected_option else None
        tick = _decimal(row["tick_size"], "tick size") if selected_option else None
        if selected_option and (strike <= 0 or lot <= 0 or tick <= 0):
            raise ValueError("Option catalogue needs positive strike, lot and tick")
        if selected_option and (not identity.startswith("BANKNIFTY") or not identity.endswith(row["instrument_type"])):
            raise ValueError("Catalogue option identity mismatch")
        item = ProviderInstrument("NSE", row["segment"], token, identity, groww_symbol, row["instrument_type"],
                                  "BANKNIFTY", expiry, strike, lot, tick, observed_at,
                                  _provenance(row, observed_at, "instrument catalogue"))
        token_key = (item.exchange, item.segment, token)
        if identity in identities or token_key in tokens:
            if identities.get(identity) == item and tokens.get(token_key) == item:
                continue
            raise ValueError("Conflicting catalogue instrument identity")
        identities[identity], tokens[token_key] = item, item
        records.append(item)
    if not records:
        raise ValueError("No documented NSE BANKNIFTY index or option records in catalogue")
    return CatalogueSnapshot(tuple(records), observed_at, raw, sha256(raw).hexdigest())


def instrument_contract(instrument: ProviderInstrument, *, expiry_at: datetime,
                        effective_until: datetime | None = None) -> ContractMetadata:
    """Require an explicitly reviewed expiry clock; catalogue supplies only date."""
    _option(instrument)
    _aware(expiry_at, "expiry_at")
    if expiry_at.astimezone(IST).date() != instrument.expiry_date or expiry_at <= instrument.observed_at:
        raise ValueError("Reviewed expiry time must match catalogue date and follow observation")
    if effective_until is not None:
        _aware(effective_until, "effective_until")
        if effective_until <= instrument.observed_at:
            raise ValueError("Metadata interval must follow catalogue observation")
    return ContractMetadata(instrument.contract_id, instrument.underlying_symbol, instrument.exchange,
                            "CALL" if instrument.instrument_type == "CE" else "PUT", instrument.strike_price,
                            expiry_at, instrument.lot_size, instrument.tick_size, instrument.observed_at,
                            effective_until, instrument.observed_at, True, instrument.provenance)


def _instrument(instrument, receipt):
    if not isinstance(instrument, ProviderInstrument):
        raise TypeError("Expected catalogue-resolved ProviderInstrument")
    _aware(receipt, "received_at")
    _aware(instrument.observed_at, "instrument observation time")
    if instrument.observed_at > receipt:
        raise ValueError("Instrument identity was not observed before this response")


def _option(instrument):
    if not isinstance(instrument, ProviderInstrument) or (instrument.exchange, instrument.segment) != ("NSE", "FNO") or instrument.instrument_type not in {"CE", "PE"}:
        raise ValueError("Expected observed NSE option instrument")


def _index(instrument):
    if not isinstance(instrument, ProviderInstrument) or (instrument.exchange, instrument.segment, instrument.instrument_type, instrument.trading_symbol) != ("NSE", "CASH", "IDX", "BANKNIFTY"):
        raise ValueError("Expected observed NSE BANKNIFTY index")


def _envelope(response):
    _bytes(response)
    if not isinstance(response, dict) or response.get("status") != "SUCCESS" or not isinstance(response.get("payload"), dict):
        raise ValueError("Expected successful documented Groww payload")
    return response["payload"]


def _feed_row(payload, instrument, receipt, *, ltp=False):
    _instrument(instrument, receipt)
    _bytes(payload)
    if not isinstance(payload, dict):
        raise ValueError("Expected feed object")
    try:
        base = payload["ltp"] if ltp else payload
        row = base[instrument.exchange][instrument.segment][instrument.exchange_token]
    except (KeyError, TypeError):
        return None
    if row is None:
        return None
    if not isinstance(row, dict):
        raise ValueError("Unexpected feed instrument record")
    return row


def adapt_index_feed(payload, instrument, received_at):
    _index(instrument)
    row = _feed_row(payload, instrument, received_at)
    if row is None:
        return None
    value = _decimal(row.get("value"), "index value")
    if value <= 0:
        raise ValueError("Index value must be positive")
    return UnderlyingObservation(instrument.underlying_symbol, value, _milliseconds(row.get("tsInMillis")),
                                 received_at, _provenance(row, received_at, "index feed"))


def adapt_rest_ltp(response, instrument, received_at):
    _index(instrument)
    _instrument(instrument, received_at)
    key = instrument.exchange + "_" + instrument.trading_symbol
    value = _decimal(_envelope(response).get(key), "index value")
    if value <= 0:
        raise ValueError("Index value must be positive")
    return UnderlyingObservation(instrument.underlying_symbol, value, None, received_at,
                                 _provenance(response, received_at, "REST LTP"))


def adapt_option_quote(response, instrument, received_at):
    _option(instrument)
    _instrument(instrument, received_at)
    row = _envelope(response)
    provenance = _provenance(response, received_at, "REST quote")
    last_time = _milliseconds(row.get("last_trade_time"))
    last = _decimal(row.get("last_price"), "last price", optional=True)
    oi = _integer(row.get("open_interest"), "open interest", optional=True)
    quote = OptionQuote(instrument.contract_id, None, received_at,
                        _decimal(row.get("bid_price"), "bid price", optional=True),
                        _decimal(row.get("offer_price"), "offer price", optional=True), provenance,
                        _integer(row.get("bid_quantity"), "bid quantity", optional=True),
                        _integer(row.get("offer_quantity"), "offer quantity", optional=True), last, oi)
    fields = (ProviderField("last_price", last, last_time, received_at, provenance),
              ProviderField("open_interest", oi, None, received_at, provenance),
              ProviderField("volume", _integer(row.get("volume"), "volume", optional=True), None, received_at, provenance),
              ProviderField("implied_volatility", _decimal(row.get("implied_volatility"), "implied volatility", optional=True), None, received_at, provenance))
    return MappedOption(quote, fields, ("quote_timestamp_not_documented", "oi_timestamp_not_documented", "last_trade_time_is_not_book_time"))


def _top(book, side):
    if book is None or book == {}:
        return None, None
    if not isinstance(book, dict) or len(book) > 100:
        raise ValueError("Unexpected feed depth book")
    levels = []
    for key, row in book.items():
        if _integer(key, "depth rank") <= 0 or not isinstance(row, dict):
            raise ValueError("Invalid feed depth level")
        price, size = _decimal(row.get("price"), "depth price"), _integer(row.get("qty"), "depth quantity")
        levels.append((price, size))
    if len({price for price, _ in levels}) != len(levels):
        raise ValueError("Duplicate depth prices cannot establish distinct liquidity")
    best = max(price for price, _ in levels) if side == "bid" else min(price for price, _ in levels)
    return best, sum(size for price, size in levels if price == best)


def adapt_option_depth_feed(payload, instrument, received_at):
    _option(instrument)
    row = _feed_row(payload, instrument, received_at)
    if row is None:
        return None
    source = _milliseconds(row.get("tsInMillis"))
    bid, bid_size = _top(row.get("buyBook"), "bid")
    ask, ask_size = _top(row.get("sellBook"), "ask")
    provenance = _provenance(row, received_at, "depth feed")
    issues = ("book_timestamp_missing",) if source is None else ()
    if source is not None and abs((received_at - source).total_seconds()) > 86400:
        issues += ("documented_millisecond_timestamp_far_from_receipt",)
    return MappedOption(OptionQuote(instrument.contract_id, source, received_at, bid, ask, provenance,
                                    bid_size, ask_size), (), issues)


def adapt_option_ltp_feed(payload, instrument, received_at):
    _option(instrument)
    row = _feed_row(payload, instrument, received_at, ltp=True)
    if row is None:
        return None
    trade_at = _milliseconds(row.get("tsInMillis"))
    last = _decimal(row.get("ltp"), "last price")
    provenance = _provenance(row, received_at, "LTP feed")
    # The LTP topic timestamp does not date an absent bid/ask book.
    quote = OptionQuote(instrument.contract_id, None, received_at, None, None, provenance, last=last)
    return MappedOption(quote, (ProviderField("last_price", last, trade_at, received_at, provenance),),
                        ("ltp_is_not_executable_book",))


def adapt_option_chain(response, instruments: Iterable[ProviderInstrument], received_at, *,
                       underlying_instrument: ProviderInstrument, expiry_date: date):
    _index(underlying_instrument)
    _instrument(underlying_instrument, received_at)
    if type(expiry_date) is not date:
        raise ValueError("An explicit option-chain expiry date is required")
    catalogue = {}
    for item in instruments:
        _option(item)
        _instrument(item, received_at)
        if item.trading_symbol in catalogue:
            raise ValueError("Ambiguous option-chain catalogue identity")
        catalogue[item.trading_symbol] = item
    row = _envelope(response)
    strikes = row.get("strikes")
    if not isinstance(strikes, dict) or len(strikes) > MAX_CHAIN_CONTRACTS:
        raise ValueError("Unexpected or oversized option-chain strikes")
    underlying_value = _decimal(row.get("underlying_ltp"), "underlying LTP", optional=True)
    chain_provenance = _provenance(response, received_at, "option chain")
    underlying = None if underlying_value is None else UnderlyingObservation(
        underlying_instrument.underlying_symbol, underlying_value, None, received_at, chain_provenance)
    results, seen, issues = [], set(), ["chain_quote_and_field_timestamps_not_documented"]
    for raw_strike, sides in strikes.items():
        strike = _decimal(raw_strike, "chain strike")
        if not isinstance(sides, dict):
            raise ValueError("Unexpected option-chain sides")
        for side in ("CE", "PE"):
            if side not in sides:
                continue
            option = sides[side]
            if not isinstance(option, dict):
                raise ValueError("Unexpected option-chain contract")
            symbol = _text(option.get("trading_symbol"), "chain trading symbol")
            instrument = catalogue.get(symbol)
            if instrument is None:
                issues.append("unmapped_chain_contract:" + symbol)
                continue
            if (instrument.strike_price != strike or instrument.instrument_type != side or instrument.expiry_date != expiry_date
                    or instrument.underlying_symbol != underlying_instrument.underlying_symbol):
                raise ValueError("Option-chain contract disagrees with observed catalogue")
            if symbol in seen or len(results) >= MAX_CHAIN_CONTRACTS:
                raise ValueError("Duplicate or oversized option-chain contracts")
            seen.add(symbol)
            provenance = _provenance({"strike": raw_strike, "side": side, "record": option}, received_at, "option chain contract")
            last, oi = _decimal(option.get("ltp"), "option LTP", optional=True), _integer(option.get("open_interest"), "open interest", optional=True)
            quote = OptionQuote(symbol, None, received_at, None, None, provenance, last=last, open_interest=oi)
            fields = [ProviderField("last_price", last, None, received_at, provenance),
                      ProviderField("open_interest", oi, None, received_at, provenance),
                      ProviderField("volume", _integer(option.get("volume"), "volume", optional=True), None, received_at, provenance)]
            greeks = option.get("greeks", {})
            if not isinstance(greeks, dict):
                raise ValueError("Unexpected option-chain Greeks")
            for name in ("delta", "gamma", "theta", "vega", "rho", "iv"):
                fields.append(ProviderField(name, _decimal(greeks.get(name), name, optional=True, signed=name in {"delta", "theta", "rho"}),
                                            None, received_at, provenance))
            results.append(MappedOption(quote, tuple(fields), ("chain_has_no_bid_ask_or_source_times",)))
    return MappedChain(tuple(results), underlying, tuple(issues))


def provider_readiness():
    """Static readiness report only: never probes a connection or subscription."""
    return {"provider": "Groww", "status": "mapped_offline_live_unverified", "connection_attempted": False,
            "authenticated": False, "orders_supported": False,
            "feed": {"index": "documented timestamp/value", "option_depth": "documented timestamp/book; unit example discrepancy needs live review"},
            "rest": {"quote": "bid/ask present; quote and OI timestamps absent", "chain": "OI/Greeks available; freshness unknown"},
            "catalogue": "observed versions only; explicit reviewed expiry time required",
            "pending": ["User-authorized market-session connection", "Provider timestamp and depth-unit verification",
                        "Observed catalogue, lot and expiry validation", "OI/Greek freshness evidence"],
            "sources": [FEED_DOC, LIVE_DOC, CATALOGUE_DOC]}

"""Read-only Bank Nifty futures snapshot with explicit receipt and trade times.

This separate prototype is deliberately not a forecast input until market-hours
quote fields, cadence and staleness have been inspected against real responses.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
import csv
import io
import math

from .groww import DataAccessError, IST


def _valid_symbol(value):
    return (isinstance(value, str) and value.startswith("BANKNIFTY") and
            value.endswith("FUT") and value.isascii() and value.isalnum())


@dataclass(frozen=True)
class FutureInstrument:
    trading_symbol: str
    groww_symbol: str
    exchange_token: str
    expiry_date: date


def nearest_banknifty_future(csv_text, on_date):
    """Select the unique earliest non-expired NSE/FNO BANKNIFTY FUT row."""
    if not isinstance(on_date, date) or isinstance(on_date, datetime):
        raise ValueError("on_date must be a date")
    if not isinstance(csv_text, str):
        raise ValueError("Instrument catalogue must be text")
    candidates = []
    for row in csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"))):
        if (row.get("exchange"), row.get("segment"), row.get("underlying_symbol"),
                row.get("instrument_type")) != ("NSE", "FNO", "BANKNIFTY", "FUT"):
            continue
        try:
            expiry = date.fromisoformat(row["expiry_date"])
            symbol = row["trading_symbol"]
            groww_symbol = row["groww_symbol"]
            token = row["exchange_token"]
        except (KeyError, TypeError, ValueError):
            continue
        if (expiry >= on_date and _valid_symbol(symbol) and
                isinstance(groww_symbol, str) and groww_symbol.startswith("NSE-BANKNIFTY-")
                and groww_symbol.endswith("-FUT") and groww_symbol.isascii()
                and all(char.isalnum() or char == "-" for char in groww_symbol)
                and isinstance(token, str) and token.isascii() and token.isdecimal()
                and int(token) > 0):
            candidates.append(FutureInstrument(symbol, groww_symbol, token, expiry))
    if not candidates:
        raise DataAccessError("No current Bank Nifty future found in Groww's catalogue.")
    candidates.sort(key=lambda row: row.expiry_date)
    earliest = [row for row in candidates if row.expiry_date == candidates[0].expiry_date]
    if len(earliest) != 1:
        raise DataAccessError("Bank Nifty futures catalogue is ambiguous at nearest expiry.")
    return earliest[0]


def _number(payload, key, *, positive=False, integer=False, signed=False):
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Futures quote has invalid " + key)
    if ((not signed and value < 0) or (positive and value <= 0) or
            (integer and int(value) != value)):
        raise ValueError("Futures quote has invalid " + key)
    return int(value) if integer else float(value)


@dataclass(frozen=True)
class FutureSnapshot:
    source: str
    trading_symbol: str
    groww_symbol: str
    exchange_token: str
    expiry_date: date
    received_at_utc: datetime
    last_trade_at_utc: datetime | None
    last_price: float
    volume: int | None
    open_interest: int | None
    previous_open_interest: int | None
    oi_day_change: int | None
    freshness: str


def describe_futures_context(snapshot, *, spot_price=None, spot_at=None,
                             previous=None, max_alignment_seconds=5):
    """Conservative basis and price/OI descriptors; never a trade signal."""
    if not isinstance(snapshot, FutureSnapshot):
        raise ValueError("A validated futures snapshot is required")
    if isinstance(max_alignment_seconds, bool) or not isinstance(max_alignment_seconds, (int, float)) or not 0 < max_alignment_seconds <= 60:
        raise ValueError("Invalid timestamp alignment limit")
    premium = basis_pct = annualized_basis_pct = None
    if spot_price is not None or spot_at is not None:
        if (isinstance(spot_price, bool) or not isinstance(spot_price, (int, float)) or
                not math.isfinite(spot_price) or spot_price <= 0 or
                not isinstance(spot_at, datetime) or spot_at.tzinfo is None or
                spot_at.utcoffset() is None):
            raise ValueError("Spot price and aware timestamp must be supplied together")
        if snapshot.last_trade_at_utc is not None:
            spot_utc = spot_at.astimezone(timezone.utc)
            lag = abs((spot_utc - snapshot.last_trade_at_utc).total_seconds())
            receipt_lag = (snapshot.received_at_utc - snapshot.last_trade_at_utc).total_seconds()
            if lag <= max_alignment_seconds and 0 <= receipt_lag <= max_alignment_seconds:
                premium = snapshot.last_price - spot_price
                basis_pct = 100 * premium / spot_price
                days = (snapshot.expiry_date - spot_at.astimezone(IST).date()).days
                annualized_basis_pct = basis_pct * 365 / days if days > 0 else None
    oi_class = None
    if previous is not None:
        if (not isinstance(previous, FutureSnapshot) or
                previous.trading_symbol != snapshot.trading_symbol or
                previous.expiry_date != snapshot.expiry_date or
                previous.received_at_utc >= snapshot.received_at_utc):
            raise ValueError("Previous futures quote must be earlier for the same contract")
        if (previous.open_interest is not None and snapshot.open_interest is not None and
                previous.last_trade_at_utc is not None and snapshot.last_trade_at_utc is not None and
                previous.last_trade_at_utc < snapshot.last_trade_at_utc):
            price_change = snapshot.last_price - previous.last_price
            oi_change = snapshot.open_interest - previous.open_interest
            oi_class = ("long_buildup" if price_change > 0 and oi_change > 0 else
                        "short_buildup" if price_change < 0 and oi_change > 0 else
                        "short_covering" if price_change > 0 and oi_change < 0 else
                        "long_unwinding" if price_change < 0 and oi_change < 0 else
                        "ambiguous_unchanged_price_or_oi")
    return {"trading_symbol": snapshot.trading_symbol,
            "received_at_utc": snapshot.received_at_utc.isoformat(),
            "last_trade_at_utc": (snapshot.last_trade_at_utc.isoformat()
                                  if snapshot.last_trade_at_utc else None),
            "spot_futures_premium_points": premium, "basis_pct": basis_pct,
            "annualized_basis_pct": annualized_basis_pct,
            "volume_oi_ratio": (snapshot.volume / snapshot.open_interest
                                if snapshot.volume is not None and snapshot.open_interest else None),
            "price_oi_pattern": oi_class,
            "interpretation": "descriptive_aligned_quote_only_not_a_forecast"}


def describe_futures_rollover(near, far, *, max_time_gap_seconds=5):
    """Two-contract OI share, only when both trades and receipts are aligned."""
    if (not isinstance(near, FutureSnapshot) or not isinstance(far, FutureSnapshot) or
            near.source != far.source or near.expiry_date >= far.expiry_date or
            near.open_interest is None or far.open_interest is None or
            near.last_trade_at_utc is None or far.last_trade_at_utc is None):
        raise ValueError("Distinct, timed near and far futures quotes with OI are required")
    if (isinstance(max_time_gap_seconds, bool) or
            not isinstance(max_time_gap_seconds, (int, float)) or
            not 0 < max_time_gap_seconds <= 30):
        raise ValueError("Invalid futures alignment limit")
    trade_gap = abs((near.last_trade_at_utc - far.last_trade_at_utc).total_seconds())
    receipt_gap = abs((near.received_at_utc - far.received_at_utc).total_seconds())
    if (trade_gap > max_time_gap_seconds or receipt_gap > max_time_gap_seconds or
            any(not 0 <= (row.received_at_utc - row.last_trade_at_utc).total_seconds()
                <= max_time_gap_seconds for row in (near, far))):
        raise ValueError("Futures rollover quotes are not time-aligned")
    total = near.open_interest + far.open_interest
    return {"near_symbol": near.trading_symbol, "far_symbol": far.trading_symbol,
            "near_expiry": near.expiry_date.isoformat(),
            "far_expiry": far.expiry_date.isoformat(),
            "far_oi_share": far.open_interest / total if total > 0 else None,
            "far_volume_share": (far.volume / (near.volume + far.volume)
                                 if near.volume is not None and far.volume is not None and
                                    near.volume + far.volume > 0 else None),
            "trade_time_gap_seconds": trade_gap,
            "interpretation": "two_contract_OI_share_not_verified_roll_trade_flow"}


def parse_future_quote(response, instrument, received_at, *, source_kind="Groww"):
    """Normalize documented quote fields; never equate trade time with quote time."""
    if source_kind not in ("Groww", "synthetic"):
        raise ValueError("Unknown futures source kind")
    if (not isinstance(instrument, FutureInstrument) or not _valid_symbol(instrument.trading_symbol)
            or not isinstance(instrument.groww_symbol, str)
            or not instrument.groww_symbol.startswith("NSE-BANKNIFTY-")
            or not instrument.groww_symbol.endswith("-FUT")
            or not instrument.groww_symbol.isascii()
            or not all(char.isalnum() or char == "-" for char in instrument.groww_symbol)
            or not isinstance(instrument.exchange_token, str)
            or not instrument.exchange_token.isascii() or not instrument.exchange_token.isdecimal()
            or int(instrument.exchange_token) <= 0
            or not isinstance(instrument.expiry_date, date)
            or isinstance(instrument.expiry_date, datetime)):
        raise ValueError("Invalid futures instrument")
    if not isinstance(received_at, datetime) or received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("Receipt time must be timezone-aware")
    if not isinstance(response, dict) or response.get("status") != "SUCCESS":
        raise ValueError("Futures quote was not successful")
    payload = response.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Futures quote payload is invalid")
    price = _number(payload, "last_price", positive=True)
    if price is None:
        raise ValueError("Futures quote has no last price")
    trade_ms = _number(payload, "last_trade_time", integer=True)
    trade_at = None
    if trade_ms is not None:
        if not 946684800000 <= trade_ms <= 4102444800000:
            raise ValueError("Futures quote has implausible last trade time")
        trade_at = datetime.fromtimestamp(trade_ms / 1000, timezone.utc)
        if trade_at > received_at.astimezone(timezone.utc):
            raise ValueError("Futures last trade is after receipt")
    # Open interest and volumes are nonnegative counts, not executed buy/sell splits.
    return FutureSnapshot(source_kind, instrument.trading_symbol, instrument.groww_symbol,
                          instrument.exchange_token, instrument.expiry_date,
                          received_at.astimezone(timezone.utc), trade_at, price,
                          _number(payload, "volume", integer=True),
                          _number(payload, "open_interest", integer=True),
                          _number(payload, "previous_open_interest", integer=True),
                          _number(payload, "oi_day_change", integer=True, signed=True),
                          "last_trade_time_only" if trade_at else "exchange_time_absent")

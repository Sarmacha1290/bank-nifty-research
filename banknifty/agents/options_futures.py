"""Receipt-time-only Bank Nifty option-chain arithmetic; no prediction.

Groww's documented option-chain response has OI, volume and optional Greeks,
but no exchange timestamp for the chain. This parser never invents one.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isfinite


SOURCE = "Groww Bank Nifty option-chain REST"


def _number(value, label, *, positive=False, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"Invalid {label}")
    if (positive and value <= 0) or (not positive and value < 0):
        raise ValueError(f"Invalid {label}")
    if integer and (not isinstance(value, int)):
        raise ValueError(f"Invalid {label}")
    return value


@dataclass(frozen=True)
class OptionContract:
    strike: float
    side: str
    trading_symbol: str
    open_interest: int
    volume: int
    ltp: float
    iv_percent: float | None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None


@dataclass(frozen=True)
class OptionChainAnalysis:
    expiry_date: date
    received_at_utc: datetime
    exchange_at: None
    source: str
    underlying_ltp: float
    strikes: int
    complete_pairs: int
    call_oi: int
    put_oi: int
    call_volume: int
    put_volume: int
    oi_pcr: float | None
    volume_pcr: float | None
    top_call_oi_strike: float | None
    top_put_oi_strike: float | None
    atm_strike: float
    atm_call_iv_percent: float | None
    atm_put_iv_percent: float | None
    status: str
    contracts: tuple[OptionContract, ...]


def analyze_option_chain(response, expiry_date, received_at, *, source_kind="groww"):
    if source_kind not in ("groww", "synthetic"):
        raise ValueError("Option-chain source must be Groww or synthetic")
    if not isinstance(expiry_date, date) or isinstance(expiry_date, datetime):
        raise ValueError("An explicit expiry date is required")
    if not isinstance(received_at, datetime) or received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("Receipt time must be timezone-aware")
    if not isinstance(response, dict) or response.get("status") != "SUCCESS":
        raise ValueError("Groww option-chain response was not successful")
    payload = response.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Option-chain payload is missing")
    underlying = _number(payload.get("underlying_ltp"), "underlying LTP", positive=True)
    strikes = payload.get("strikes")
    if not isinstance(strikes, dict) or not 1 <= len(strikes) <= 1000:
        raise ValueError("Option-chain strikes are missing or excessive")
    contracts, complete = [], 0
    numeric_strikes = set()
    for strike_label, sides in strikes.items():
        try:
            strike = float(strike_label)
        except (TypeError, ValueError):
            raise ValueError("Invalid option strike") from None
        _number(strike, "option strike", positive=True)
        if not strike.is_integer():
            raise ValueError("Bank Nifty option strike must be an integer")
        if strike in numeric_strikes or not isinstance(sides, dict):
            raise ValueError("Duplicate or invalid option strike")
        numeric_strikes.add(strike)
        present = 0
        for side in ("CE", "PE"):
            row = sides.get(side)
            if row is None:
                continue
            if not isinstance(row, dict):
                raise ValueError("Invalid option contract row")
            symbol = row.get("trading_symbol")
            if (not isinstance(symbol, str) or not symbol.isascii() or not symbol.isalnum() or
                    symbol != symbol.upper() or not symbol.startswith("BANKNIFTY") or
                    not symbol.endswith(f"{int(strike)}{side}")):
                raise ValueError("Option contract symbol does not match Bank Nifty, strike and side")
            oi = _number(row.get("open_interest"), "open interest", integer=True)
            volume = _number(row.get("volume"), "volume", integer=True)
            ltp = _number(row.get("ltp"), "option LTP")
            greeks = row.get("greeks")
            if greeks is not None and not isinstance(greeks, dict):
                raise ValueError("Invalid option Greeks")
            greeks = greeks or {}
            iv = greeks.get("iv")
            if iv is not None:
                iv = _number(iv, "implied volatility")
            parsed = {}
            for name in ("delta", "gamma", "theta", "vega", "rho"):
                value = greeks.get(name)
                if value is not None:
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                        raise ValueError(f"Invalid option {name}")
                    if name in ("gamma", "vega") and value < 0:
                        raise ValueError(f"Invalid option {name}")
                    if name == "delta" and not (-1 <= value <= 1):
                        raise ValueError("Invalid option delta")
                    parsed[name] = float(value)
                else:
                    parsed[name] = None
            contracts.append(OptionContract(strike, side, symbol, oi, volume, ltp, iv,
                                            **parsed))
            present += 1
        complete += present == 2
    if not contracts:
        raise ValueError("Option chain contains no contracts")
    calls = [row for row in contracts if row.side == "CE"]
    puts = [row for row in contracts if row.side == "PE"]
    call_oi, put_oi = sum(row.open_interest for row in calls), sum(row.open_interest for row in puts)
    call_volume, put_volume = sum(row.volume for row in calls), sum(row.volume for row in puts)
    full = complete == len(strikes)
    atm = min(numeric_strikes, key=lambda strike: (abs(strike - underlying), strike))
    atm_contracts = {row.side: row for row in contracts if row.strike == atm}
    return OptionChainAnalysis(
        expiry_date, received_at.astimezone(timezone.utc), None,
        SOURCE if source_kind == "groww" else "synthetic option-chain example", float(underlying),
        len(strikes), complete, call_oi, put_oi, call_volume, put_volume,
        (put_oi / call_oi if full and call_oi else None),
        (put_volume / call_volume if full and call_volume else None),
        max(calls, key=lambda row: row.open_interest).strike if calls else None,
        max(puts, key=lambda row: row.open_interest).strike if puts else None,
        atm, atm_contracts.get("CE").iv_percent if "CE" in atm_contracts else None,
        atm_contracts.get("PE").iv_percent if "PE" in atm_contracts else None,
        ("synthetic_demo_complete" if full else "synthetic_demo_partial") if source_kind == "synthetic"
        else ("receipt_time_only_complete" if full else "receipt_time_only_partial"),
        tuple(sorted(contracts, key=lambda row: (row.strike, row.side))),
    )


def summarize_known_option_positions(analysis, signed_contracts, lot_size):
    """Sensitivity of an explicitly supplied position, never dealer exposure.

    Positive contract counts are long and negative counts are short. Open
    interest alone cannot reveal these signs or identify a dealer's position.
    Returned sensitivities are per one underlying index point, one day of
    theta, or one percentage point of IV according to provider conventions.
    """
    if not isinstance(analysis, OptionChainAnalysis) or not isinstance(signed_contracts, dict):
        raise ValueError("A parsed chain and explicit signed positions are required")
    if isinstance(lot_size, bool) or not isinstance(lot_size, int) or lot_size <= 0:
        raise ValueError("A verified positive lot size is required")
    indexed = {row.trading_symbol: row for row in analysis.contracts}
    totals = {name: 0.0 for name in ("delta", "gamma", "theta", "vega", "rho")}
    if not signed_contracts:
        raise ValueError("At least one explicit position is required")
    for symbol, count in signed_contracts.items():
        if symbol not in indexed or isinstance(count, bool) or not isinstance(count, int) or count == 0:
            raise ValueError("Unknown or invalid signed option position")
        contract = indexed[symbol]
        if any(getattr(contract, name) is None for name in totals):
            raise ValueError("Position contract is missing a required Greek")
        for name in totals:
            totals[name] += count * lot_size * getattr(contract, name)
    if any(not isfinite(value) for value in totals.values()):
        raise ValueError("Position Greek aggregation overflowed")
    return {"source": analysis.source, "received_at_utc": analysis.received_at_utc.isoformat(),
            "exchange_at": None, "lot_size": lot_size, "signed_contracts": dict(signed_contracts),
            "sensitivities": totals,
            "interpretation": "explicit_position_sensitivity_not_market_dealer_gamma_or_price_forecast"}


def describe_option_structure(analysis):
    """Receipt-time chain descriptors; max pain is hypothetical OI payoff."""
    if not isinstance(analysis, OptionChainAnalysis):
        raise ValueError("A parsed option chain is required")
    calls = [row for row in analysis.contracts if row.side == "CE"]
    puts = [row for row in analysis.contracts if row.side == "PE"]
    complete = analysis.complete_pairs == analysis.strikes
    strikes = sorted({row.strike for row in analysis.contracts})
    max_pain = None
    if complete:
        max_pain = min(strikes, key=lambda settlement: (
            sum(row.open_interest * (max(settlement - row.strike, 0) if row.side == "CE"
                                     else max(row.strike - settlement, 0))
                for row in analysis.contracts), settlement))
    return {
        "source": analysis.source, "received_at_utc": analysis.received_at_utc.isoformat(),
        "exchange_at": None, "complete_chain": complete,
        "call_oi_concentration": (max(row.open_interest for row in calls) / analysis.call_oi
                                  if calls and analysis.call_oi else None),
        "put_oi_concentration": (max(row.open_interest for row in puts) / analysis.put_oi
                                 if puts and analysis.put_oi else None),
        "atm_put_minus_call_iv_points": (
            analysis.atm_put_iv_percent - analysis.atm_call_iv_percent
            if analysis.atm_put_iv_percent is not None and
               analysis.atm_call_iv_percent is not None else None),
        "max_pain_strike": max_pain,
        "interpretation": "OI_payoff_descriptor_not_a_target_or_signed_dealer_exposure",
    }


def compare_option_chains(previous, current):
    """Contract-level OI changes only for identical, ordered chain contexts."""
    if (not isinstance(previous, OptionChainAnalysis) or
            not isinstance(current, OptionChainAnalysis) or
            previous.expiry_date != current.expiry_date or
            previous.source != current.source or
            previous.received_at_utc >= current.received_at_utc):
        raise ValueError("Option chains must share expiry/source and increase in receipt time")
    old = {row.trading_symbol: row for row in previous.contracts}
    new = {row.trading_symbol: row for row in current.contracts}
    return {"source": current.source,
            "previous_received_at_utc": previous.received_at_utc.isoformat(),
            "current_received_at_utc": current.received_at_utc.isoformat(),
            "common_contracts": len(old.keys() & new.keys()),
            "oi_change_by_symbol": {symbol: new[symbol].open_interest - old[symbol].open_interest
                                    for symbol in sorted(old.keys() & new.keys())},
            "interpretation": "receipt_time_OI_change_not_trade_direction_or_exchange_time"}


def describe_iv_term_structure(chains, *, max_receipt_gap_seconds=5):
    """ATM IV by expiry only for near-simultaneous, same-source chains.

    Receipt alignment is a local check. Groww's chain has no exchange snapshot
    timestamp, so even a passing comparison has unverified market freshness.
    """
    supplied = tuple(chains)
    if len(supplied) < 2 or any(not isinstance(row, OptionChainAnalysis) for row in supplied):
        raise ValueError("At least two parsed option expiries are required")
    rows = sorted(supplied, key=lambda row: row.expiry_date)
    if len({row.expiry_date for row in rows}) != len(rows):
        raise ValueError("Duplicate option expiry")
    if len({row.source for row in rows}) != 1 or any(row.exchange_at is not None for row in rows):
        raise ValueError("Option term structure requires one receipt-time-only source")
    if (isinstance(max_receipt_gap_seconds, bool) or
            not isinstance(max_receipt_gap_seconds, (int, float)) or
            not 0 < max_receipt_gap_seconds <= 30):
        raise ValueError("Invalid option receipt alignment limit")
    receipt_gap = (max(row.received_at_utc for row in rows) -
                   min(row.received_at_utc for row in rows)).total_seconds()
    if receipt_gap > max_receipt_gap_seconds:
        raise ValueError("Option expiries were not received close enough together")
    if any(abs(row.underlying_ltp / rows[0].underlying_ltp - 1) > 0.0025 for row in rows):
        raise ValueError("Underlying prices moved too far between option receipts")
    values = {row.expiry_date.isoformat(): {
        "atm_strike": row.atm_strike, "call_iv_percent": row.atm_call_iv_percent,
        "put_iv_percent": row.atm_put_iv_percent} for row in rows}
    near, far = rows[0], rows[-1]
    return {"source": rows[0].source, "receipt_span_seconds": receipt_gap,
            "expiry_iv": values,
            "far_minus_near_call_iv_points": (
                far.atm_call_iv_percent - near.atm_call_iv_percent
                if far.atm_call_iv_percent is not None and
                   near.atm_call_iv_percent is not None else None),
            "far_minus_near_put_iv_points": (
                far.atm_put_iv_percent - near.atm_put_iv_percent
                if far.atm_put_iv_percent is not None and
                   near.atm_put_iv_percent is not None else None),
            "interpretation": "receipt_aligned_IV_only_exchange_freshness_unverified"}

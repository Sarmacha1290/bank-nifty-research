"""Resolve dated NSE bank symbols to Groww NSE cash-equity instruments.

The catalog proves a feed token mapping, not current index membership or weight.
"""
import csv
import io

from .groww import DataAccessError


def resolve_bank_equities(csv_text, symbols):
    wanted = tuple(symbols)
    if not wanted or len(wanted) != len(set(wanted)) or len(wanted) > 14:
        raise ValueError("Expected 1-14 unique NSE bank symbols")
    if any(not isinstance(symbol, str) or not symbol.isalnum() or symbol != symbol.upper()
           for symbol in wanted):
        raise ValueError("Invalid NSE bank symbol")
    matches = {symbol: [] for symbol in wanted}
    for row in csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"))):
        symbol = row.get("trading_symbol")
        if symbol not in matches:
            continue
        if (row.get("exchange"), row.get("segment"), row.get("instrument_type"),
                row.get("series")) != ("NSE", "CASH", "EQ", "EQ"):
            continue
        if (not row.get("exchange_token") or
                row.get("groww_symbol") != "NSE-" + symbol):
            continue
        matches[symbol].append({key: row[key] for key in
                                ("exchange", "segment", "instrument_type", "series",
                                 "trading_symbol", "groww_symbol", "exchange_token")})
    if any(len(rows) != 1 for rows in matches.values()):
        raise DataAccessError("Cannot uniquely map every dated NIFTY Bank symbol to a current Groww NSE equity.")
    tokens = [matches[symbol][0]["exchange_token"] for symbol in wanted]
    if len(tokens) != len(set(tokens)):
        raise DataAccessError("Groww catalogue repeated an NSE equity exchange token.")
    return tuple(matches[symbol][0] for symbol in wanted)

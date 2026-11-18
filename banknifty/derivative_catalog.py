"""Select the nearest listed BANKNIFTY option expiry from Groww's catalogue."""
import csv
from datetime import date
import io

from .groww import DataAccessError


def nearest_banknifty_option_expiry(csv_text, on_date):
    if not isinstance(on_date, date):
        raise ValueError("on_date must be a date")
    expiries = {}
    for row in csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"))):
        if (row.get("exchange"), row.get("segment"), row.get("underlying_symbol")) != (
                "NSE", "FNO", "BANKNIFTY"):
            continue
        side = row.get("instrument_type")
        if side not in ("CE", "PE"):
            continue
        try:
            expiry = date.fromisoformat(row["expiry_date"])
        except (KeyError, TypeError, ValueError):
            continue
        if expiry >= on_date:
            expiries.setdefault(expiry, set()).add(side)
    complete = sorted(expiry for expiry, sides in expiries.items() if sides == {"CE", "PE"})
    if not complete:
        raise DataAccessError("No complete current Bank Nifty option expiry found in Groww's catalogue.")
    return complete[0]

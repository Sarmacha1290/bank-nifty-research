"""Offline staging for delayed, market-wide institutional cash activity.

The CSV format here is an internal normalized format, not a claim about NSE's
download layout. Importing a file does not authenticate its origin or release
time. Staged observations cannot enter the live Master decision path.
"""

import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO
from pathlib import Path
import re
import sqlite3
from contextlib import closing
from .groww import IST


HEADERS = ("date", "category", "buy_crore", "sell_crore", "net_crore")
NSE_HEADERS = ("CATEGORY", "DATE", "BUY VALUE (₹ Crores)",
               "SELL VALUE (₹ Crores)", "NET VALUE (₹ Crores)")
MONTHS = {name: number for number, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
     "Oct", "Nov", "Dec"), 1)}
SCOPES = frozenset({"nse_cash", "nse_bse_msei_cash"})
CATEGORIES = frozenset({"FII/FPI", "DII"})
MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class FlowRow:
    day: date
    category: str
    buy_crore: Decimal
    sell_crore: Decimal
    net_crore: Decimal


def _amount(value, field):
    try:
        result = Decimal(value.strip().replace(",", ""))
    except (AttributeError, InvalidOperation) as error:
        raise ValueError(f"Invalid {field}") from error
    if not result.is_finite() or result.as_tuple().exponent < -2:
        raise ValueError(f"Invalid {field}: expected finite crores with at most two decimals")
    return result


def _check_context(raw, scope, observed_at):
    if scope not in SCOPES:
        raise ValueError("Choose an explicit cash-market scope")
    if (not isinstance(observed_at, datetime) or observed_at.tzinfo is None or
            observed_at.utcoffset() is None):
        raise ValueError("observed_at must be timezone-aware")
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES or not raw:
        raise ValueError("Expected a nonempty CSV below the size limit")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeError as error:
        raise ValueError("Expected UTF-8 CSV") from error


def _read_rows(body, *, headers, header_label):
    reader = csv.DictReader(StringIO(body, newline=""), strict=True)
    actual = reader.fieldnames
    if actual is None:
        raise ValueError("Missing CSV headers")
    if header_label == "NSE":
        actual = [" ".join(value.split()) for value in actual]
        reader.fieldnames = list(actual)
    if actual != list(headers):
        raise ValueError(f"CSV must use the exact {header_label} flow headers")
    return reader


def _nse_date(value):
    match = re.fullmatch(r"(\d{2})-([A-Za-z]{3})-(\d{4})", value.strip())
    if not match or match.group(2) not in MONTHS:
        raise ValueError("Invalid NSE report date")
    try:
        return date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1)))
    except ValueError as error:
        raise ValueError("Invalid NSE report date") from error


def _parse_records(reader, *, fieldnames, date_parser, observed_at):
    date_field, category_field, buy_field, sell_field, net_field = fieldnames
    rows = []
    seen = set()
    try:
        for record in reader:
            if None in record or any(value is None for value in record.values()):
                raise ValueError("Malformed CSV row")
            try:
                day = date_parser(record[date_field])
            except ValueError as error:
                raise ValueError("Invalid report date") from error
            category = record[category_field].strip()
            if category not in CATEGORIES or category in seen:
                raise ValueError("Expected one FII/FPI and one DII row")
            seen.add(category)
            buy = _amount(record[buy_field], "buy")
            sell = _amount(record[sell_field], "sell")
            net = _amount(record[net_field], "net")
            if buy < 0 or sell < 0 or abs(buy - sell - net) > Decimal("0.02"):
                raise ValueError("Invalid flow totals: net must equal buy minus sell")
            rows.append(FlowRow(day, category, buy, sell, net))
    except csv.Error as error:
        raise ValueError("Malformed CSV") from error
    if len(rows) != 2 or len({row.day for row in rows}) != 1:
        raise ValueError("Expected exactly two categories for one report date")
    if rows[0].day > observed_at.astimezone(IST).date():
        raise ValueError("Report date is after local observation date")
    return tuple(rows)


def parse_normalized_csv(raw, *, scope, observed_at):
    """Validate the app's internal one-day normalized CSV format."""
    body = _check_context(raw, scope, observed_at)
    reader = _read_rows(body, headers=HEADERS, header_label="normalized")
    return _parse_records(reader, fieldnames=HEADERS,
                          date_parser=lambda value: date.fromisoformat(value.strip()),
                          observed_at=observed_at)


def parse_nse_download_csv(raw, *, scope, observed_at):
    """Validate the downloaded NSE FII/DII CSV layout observed in supplied files.

    Scope is external metadata: the CSV itself does not say NSE-only versus
    combined. The caller must identify the clicked report, not guess by value.
    """
    body = _check_context(raw, scope, observed_at)
    reader = _read_rows(body, headers=NSE_HEADERS, header_label="NSE")
    return _parse_records(reader, fieldnames=(NSE_HEADERS[1], NSE_HEADERS[0],
                                              *NSE_HEADERS[2:]),
                          date_parser=_nse_date, observed_at=observed_at)


def stage_file(database, csv_file, *, scope, observed_at, file_format="normalized"):
    """Save a manual, unverified import without treating it as a live signal."""
    path = Path(csv_file)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("Choose an existing small CSV file")
    raw = path.read_bytes()
    if file_format == "normalized":
        rows = parse_normalized_csv(raw, scope=scope, observed_at=observed_at)
    elif file_format == "nse_download":
        rows = parse_nse_download_csv(raw, scope=scope, observed_at=observed_at)
    else:
        raise ValueError("Choose normalized or nse_download file format")
    digest = sha256(raw).hexdigest()
    observed_utc = observed_at.astimezone(timezone.utc).isoformat()
    destination = Path(database)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(destination)) as connection:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS staged_institutional_flows (
                scope TEXT NOT NULL, report_date TEXT NOT NULL,
                category TEXT NOT NULL, buy_crore TEXT NOT NULL,
                sell_crore TEXT NOT NULL, net_crore TEXT NOT NULL,
                first_observed_utc TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
                authenticity TEXT NOT NULL CHECK (authenticity='unverified_manual'),
                PRIMARY KEY (scope, report_date, category)
            )""")
            for row in rows:
                key = (scope, row.day.isoformat(), row.category)
                existing = connection.execute(
                    "SELECT buy_crore,sell_crore,net_crore,first_observed_utc,raw_sha256 "
                    "FROM staged_institutional_flows WHERE scope=? AND report_date=? AND category=?",
                    key).fetchone()
                values = (str(row.buy_crore), str(row.sell_crore), str(row.net_crore),
                          observed_utc, digest)
                if existing is not None and (existing[:3] != values[:3] or
                                             existing[4] != digest):
                    raise ValueError("Existing report differs; corrections need source review")
                if existing is None:
                    connection.execute(
                        "INSERT INTO staged_institutional_flows VALUES (?,?,?,?,?,?,?,?,?)",
                        (*key, *values, "unverified_manual"))
    return {"scope": scope, "report_date": rows[0].day.isoformat(),
            "rows": len(rows), "raw_sha256": digest,
            "first_observed_utc": observed_utc, "authenticity": "unverified_manual",
            "file_format": file_format}


def read_staged(database, *, scope, report_date, decision_at):
    """Return delayed context only if this import was observed by the cutoff."""
    if scope not in SCOPES or not isinstance(report_date, date):
        raise ValueError("Invalid scope or report date")
    if (not isinstance(decision_at, datetime) or decision_at.tzinfo is None or
            decision_at.utcoffset() is None):
        raise ValueError("decision_at must be timezone-aware")
    path = Path(database)
    if not path.is_file():
        return ()
    with closing(sqlite3.connect(path)) as connection:
        try:
            records = connection.execute(
                "SELECT category,buy_crore,sell_crore,net_crore,first_observed_utc,raw_sha256 "
                "FROM staged_institutional_flows WHERE scope=? AND report_date=? "
                "AND first_observed_utc<=? ORDER BY category",
                (scope, report_date.isoformat(), decision_at.astimezone(timezone.utc).isoformat())
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error):
                return ()
            raise
    if len(records) != 2:
        return ()
    return tuple({"category": category, "buy_crore": Decimal(buy),
                  "sell_crore": Decimal(sell), "net_crore": Decimal(net),
                  "first_observed_utc": observed, "raw_sha256": digest,
                  "scope": scope, "authenticity": "unverified_manual",
                  "forecast_eligible": False} for category, buy, sell, net, observed, digest in records)


def latest_staged(database, *, decision_at):
    """Read the latest visible, complete day for each scope; no forecast use."""
    if (not isinstance(decision_at, datetime) or decision_at.tzinfo is None or
            decision_at.utcoffset() is None):
        raise ValueError("decision_at must be timezone-aware")
    path = Path(database)
    if not path.is_file():
        return ()
    with closing(sqlite3.connect(path)) as connection:
        try:
            keys = connection.execute(
                "SELECT scope, MAX(report_date) FROM staged_institutional_flows "
                "WHERE first_observed_utc<=? GROUP BY scope ORDER BY scope",
                (decision_at.astimezone(timezone.utc).isoformat(),)
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error):
                return ()
            raise
    result = []
    for scope, day in keys:
        rows = read_staged(path, scope=scope, report_date=date.fromisoformat(day),
                           decision_at=decision_at)
        if rows:
            result.append({"scope": scope, "report_date": day, "rows": rows})
    return tuple(result)

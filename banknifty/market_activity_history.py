"""Append-only, receipt-time NSE bank quote archive and Excel export."""
from contextlib import closing
import csv
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
import re
import sqlite3

from .market_activity import BankQuote


def save_bank_quotes(database, sweep_id, quotes):
    rows = tuple(quotes)
    if (not isinstance(sweep_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", sweep_id)
            or not rows or any(not isinstance(row, BankQuote) for row in rows)):
        raise ValueError("A sweep needs a safe id and bank quotes")
    if any(not isinstance(row, BankQuote) or row.source != "Groww NSE cash quote"
           or not isinstance(row.symbol, str) or not re.fullmatch(r"NSE-[A-Z0-9]+", row.symbol)
           or not isinstance(row.received_at, datetime) or row.received_at.tzinfo is None
           or row.received_at.utcoffset() is None
           or (row.volume is not None and
               (isinstance(row.volume, bool) or not isinstance(row.volume, int) or row.volume < 0))
           or any(value is not None and (isinstance(value, bool) or
                   not isinstance(value, (int, float)) or not isfinite(value) or value < 0)
                  for value in (row.total_buy_quantity, row.total_sell_quantity))
           or (row.last_price is not None and (isinstance(row.last_price, bool) or
               not isinstance(row.last_price, (int, float)) or not isfinite(row.last_price)
               or row.last_price <= 0)) for row in rows):
        raise ValueError("Only validated Groww bank quotes may be saved")
    if len({row.symbol for row in rows}) != len(rows):
        raise ValueError("A sweep needs distinct bank quotes")
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS bank_quotes (
                sweep_id TEXT NOT NULL, symbol TEXT NOT NULL, received_at_utc TEXT NOT NULL,
                cumulative_volume INTEGER, total_buy_quantity REAL, total_sell_quantity REAL,
                last_price REAL, source TEXT NOT NULL,
                PRIMARY KEY(sweep_id, symbol))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS bank_quote_sweeps (
                sweep_id TEXT PRIMARY KEY, completed_at_utc TEXT NOT NULL)""")
            connection.execute("CREATE INDEX IF NOT EXISTS bank_quotes_received "
                               "ON bank_quotes(received_at_utc)")
            connection.execute("CREATE INDEX IF NOT EXISTS bank_quote_sweeps_latest "
                               "ON bank_quote_sweeps(completed_at_utc DESC)")
            if connection.execute("SELECT 1 FROM bank_quote_sweeps WHERE sweep_id=?",
                                  (sweep_id,)).fetchone():
                raise ValueError("A quote sweep already exists")
            connection.execute("INSERT INTO bank_quote_sweeps VALUES (?,?)", (
                sweep_id, max(row.received_at for row in rows).astimezone(timezone.utc).isoformat(timespec="milliseconds")))
            for row in rows:
                if connection.execute("SELECT 1 FROM bank_quotes WHERE sweep_id=? AND symbol=?",
                                      (sweep_id, row.symbol)).fetchone():
                    raise ValueError("A bank quote already exists for this sweep")
                connection.execute("INSERT INTO bank_quotes VALUES (?,?,?,?,?,?,?,?)", (
                    sweep_id, row.symbol, row.received_at.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
                    row.volume, row.total_buy_quantity, row.total_sell_quantity, row.last_price, row.source))
    return len(rows)


def load_bank_quotes(database, *, limit=10000):
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("Positive row limit required")
    path = Path(database)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_quotes'").fetchone() is None:
            return []
        rows = connection.execute("SELECT symbol, received_at_utc, cumulative_volume, total_buy_quantity, "
                                  "total_sell_quantity, last_price, source FROM bank_quotes "
                                  "ORDER BY received_at_utc DESC LIMIT ?", (limit,)).fetchall()
    return [BankQuote(row[0], datetime.fromisoformat(row[1]), *row[2:]) for row in reversed(rows)]


def load_bank_quote_sweeps(database, *, limit=20):
    """Load recent collection sweeps as separately labelled quote groups."""
    if not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("Sweep limit must be between 1 and 1000")
    path = Path(database)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_quotes'").fetchone() is None:
            return []
        ids = [row[0] for row in connection.execute(
            "SELECT sweep_id FROM bank_quote_sweeps "
            "ORDER BY completed_at_utc DESC LIMIT ?", (limit,))]
        result = []
        for sweep_id in reversed(ids):
            rows = connection.execute("SELECT symbol, received_at_utc, cumulative_volume, total_buy_quantity, "
                                      "total_sell_quantity, last_price, source FROM bank_quotes "
                                      "WHERE sweep_id=? ORDER BY symbol", (sweep_id,)).fetchall()
            result.append([BankQuote(row[0], datetime.fromisoformat(row[1]), *row[2:]) for row in rows])
    return result


def count_bank_quotes(database):
    path = Path(database)
    if not path.exists():
        return 0
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_quotes'").fetchone() is None:
            return 0
        return connection.execute("SELECT COUNT(*) FROM bank_quotes").fetchone()[0]


def save_activity_alerts(database, alerts):
    """Save exploratory alerts once; never call them trade signals."""
    rows = tuple(alerts)
    if not rows:
        return 0
    meanings = {
        "traded_volume_surge": "Traded shares increased; trade direction unknown",
        "outstanding_buy_orders_surge": "Outstanding buy quantity rose; not executed buy trades",
        "outstanding_sell_orders_surge": "Outstanding sell quantity rose; not executed sell trades",
        "top5_buy_depth_surge": "Visible top-five buy orders rose; executed buy trades unknown",
        "top5_sell_depth_surge": "Visible top-five sell orders rose; executed sell trades unknown",
    }
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in meanings:
            raise ValueError("Invalid activity alert")
        stamp = row.get("at")
        try:
            at = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
        except ValueError:
            at = None
        values = (row.get("current"), row.get("baseline"), row.get("multiple"))
        if (not isinstance(row.get("symbol"), str) or
                not re.fullmatch(r"NSE-(?:[A-Z0-9]+|ALL14-UNWEIGHTED)", row["symbol"]) or
                at is None or at.tzinfo is None or at.utcoffset() is None or
                row.get("meaning") != meanings[row["kind"]] or
                any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not isfinite(value) for value in values) or
                values[0] < 0 or values[1] <= 0 or values[2] <= 1 or
                abs(values[2] - values[0] / values[1]) > 1e-6 * values[2]):
            raise ValueError("Invalid activity alert")
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS bank_activity_alerts (
                symbol TEXT NOT NULL, kind TEXT NOT NULL, at_utc TEXT NOT NULL,
                current_value REAL NOT NULL, baseline_value REAL NOT NULL,
                multiple REAL NOT NULL, meaning TEXT NOT NULL,
                PRIMARY KEY(symbol, kind, at_utc))""")
            inserted = 0
            for row in rows:
                at_utc = datetime.fromisoformat(row["at"]).astimezone(timezone.utc).isoformat()
                identity = (row["symbol"], row["kind"], at_utc)
                metrics = (row["current"], row["baseline"], row["multiple"], row["meaning"])
                previous = connection.execute(
                    "SELECT current_value, baseline_value, multiple, meaning "
                    "FROM bank_activity_alerts WHERE symbol=? AND kind=? AND at_utc=?",
                    identity).fetchone()
                if previous is not None:
                    if previous != metrics:
                        raise ValueError("Conflicting activity alert at an existing timestamp")
                    continue
                connection.execute("INSERT INTO bank_activity_alerts VALUES (?,?,?,?,?,?,?)",
                                   (*identity, *metrics))
                inserted += 1
    return inserted


def load_activity_alerts(database, *, limit=100):
    path = Path(database)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_activity_alerts'").fetchone() is None:
            return []
        rows = connection.execute("SELECT symbol, kind, at_utc, current_value, baseline_value, multiple, meaning "
                                  "FROM bank_activity_alerts ORDER BY at_utc DESC LIMIT ?", (limit,)).fetchall()
    return [dict(zip(("symbol", "kind", "at", "current", "baseline", "multiple", "meaning"), row))
            for row in rows]


def export_activity_alerts_csv(database, destination):
    source, target = Path(database), Path(destination)
    if source.resolve() == target.resolve():
        raise ValueError("Export must not overwrite the archive")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("symbol", "kind", "at", "current", "baseline", "multiple", "meaning"))
        count = 0
        if source.exists():
            with closing(sqlite3.connect(source)) as connection:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_activity_alerts'").fetchone():
                    cursor = connection.execute("SELECT symbol, kind, at_utc, current_value, "
                        "baseline_value, multiple, meaning FROM bank_activity_alerts ORDER BY at_utc")
                    for row in cursor:
                        writer.writerow(row)
                        count += 1
    temporary.replace(target)
    return count


def export_bank_quotes_csv(database, destination):
    source, target = Path(database), Path(destination)
    if source.resolve() == target.resolve() or (source.exists() and target.exists() and source.samefile(target)):
        raise ValueError("Export must not overwrite the archive")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("symbol", "received_at_utc", "cumulative_traded_shares",
                         "outstanding_buy_shares", "outstanding_sell_shares", "last_price",
                         "source", "exchange_quote_timestamp"))
        count = 0
        if source.exists():
            with closing(sqlite3.connect(source)) as connection:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_quotes'").fetchone():
                    cursor = connection.execute("SELECT symbol, received_at_utc, cumulative_volume, "
                        "total_buy_quantity, total_sell_quantity, last_price, source FROM bank_quotes "
                        "ORDER BY received_at_utc, symbol")
                    for row in cursor:
                        writer.writerow((*row, ""))
                        count += 1
    temporary.replace(target)
    return count

"""Durable raw Groww bank-equity updates, separate from index ticks and candles."""
from contextlib import closing
import csv
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
import re
import sqlite3

from .equity_feed import EquityTick


def save_equity_ticks(database, ticks):
    """Atomically save unique validated updates; return inserted/duplicate counts."""
    ticks = tuple(ticks)
    if any(not isinstance(tick, EquityTick) or tick.source != "Groww equity LTP feed"
           or not re.fullmatch(r"NSE-[A-Z0-9]+", tick.symbol)
           or tick.exchange_at.tzinfo is None or tick.exchange_at.utcoffset() is None
           or tick.received_at.tzinfo is None or tick.received_at.utcoffset() is None
           or not isinstance(tick.ltp, (int, float)) or isinstance(tick.ltp, bool)
           or not isfinite(tick.ltp) or tick.ltp <= 0 for tick in ticks):
        raise ValueError("Only validated Groww NSE equity ticks may be saved")
    if not ticks:
        return {"inserted": 0, "duplicates": 0, "inserted_symbols": ()}
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    result = {"inserted": 0, "duplicates": 0}
    inserted_symbols = set()
    with closing(sqlite3.connect(path, timeout=1.0)) as connection:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS equity_ticks (
                symbol TEXT NOT NULL, exchange_at_utc TEXT NOT NULL,
                received_at_utc TEXT NOT NULL, ltp REAL NOT NULL,
                source TEXT NOT NULL,
                PRIMARY KEY (symbol, exchange_at_utc))""")
            for tick in ticks:
                exchange_at = tick.exchange_at.astimezone(timezone.utc).isoformat(timespec="milliseconds")
                received_at = tick.received_at.astimezone(timezone.utc).isoformat(timespec="milliseconds")
                existing = connection.execute(
                    "SELECT ltp FROM equity_ticks WHERE symbol=? AND exchange_at_utc=?",
                    (tick.symbol, exchange_at)).fetchone()
                if existing is not None:
                    if existing[0] != tick.ltp:
                        raise ValueError("Conflicting equity price at an existing exchange timestamp")
                    result["duplicates"] += 1
                else:
                    connection.execute("INSERT INTO equity_ticks VALUES (?,?,?,?,?)",
                                       (tick.symbol, exchange_at, received_at, tick.ltp, tick.source))
                    result["inserted"] += 1
                    inserted_symbols.add(tick.symbol)
    return {**result, "inserted_symbols": tuple(sorted(inserted_symbols))}


def count_equity_ticks(database):
    path = Path(database)
    if not path.exists():
        return 0
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='equity_ticks' AND type='table'").fetchone() is None:
            return 0
        return connection.execute("SELECT COUNT(*) FROM equity_ticks").fetchone()[0]


def equity_symbol_counts(database):
    path = Path(database)
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='equity_ticks' AND type='table'").fetchone() is None:
            return {}
        rows = connection.execute("SELECT symbol, COUNT(*) FROM equity_ticks GROUP BY symbol ORDER BY symbol").fetchall()
    return dict(rows)


def equity_coverage(database, expected_symbols):
    """Raw per-symbol counts and time span; zero count is explicit."""
    symbols = tuple(expected_symbols)
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError("Expected unique bank symbols")
    path = Path(database)
    found = {}
    if path.exists():
        with closing(sqlite3.connect(path)) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='equity_ticks' AND type='table'").fetchone():
                found = {row[0]: {"count": row[1], "first_at_utc": row[2], "last_at_utc": row[3]}
                         for row in connection.execute(
                             "SELECT symbol, COUNT(*), MIN(exchange_at_utc), MAX(exchange_at_utc) "
                             "FROM equity_ticks GROUP BY symbol")}
    return {symbol: found.get(symbol, {"count": 0, "first_at_utc": None, "last_at_utc": None})
            for symbol in symbols}


def load_equity_ticks(database, *, start=None, end=None, symbols=None):
    """Read raw observations in exchange-time order; never fill missing prices."""
    path = Path(database)
    if not path.exists():
        return []
    for bound in (start, end):
        if bound is not None and (not isinstance(bound, datetime) or
                                  bound.tzinfo is None or bound.utcoffset() is None):
            raise ValueError("Equity query bounds must be timezone-aware")
    wanted = tuple(symbols) if symbols is not None else None
    if wanted is not None and (not wanted or len(wanted) != len(set(wanted))):
        raise ValueError("Equity query symbols must be unique")
    clauses, parameters = [], []
    if start is not None:
        clauses.append("exchange_at_utc>=?")
        parameters.append(start.astimezone(timezone.utc).isoformat(timespec="milliseconds"))
    if end is not None:
        clauses.append("exchange_at_utc<?")
        parameters.append(end.astimezone(timezone.utc).isoformat(timespec="milliseconds"))
    if wanted is not None:
        clauses.append("symbol IN (" + ",".join("?" for _ in wanted) + ")")
        parameters.extend(wanted)
    query = "SELECT symbol, exchange_at_utc, received_at_utc, ltp, source FROM equity_ticks"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY exchange_at_utc, symbol"
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='equity_ticks' AND type='table'").fetchone() is None:
            return []
        rows = connection.execute(query, parameters).fetchall()
    return [EquityTick(row[0], datetime.fromisoformat(row[1]), datetime.fromisoformat(row[2]),
                       row[3], row[4]) for row in rows]


def export_equity_ticks_csv(database, destination):
    """Export raw bank LTP observations without treating them as aligned bars."""
    source, target = Path(database), Path(destination)
    if target.resolve() == source.resolve() or (
        target.exists() and source.exists() and target.samefile(source)
    ):
        raise ValueError("Export destination must not overwrite the archive database")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("symbol", "exchange_at_utc", "received_at_utc", "ltp", "source"))
        if not source.exists():
            return 0
        with closing(sqlite3.connect(source)) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='equity_ticks' AND type='table'").fetchone() is None:
                return 0
            cursor = connection.execute("SELECT symbol, exchange_at_utc, received_at_utc, ltp, source "
                                        "FROM equity_ticks ORDER BY exchange_at_utc, symbol")
            count = 0
            for row in cursor:
                writer.writerow(row)
                count += 1
    return count

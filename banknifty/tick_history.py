"""Durable Groww index updates, separate from verified one-minute candles."""

from contextlib import closing
import csv
from datetime import datetime, timezone
from math import isfinite
from numbers import Real
from pathlib import Path
import sqlite3

from .feed import IndexTick
from .groww import IST


def save_index_tick(database, tick):
    """Save one unique exchange-timestamped update; return inserted/duplicate."""
    if not isinstance(tick, IndexTick) or tick.source != "Groww index feed" or tick.symbol != "NSE-BANKNIFTY":
        raise ValueError("Only validated Groww index ticks may be saved")
    if (not isinstance(tick.exchange_at, datetime) or tick.exchange_at.tzinfo is None or
            tick.exchange_at.utcoffset() is None or
            not isinstance(tick.received_at, datetime) or tick.received_at.tzinfo is None or
            tick.received_at.utcoffset() is None):
        raise ValueError("Index tick timestamps must be timezone-aware")
    if (isinstance(tick.value, bool) or not isinstance(tick.value, Real) or
            not isfinite(tick.value) or tick.value <= 0):
        raise ValueError("Index tick value must be finite and positive")
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    exchange_at = tick.exchange_at.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    received_at = tick.received_at.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    with closing(sqlite3.connect(path, timeout=1.0)) as connection:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS index_ticks (
                symbol TEXT NOT NULL, exchange_at_utc TEXT NOT NULL,
                received_at_utc TEXT NOT NULL, value REAL NOT NULL,
                source TEXT NOT NULL,
                PRIMARY KEY (symbol, exchange_at_utc))""")
            existing = connection.execute(
                "SELECT value FROM index_ticks WHERE symbol=? AND exchange_at_utc=?",
                (tick.symbol, exchange_at)).fetchone()
            if existing is not None:
                if existing[0] != tick.value:
                    raise ValueError("Conflicting value at an existing exchange timestamp")
                return "duplicate"
            connection.execute("INSERT INTO index_ticks VALUES (?,?,?,?,?)",
                               (tick.symbol, exchange_at, received_at, tick.value, tick.source))
    return "inserted"


def count_index_ticks(database, *, symbol="NSE-BANKNIFTY"):
    path = Path(database)
    if not path.exists():
        return 0
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='index_ticks' AND type='table'").fetchone() is None:
            return 0
        return connection.execute("SELECT COUNT(*) FROM index_ticks WHERE symbol=?", (symbol,)).fetchone()[0]


def latest_index_session_date(database, *, symbol="NSE-BANKNIFTY"):
    path = Path(database)
    if not path.exists():
        return None
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='index_ticks' AND type='table'").fetchone() is None:
            return None
        row = connection.execute("SELECT MAX(exchange_at_utc) FROM index_ticks WHERE symbol=?", (symbol,)).fetchone()
    return datetime.fromisoformat(row[0]).astimezone(IST).date() if row and row[0] else None


def load_index_ticks(database, *, symbol="NSE-BANKNIFTY", start=None, end=None):
    """Read recorded updates in exchange-time order; never infer missing data."""
    path = Path(database)
    if not path.exists():
        return []
    for bound in (start, end):
        if bound is not None and (bound.tzinfo is None or bound.utcoffset() is None):
            raise ValueError("Tick query bounds must be timezone-aware")
    clauses, parameters = ["symbol=?"], [symbol]
    if start is not None:
        clauses.append("exchange_at_utc>=?")
        parameters.append(start.astimezone(timezone.utc).isoformat(timespec="milliseconds"))
    if end is not None:
        clauses.append("exchange_at_utc<?")
        parameters.append(end.astimezone(timezone.utc).isoformat(timespec="milliseconds"))
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='index_ticks' AND type='table'").fetchone() is None:
            return []
        rows = connection.execute(
            "SELECT symbol, exchange_at_utc, received_at_utc, value, source FROM index_ticks WHERE "
            + " AND ".join(clauses) + " ORDER BY exchange_at_utc", parameters
        ).fetchall()
    return [IndexTick(row[0], datetime.fromisoformat(row[1]), datetime.fromisoformat(row[2]), row[3], row[4])
            for row in rows]


def export_index_ticks_csv(database, destination, *, symbol="NSE-BANKNIFTY"):
    """Export original exchange and receipt times; never replace the archive."""
    source, target = Path(database), Path(destination)
    if target.resolve() == source.resolve() or (
        target.exists() and source.exists() and target.samefile(source)
    ):
        raise ValueError("Export destination must not overwrite the archive database")
    ticks = load_index_ticks(database, symbol=symbol)
    def excel_safe(value):
        if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@")) or
                                       value.startswith(("\t", "\r", "\n"))):
            return "'" + value
        return value
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("symbol", "exchange_at_utc", "received_at_utc", "value", "source"))
        for tick in ticks:
            writer.writerow((excel_safe(tick.symbol), tick.exchange_at.astimezone(timezone.utc).isoformat(),
                             tick.received_at.astimezone(timezone.utc).isoformat(),
                             tick.value, excel_safe(tick.source)))
    return len(ticks)

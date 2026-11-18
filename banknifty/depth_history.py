"""Durable five-level order-book observations with exchange and receipt times."""
from contextlib import closing
import csv
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import re
import sqlite3

from .depth_feed import DepthTick


def _valid_levels(levels):
    return (isinstance(levels, tuple) and len(levels) <= 5 and all(
        isinstance(level, tuple) and len(level) == 2 and
        all(isinstance(value, (int, float)) and not isinstance(value, bool) and
            isfinite(value) for value in level) and
        level[0] > 0 and level[1] >= 0 for level in levels))


def save_depth_ticks(database, ticks):
    rows = tuple(ticks)
    if not rows:
        return {"inserted": 0, "duplicates": 0, "symbols": ()}
    if any(not isinstance(row, DepthTick) or row.source != "Groww market depth feed"
           or not isinstance(row.symbol, str) or not re.fullmatch(r"NSE-[A-Z0-9]+", row.symbol)
           or not isinstance(row.exchange_at, datetime) or row.exchange_at.tzinfo is None
           or row.exchange_at.utcoffset() is None
           or not isinstance(row.received_at, datetime) or row.received_at.tzinfo is None
           or row.received_at.utcoffset() is None
           or not _valid_levels(row.buy_levels) or not _valid_levels(row.sell_levels)
           or (not row.buy_levels and not row.sell_levels) for row in rows):
        raise ValueError("Only validated depth feed ticks may be archived")
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    inserted, duplicates, symbols = 0, 0, set()
    with closing(sqlite3.connect(path, timeout=1.0)) as connection:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS bank_depth_ticks (
                symbol TEXT NOT NULL, exchange_at_utc TEXT NOT NULL,
                received_at_utc TEXT NOT NULL, buy_levels_json TEXT NOT NULL,
                sell_levels_json TEXT NOT NULL, source TEXT NOT NULL,
                PRIMARY KEY(symbol, exchange_at_utc))""")
            connection.execute("CREATE INDEX IF NOT EXISTS bank_depth_latest "
                               "ON bank_depth_ticks(exchange_at_utc DESC)")
            for row in rows:
                exchange_at = row.exchange_at.astimezone(timezone.utc).isoformat(timespec="milliseconds")
                received_at = row.received_at.astimezone(timezone.utc).isoformat(timespec="milliseconds")
                buy = json.dumps(row.buy_levels, separators=(",", ":"))
                sell = json.dumps(row.sell_levels, separators=(",", ":"))
                previous = connection.execute("SELECT buy_levels_json, sell_levels_json FROM bank_depth_ticks "
                    "WHERE symbol=? AND exchange_at_utc=?", (row.symbol, exchange_at)).fetchone()
                if previous:
                    if previous != (buy, sell):
                        raise ValueError("Conflicting order book at an existing exchange timestamp")
                    duplicates += 1
                    continue
                connection.execute("INSERT INTO bank_depth_ticks VALUES (?,?,?,?,?,?)",
                                   (row.symbol, exchange_at, received_at, buy, sell, row.source))
                inserted += 1
                symbols.add(row.symbol)
    return {"inserted": inserted, "duplicates": duplicates, "symbols": tuple(sorted(symbols))}


def count_depth_ticks(database):
    path = Path(database)
    if not path.exists():
        return 0
    with closing(sqlite3.connect(path)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_depth_ticks'").fetchone():
            return 0
        return connection.execute("SELECT COUNT(*) FROM bank_depth_ticks").fetchone()[0]


def latest_depth_by_symbol(database):
    """One most recent saved exchange-time book for each bank."""
    path = Path(database)
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_depth_ticks'").fetchone():
            return {}
        rows = connection.execute("SELECT d.symbol, d.exchange_at_utc, d.received_at_utc, "
            "d.buy_levels_json, d.sell_levels_json, d.source FROM bank_depth_ticks d JOIN "
            "(SELECT symbol, MAX(exchange_at_utc) AS last_at FROM bank_depth_ticks GROUP BY symbol) last "
            "ON d.symbol=last.symbol AND d.exchange_at_utc=last.last_at ORDER BY d.symbol").fetchall()
    return {row[0]: DepthTick(row[0], datetime.fromisoformat(row[1]), datetime.fromisoformat(row[2]),
             tuple(tuple(item) for item in json.loads(row[3])),
             tuple(tuple(item) for item in json.loads(row[4])), row[5]) for row in rows}


def load_recent_depth_ticks(database, *, limit=500):
    if not isinstance(limit, int) or not 1 <= limit <= 100000:
        raise ValueError("Depth history limit must be positive")
    path = Path(database)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_depth_ticks'").fetchone():
            return []
        rows = connection.execute("SELECT symbol, exchange_at_utc, received_at_utc, "
            "buy_levels_json, sell_levels_json, source FROM bank_depth_ticks "
            "ORDER BY exchange_at_utc DESC LIMIT ?", (limit,)).fetchall()
    return [DepthTick(row[0], datetime.fromisoformat(row[1]), datetime.fromisoformat(row[2]),
            tuple(tuple(item) for item in json.loads(row[3])),
            tuple(tuple(item) for item in json.loads(row[4])), row[5]) for row in reversed(rows)]


def export_depth_ticks_csv(database, destination):
    source, target = Path(database), Path(destination)
    if source.resolve() == target.resolve() or (source.exists() and target.exists() and source.samefile(target)):
        raise ValueError("Depth export must not overwrite the archive")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    count = 0
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("symbol", "exchange_at_utc", "received_at_utc", "top5_buy_quantity",
                         "top5_sell_quantity", "best_bid", "best_ask", "buy_levels_json",
                         "sell_levels_json", "source", "note"))
        if source.exists():
            with closing(sqlite3.connect(source)) as connection:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_depth_ticks'").fetchone():
                    cursor = connection.execute("SELECT symbol, exchange_at_utc, received_at_utc, "
                        "buy_levels_json, sell_levels_json, source FROM bank_depth_ticks "
                        "ORDER BY exchange_at_utc, symbol")
                    for symbol, exchange_at, received_at, buy_raw, sell_raw, provider in cursor:
                        buy, sell = json.loads(buy_raw), json.loads(sell_raw)
                        writer.writerow((symbol, exchange_at, received_at,
                            sum(level[1] for level in buy) if buy else "",
                            sum(level[1] for level in sell) if sell else "",
                            buy[0][0] if buy else "", sell[0][0] if sell else "",
                            buy_raw, sell_raw, provider,
                            "Visible top five levels; not executed buy/sell volume"))
                        count += 1
    temporary.replace(target)
    return count

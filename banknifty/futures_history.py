"""Sanitized Bank Nifty futures event archive and Excel-readable export."""
from contextlib import closing
import csv
from dataclasses import asdict
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .futures import FutureInstrument, FutureSnapshot, parse_future_quote
from .storage import record_event


def _canonical(snapshot):
    if not isinstance(snapshot, FutureSnapshot):
        raise ValueError("Expected a futures snapshot")
    instrument = FutureInstrument(snapshot.trading_symbol, snapshot.groww_symbol,
                                  snapshot.exchange_token, snapshot.expiry_date)
    trade_ms = (int(snapshot.last_trade_at_utc.timestamp() * 1000)
                if isinstance(snapshot.last_trade_at_utc, datetime) and
                snapshot.last_trade_at_utc.tzinfo is not None and
                snapshot.last_trade_at_utc.utcoffset() is not None else None)
    quote = {"status": "SUCCESS", "payload": {
        "last_price": snapshot.last_price, "last_trade_time": trade_ms,
        "volume": snapshot.volume, "open_interest": snapshot.open_interest,
        "previous_open_interest": snapshot.previous_open_interest,
        "oi_day_change": snapshot.oi_day_change}}
    rebuilt = parse_future_quote(quote, instrument, snapshot.received_at_utc,
                                 source_kind=snapshot.source)
    if rebuilt != snapshot:
        raise ValueError("Futures snapshot provenance or fields are inconsistent")
    result = asdict(snapshot)
    result["exchange_instrument_id"] = result.pop("exchange_token")
    result["expiry_date"] = snapshot.expiry_date.isoformat()
    result["received_at_utc"] = snapshot.received_at_utc.isoformat()
    result["last_trade_at_utc"] = (snapshot.last_trade_at_utc.isoformat()
                                   if snapshot.last_trade_at_utc else None)
    return result


def save_future_snapshot(database, snapshot):
    """Revalidate direct writes, then append. Synthetic rows stay labelled."""
    return record_event(database, "banknifty_future_quote", _canonical(snapshot))


def load_future_snapshots(database):
    path = Path(database)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='events' AND type='table'").fetchone() is None:
            return []
        rows = connection.execute(
            "SELECT payload, sha256 FROM events WHERE event_type='banknifty_future_quote' "
            "ORDER BY recorded_at, id").fetchall()
    snapshots = []
    for payload, digest in rows:
        if sha256(payload.encode()).hexdigest() != digest:
            raise ValueError("Futures record failed its integrity check")
        row = json.loads(payload)
        snapshot = FutureSnapshot(
            source=row["source"], trading_symbol=row["trading_symbol"],
            groww_symbol=row["groww_symbol"], exchange_token=row["exchange_instrument_id"],
            expiry_date=date.fromisoformat(row["expiry_date"]),
            received_at_utc=datetime.fromisoformat(row["received_at_utc"]),
            last_trade_at_utc=(datetime.fromisoformat(row["last_trade_at_utc"])
                                if row["last_trade_at_utc"] else None),
            last_price=row["last_price"], volume=row["volume"],
            open_interest=row["open_interest"],
            previous_open_interest=row["previous_open_interest"],
            oi_day_change=row["oi_day_change"], freshness=row["freshness"])
        if _canonical(snapshot) != row:
            raise ValueError("Futures archive record failed source validation")
        snapshots.append(row)
    return snapshots


def export_future_snapshots_csv(database, destination):
    source, target = Path(database), Path(destination)
    if target.resolve() == source.resolve() or (
            target.exists() and source.exists() and target.samefile(source)):
        raise ValueError("Export destination must not overwrite the archive database")
    snapshots = load_future_snapshots(database)
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = ("source", "trading_symbol", "groww_symbol", "exchange_instrument_id", "expiry_date",
              "received_at_utc", "last_trade_at_utc", "last_price", "volume",
              "open_interest", "previous_open_interest", "oi_day_change", "freshness")
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(snapshots)
    return len(snapshots)

"""Bounded read-only Groww LTP sampling when the socket feed is unavailable.

These are receipt-time observations, not exchange-timestamped ticks or candles.
They must never be admitted to the exchange-feed forecasting quality gate.
"""

from contextlib import closing
from datetime import datetime, timezone
import csv
import math
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .groww import DataAccessError, IST
from .market_calendar import regular_market_status


def save_rest_observation(database, snapshot, *, before_commit=None):
    """Append a receipt-time observation, rolling back if its commit guard raises.

    Guarded writes acquire an exclusive transaction before checking the guard,
    then check it again immediately before commit. A response waiting on an
    archive reader or writer is therefore rejected if its capture has ended.
    """
    if (not isinstance(snapshot, dict) or snapshot.get("source") != "Groww" or
            snapshot.get("symbol") != "NSE-BANKNIFTY" or
            snapshot.get("exchange_timestamp") is not None or
            snapshot.get("freshness") != "unverified"):
        raise ValueError("Invalid receipt-time price observation")
    value = snapshot.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("Invalid observed price")
    observed = datetime.fromisoformat(snapshot["received_at"])
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("Observation time must be timezone-aware")
    observed_at_utc = observed.astimezone(timezone.utc).isoformat()
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        with connection:
            if before_commit is not None:
                # Reserve the lock needed for commit before the final checks.
                # In WAL mode this reserves the writer without blocking readers.
                connection.execute("BEGIN EXCLUSIVE")
                before_commit()
            connection.execute("""CREATE TABLE IF NOT EXISTS rest_price_observations (
                id TEXT PRIMARY KEY, observed_at_utc TEXT NOT NULL,
                provider TEXT NOT NULL, symbol TEXT NOT NULL, value REAL NOT NULL,
                time_basis TEXT NOT NULL,
                CHECK(provider='Groww' AND symbol='NSE-BANKNIFTY' AND value>0
                      AND time_basis='laptop_receipt_no_exchange_timestamp'))""")
            connection.execute("""INSERT INTO rest_price_observations
                (id, observed_at_utc, provider, symbol, value, time_basis)
                VALUES (?,?,?,?,?,?)""", (uuid4().hex, observed_at_utc, "Groww",
                "NSE-BANKNIFTY", float(value), "laptop_receipt_no_exchange_timestamp"))
            if before_commit is not None:
                before_commit()
    return observed_at_utc


class _SamplingBoundaryReached(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(status)


def sample_rest_prices(client, instrument, database, *, minutes, stop_event,
                       interval_seconds=5.0, on_sample=None,
                       monotonic=time.monotonic, now=lambda: datetime.now(timezone.utc)):
    """Poll at a conservative cadence; stop on the first read or archive failure."""
    if (isinstance(minutes, bool) or not isinstance(minutes, int) or
            minutes not in (1, 5, 90, 390) or
            isinstance(interval_seconds, bool) or
            not isinstance(interval_seconds, (int, float)) or
            not math.isfinite(interval_seconds) or interval_seconds < 5):
        raise ValueError("Unsupported sampling duration or interval")
    if instrument.get("groww_symbol") != "NSE-BANKNIFTY":
        raise ValueError("Bank Nifty instrument required")
    count = 0
    deadline = monotonic() + minutes * 60

    def check_boundary():
        if stop_event.is_set():
            raise _SamplingBoundaryReached("stopped")
        if monotonic() >= deadline:
            raise _SamplingBoundaryReached("completed")
        if regular_market_status(now()) != "regular_session":
            raise _SamplingBoundaryReached("market_closed")

    while True:
        try:
            check_boundary()
            snapshot = client.index_snapshot(instrument)
            check_boundary()
        except _SamplingBoundaryReached as boundary:
            return {"status": boundary.status, "saved": count}
        except DataAccessError:
            return {"status": "access_error", "saved": count}
        try:
            observed_at = save_rest_observation(database, snapshot,
                                               before_commit=check_boundary)
        except _SamplingBoundaryReached as boundary:
            return {"status": boundary.status, "saved": count}
        except (sqlite3.Error, OSError, ValueError, KeyError):
            return {"status": "archive_error", "saved": count}
        count += 1
        if on_sample is not None:
            on_sample(count, snapshot["value"], observed_at)
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        stop_event.wait(min(interval_seconds, remaining))
    return {"status": "completed", "saved": count}


def load_rest_observations(database):
    """Read only the dedicated receipt-time table; no exchange time is inferred."""
    path = Path(database)
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rest_price_observations'").fetchone() is None:
            return []
        rows = connection.execute("""SELECT observed_at_utc,value,time_basis
            FROM rest_price_observations ORDER BY observed_at_utc,id""").fetchall()
    return [{"observed_at_utc": row[0], "value": row[1], "time_basis": row[2]}
            for row in rows]


def summarize_rest_observations(database):
    rows = load_rest_observations(database)
    if not rows:
        return {"count": 0, "first": None, "last": None,
                "repeated_prices": 0, "gaps_over_15_seconds": 0,
                "largest_gap_seconds": None, "observed_low": None, "observed_high": None,
                "exchange_freshness": "unverified"}
    times = [datetime.fromisoformat(row["observed_at_utc"]) for row in rows]
    gaps = [(current - previous).total_seconds() for previous, current in zip(times, times[1:])]
    return {"count": len(rows), "first": rows[0]["observed_at_utc"],
            "last": rows[-1]["observed_at_utc"],
            "repeated_prices": sum(a["value"] == b["value"] for a, b in zip(rows, rows[1:])),
            "gaps_over_15_seconds": sum(gap > 15 for gap in gaps),
            "largest_gap_seconds": max(gaps) if gaps else None,
            "observed_low": min(row["value"] for row in rows),
            "observed_high": max(row["value"] for row in rows),
            "exchange_freshness": "unverified"}


def audit_rest_receipt_integrity(database):
    """Review local receipt chronology without claiming exchange-time freshness.

    Rows are read in insertion order so a laptop clock moving backward is visible.
    Gaps across different IST dates are excluded from the within-day gap count.
    A >1% adjacent move within ten seconds is a manual-review flag, not proof
    of a bad quote or an automatic rejection.
    """
    path = Path(database)
    if not path.is_file():
        rows = []
    else:
        with closing(sqlite3.connect(path)) as connection:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rest_price_observations'").fetchone()
            rows = (connection.execute("""SELECT observed_at_utc,value,time_basis
                FROM rest_price_observations ORDER BY rowid""").fetchall() if exists else [])
    review = {"count": len(rows), "clock_regressions": 0,
              "outside_regular_session": 0, "within_day_gaps_over_15_seconds": 0,
              "large_move_review_flags": 0, "unexpected_time_basis": 0,
              "exchange_freshness": "unverified"}
    previous = None
    for raw_time, value, basis in rows:
        if not isinstance(raw_time, str) or (isinstance(value, bool) or
                not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
            raise ValueError("Saved receipt observation is malformed")
        try:
            stamp = datetime.fromisoformat(raw_time)
        except ValueError as exc:
            raise ValueError("Saved receipt time is malformed") from exc
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("Saved receipt time lacks timezone")
        if basis != "laptop_receipt_no_exchange_timestamp":
            review["unexpected_time_basis"] += 1
        if regular_market_status(stamp) != "regular_session":
            review["outside_regular_session"] += 1
        if previous is not None:
            prior_stamp, prior_value = previous
            gap = (stamp - prior_stamp).total_seconds()
            if gap < 0:
                review["clock_regressions"] += 1
            if (stamp.astimezone(IST).date() == prior_stamp.astimezone(IST).date()
                    and gap > 15):
                review["within_day_gaps_over_15_seconds"] += 1
            if 0 <= gap <= 10 and abs(value / prior_value - 1) > 0.01:
                review["large_move_review_flags"] += 1
        previous = (stamp, value)
    return review


def summarize_rest_session_coverage(database):
    """Count receipt coverage on the 375-minute regular grid, not price candles."""
    sessions = {}
    for row in load_rest_observations(database):
        observed = datetime.fromisoformat(row["observed_at_utc"]).astimezone(IST)
        if regular_market_status(observed) != "regular_session":
            continue
        day = observed.date().isoformat()
        minute = observed.strftime("%H:%M")
        slot = sessions.setdefault(day, {}).setdefault(minute, {"count": 0, "quadrants": set()})
        slot["count"] += 1
        slot["quadrants"].add(observed.second // 15)
    return [{"date_ist": day, "regular_minutes": 375,
             "minutes_with_receipts": len(minutes),
             "four_quadrant_minutes": sum(len(slot["quadrants"]) == 4
                                          for slot in minutes.values()),
             "minutes_without_receipts": 375 - len(minutes),
             "receipt_samples": sum(slot["count"] for slot in minutes.values()),
             "exchange_freshness": "unverified"}
            for day, minutes in sorted(sessions.items())]


def export_rest_observations_csv(database, destination):
    source, target = Path(database), Path(destination)
    if target.resolve() == source.resolve() or (
            target.exists() and source.exists() and target.samefile(source)):
        raise ValueError("Export destination must not overwrite the market archive")
    rows = load_rest_observations(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("symbol", "laptop_received_at_utc", "observed_price",
                         "time_basis", "exchange_freshness"))
        for row in rows:
            writer.writerow(("NSE-BANKNIFTY", row["observed_at_utc"], row["value"],
                             row["time_basis"], "unverified"))
    return len(rows)

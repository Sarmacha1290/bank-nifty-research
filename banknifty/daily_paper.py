"""Immutable, pre-session daily-range research references and later scoring."""
from contextlib import closing
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .daily_reference import (_empirical_rank, _previous_regular_day,
                              LOOKBACK, LOWER_RANK, UPPER_RANK, REFERENCE_VERSION)
from .groww import IST
from .market_calendar import OPEN, regular_session, regular_session_date
from .official_daily import load_official_days


def next_regular_day(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("A timezone-aware current time is required")
    local = now.astimezone(IST)
    cursor = local.date()
    if not (regular_session_date(cursor)[0] and local.time() < OPEN):
        cursor += timedelta(days=1)
    while cursor.year == 2026:
        if regular_session_date(cursor)[0]:
            return cursor
        cursor += timedelta(days=1)
    raise ValueError("Next regular session calendar is not verified")


def build_next_reference(bars, *, recorded_at):
    """Freeze a broad pre-open benchmark using only archived prior-day inputs."""
    target = next_regular_day(recorded_at)
    rows = list(bars)
    if len(rows) < LOOKBACK + 1 or any(rows[i].day >= rows[i + 1].day
                                      for i in range(len(rows) - 1)):
        raise ValueError("At least 41 unique chronological official days are required")
    if _previous_regular_day(target) != rows[-1].day:
        raise ValueError("Official archive is not current through the preceding regular session")
    window = rows[-(LOOKBACK + 1):]
    if any(_previous_regular_day(window[i].day) != window[i - 1].day
           for i in range(1, len(window))):
        raise ValueError("The 40-day training window has a missing regular session")
    if any(bar.retrieved_at_utc > recorded_at.astimezone(timezone.utc) for bar in window):
        raise ValueError("Training source was not available at the recording cutoff")
    training = window[1:]
    high_offsets = [bar.high - window[i].close for i, bar in enumerate(training)]
    low_offsets = [bar.low - window[i].close for i, bar in enumerate(training)]
    anchor = rows[-1].close
    lower = anchor + _empirical_rank(low_offsets, LOWER_RANK)
    upper = anchor + _empirical_rank(high_offsets, UPPER_RANK)
    if lower >= upper:
        raise ValueError("Reference interval is invalid")
    provenance = [{"date": bar.day.isoformat(), "open": str(bar.open),
                   "high": str(bar.high), "low": str(bar.low), "close": str(bar.close),
                   "source_sha256": bar.sha256_hex} for bar in window]
    input_hash = sha256(json.dumps(provenance, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "session_date_ist": target.isoformat(),
        "recorded_at_utc": recorded_at.astimezone(timezone.utc).isoformat(),
        "reference_version": REFERENCE_VERSION,
        "previous_official_date": rows[-1].day.isoformat(),
        "training_first_date": training[0].day.isoformat(),
        "training_last_date": training[-1].day.isoformat(),
        "training_days": LOOKBACK,
        "previous_close": str(anchor),
        "reference_lower": str(lower),
        "reference_upper": str(upper),
        "input_sha256": input_hash,
        "interpretation": "pre_open_research_reference_not_validated_or_trading_signal",
    }


def _payload_hash(payload):
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")).hexdigest()


def record_next_reference(database, *, recorded_at=None):
    now = datetime.now(timezone.utc) if recorded_at is None else recorded_at
    payload = build_next_reference(load_official_days(database), recorded_at=now)
    path = Path(database)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS paper_daily_range_reference (
                session_date_ist TEXT PRIMARY KEY, recorded_at_utc TEXT NOT NULL,
                payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL)""")
            old = connection.execute("SELECT payload, payload_sha256 FROM paper_daily_range_reference "
                                     "WHERE session_date_ist=?",
                                     (payload["session_date_ist"],)).fetchone()
            if old is not None:
                if _payload_hash(json.loads(old[0])) != old[1]:
                    raise ValueError("Stored daily reference failed integrity check")
                previous = json.loads(old[0])
                comparable = lambda row: {key: value for key, value in row.items()
                                          if key != "recorded_at_utc"}
                if comparable(previous) != comparable(payload):
                    raise ValueError("Conflicting pre-session reference already exists")
                return "duplicate", previous
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            connection.execute("INSERT INTO paper_daily_range_reference VALUES (?,?,?,?)",
                               (payload["session_date_ist"], payload["recorded_at_utc"],
                                encoded, _payload_hash(payload)))
    return "inserted", payload


def load_scored_references(database):
    path = Path(database)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='paper_daily_range_reference' "
                              "AND type='table'").fetchone() is None:
            return []
        stored = connection.execute("SELECT payload, payload_sha256 FROM "
                                    "paper_daily_range_reference ORDER BY session_date_ist").fetchall()
    official = {bar.day.isoformat(): bar for bar in load_official_days(database)}
    result = []
    for encoded, digest in stored:
        payload = json.loads(encoded)
        if _payload_hash(payload) != digest:
            raise ValueError("Stored daily reference failed integrity check")
        session = regular_session(date.fromisoformat(payload["session_date_ist"]))
        recorded = datetime.fromisoformat(payload["recorded_at_utc"])
        if recorded >= session.open or recorded.tzinfo is None:
            raise ValueError("Stored daily reference is not pre-session")
        row = dict(payload, status="awaiting_official_day", official_low=None,
                   official_high=None, both_extremes_contained=None,
                   reference_width_points=str(Decimal(payload["reference_upper"]) -
                                              Decimal(payload["reference_lower"])))
        bar = official.get(payload["session_date_ist"])
        if bar is not None and bar.retrieved_at_utc > session.close.astimezone(timezone.utc):
            lower, upper = Decimal(payload["reference_lower"]), Decimal(payload["reference_upper"])
            row.update(status="scored_official_day", official_low=str(bar.low),
                       official_high=str(bar.high),
                       both_extremes_contained=lower <= bar.low and bar.high <= upper)
        result.append(row)
    return result


def export_daily_paper_csv(database, destination):
    source, target = Path(database), Path(destination)
    if target.resolve() == source.resolve() or (
            source.exists() and target.exists() and source.samefile(target)):
        raise ValueError("Paper reference export cannot overwrite the market database")
    rows = load_scored_references(database)
    if not rows:
        raise ValueError("No pre-session daily references have been recorded")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="",
                                         suffix=".csv", prefix=".banknifty-daily-paper-",
                                         dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(rows)

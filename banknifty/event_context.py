"""Locally staged bank/news/macro events with explicit first-seen cutoffs.

CSV imports are unverified manual observations. A URL and publication time do
not authenticate a story or prove the software saw it when it was published.
These records are descriptive context only, never an automatic forecast input.
"""

from contextlib import closing
import csv
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qsl, urlsplit


FIELDS = ("kind", "subject", "title", "source_url", "published_at", "event_at")
KINDS = frozenset(("bank_news", "bank_results", "macro_release", "policy_event"))
AUTHENTICITY = "unverified_manual"
MAX_BYTES = 512_000
MAX_ROWS = 250


def _time(value, label):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid {label} timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} timestamp needs a timezone")
    return parsed.astimezone(timezone.utc)


def _clean_text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(
            ord(char) < 32 for char in value):
        raise ValueError(f"Invalid {label}")
    return value.strip()


def parse_event_csv(raw, first_seen_at):
    """Validate a small explicit CSV; preserve the actual import time."""
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError("Event CSV exceeds the reviewed size limit")
    seen = _time(first_seen_at.isoformat() if isinstance(first_seen_at, datetime) else None,
                 "first-seen")
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeError as error:
        raise ValueError("Event CSV must be UTF-8") from error
    reader = csv.DictReader(io.StringIO(decoded))
    if reader.fieldnames != list(FIELDS):
        raise ValueError("Event CSV headers do not match the reviewed format")
    rows = []
    for number, row in enumerate(reader, 1):
        if number > MAX_ROWS or None in row:
            raise ValueError("Event CSV has too many or malformed rows")
        kind = row["kind"].strip() if row["kind"] else ""
        if kind not in KINDS:
            raise ValueError("Unknown event kind")
        subject = _clean_text(row["subject"], "subject", 40)
        title = _clean_text(row["title"], "title", 240)
        url = _clean_text(row["source_url"], "source URL", 500)
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
        except ValueError as error:
            raise ValueError("Invalid event source URL") from error
        if (parsed.scheme != "https" or not host or parsed.username or
                parsed.password or parsed.fragment or any(
                    term in key.lower() for key, _ in parse_qsl(parsed.query)
                    for term in ("token", "api_key", "secret", "password", "authorization"))):
            raise ValueError("Event source must be a plain HTTPS URL")
        published = _time(row["published_at"], "publication")
        event_at = _time(row["event_at"], "event")
        if published > seen:
            raise ValueError("Event publication cannot be later than first observation")
        rows.append({
            "kind": kind, "subject": subject, "title": title, "source_url": url,
            "published_at_utc": published.isoformat(),
            "event_at_utc": event_at.isoformat(),
            "first_seen_at_utc": seen.isoformat(),
            "authenticity": AUTHENTICITY,
            "source_file_sha256": sha256(raw).hexdigest(),
        })
    if not rows:
        raise ValueError("Event CSV has no records")
    return rows


def stage_event_csv(database, csv_path, first_seen_at=None):
    source = Path(csv_path)
    raw = source.read_bytes()
    observed = first_seen_at or datetime.now(timezone.utc)
    rows = parse_event_csv(raw, observed)
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS staged_market_events (
                event_hash TEXT PRIMARY KEY, payload TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL)""")
            inserted = 0
            for row in rows:
                encoded = json.dumps(row, sort_keys=True, separators=(",", ":"))
                digest = sha256(encoded.encode()).hexdigest()
                # Re-importing the same file is idempotent, preserving the first-seen time.
                identity = sha256((row["source_file_sha256"] + "|" +
                                   row["kind"] + "|" + row["subject"] + "|" +
                                   row["published_at_utc"] + "|" +
                                   row["event_at_utc"] + "|" + row["title"]).encode()).hexdigest()
                existing = connection.execute("SELECT payload FROM staged_market_events WHERE event_hash=?",
                                              (identity,)).fetchone()
                if existing is None:
                    connection.execute("INSERT INTO staged_market_events VALUES (?,?,?)",
                                       (identity, encoded, digest))
                    inserted += 1
    return {"parsed": len(rows), "inserted": inserted,
            "authenticity": AUTHENTICITY, "forecast_eligible": False}


def load_known_events(database, as_of):
    cutoff = _time(as_of.isoformat() if isinstance(as_of, datetime) else None, "cutoff")
    path = Path(database)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='staged_market_events'").fetchone() is None:
            return []
        stored = connection.execute("SELECT payload, payload_sha256 FROM staged_market_events").fetchall()
    result = []
    for encoded, digest in stored:
        if sha256(encoded.encode()).hexdigest() != digest:
            raise ValueError("Staged event integrity check failed")
        row = json.loads(encoded)
        if row.get("authenticity") != AUTHENTICITY:
            raise ValueError("Staged event has an unapproved authenticity label")
        if (_time(row.get("first_seen_at_utc"), "first-seen") <= cutoff and
                _time(row.get("published_at_utc"), "publication") <= cutoff):
            result.append(row)
    return sorted(result, key=lambda row: row["event_at_utc"])


def describe_event_window(as_of, known_events):
    """Count only already-known events; absence is not full-feed coverage."""
    cutoff = _time(as_of.isoformat() if isinstance(as_of, datetime) else None, "cutoff")
    recent = []
    upcoming = []
    visible = []
    for item in known_events:
        if item.get("authenticity") != AUTHENTICITY:
            raise ValueError("Event context requires labelled manual observations")
        if (_time(item.get("first_seen_at_utc"), "first-seen") > cutoff or
                _time(item.get("published_at_utc"), "publication") > cutoff):
            continue
        visible.append(item)
        event_at = _time(item.get("event_at_utc"), "event")
        if cutoff - timedelta(minutes=60) < event_at <= cutoff:
            recent.append(item)
        if cutoff < event_at <= cutoff + timedelta(minutes=60):
            upcoming.append(item)
    return {
        "known_recent_bank_events": sum(item["kind"].startswith("bank_") for item in recent) if visible else None,
        "known_recent_macro_events": sum(item["kind"] in ("macro_release", "policy_event") for item in recent) if visible else None,
        "known_upcoming_events": len(upcoming) if visible else None,
        "source_status": "unverified_manual_partial_coverage" if visible else "no_event_source",
        "forecast_eligible": False,
    }

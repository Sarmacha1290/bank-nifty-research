"""Point-in-time Bank Nifty option-expiry metadata from Groww's catalogue.

Never infer an expiry from a weekday. Historical rows need a catalogue
observation that was actually saved before their decision cutoff.
"""

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .derivative_catalog import nearest_banknifty_option_expiry
from .groww import IST
from .market_calendar import regular_session_date
from .storage import record_event


SOURCE = "Groww instrument catalogue"
EVENT_TYPE = "banknifty_option_expiry_reference"


def record_expiry_reference(database, catalog_text, observed_at):
    """Store only the dated expiry, observation time and catalogue hash."""
    if (not isinstance(observed_at, datetime) or observed_at.tzinfo is None or
            observed_at.utcoffset() is None or not isinstance(catalog_text, str)):
        raise ValueError("Catalogue and timezone-aware observation time are required")
    expiry = nearest_banknifty_option_expiry(catalog_text, observed_at.astimezone(IST).date())
    payload = {
        "expiry_date": expiry.isoformat(),
        "observed_at_utc": observed_at.astimezone(timezone.utc).isoformat(),
        "source": SOURCE,
        "catalog_sha256": sha256(catalog_text.encode("utf-8")).hexdigest(),
    }
    record_event(database, EVENT_TYPE, payload)
    return payload


def load_expiry_references(database):
    """Read and integrity-check saved metadata; ignore no other event types."""
    path = Path(database)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone() is None:
            return []
        rows = connection.execute(
            "SELECT payload, sha256 FROM events WHERE event_type=? ORDER BY recorded_at, id",
            (EVENT_TYPE,)).fetchall()
    result = []
    for encoded, digest in rows:
        if sha256(encoded.encode("utf-8")).hexdigest() != digest:
            raise ValueError("Saved expiry-reference audit hash mismatch")
        item = json.loads(encoded)
        _validated_reference(item)
        result.append(item)
    return result


def _validated_reference(reference):
    if not isinstance(reference, dict) or reference.get("source") != SOURCE:
        raise ValueError("Unapproved expiry reference source")
    try:
        observed = datetime.fromisoformat(reference["observed_at_utc"])
        expiry = date.fromisoformat(reference["expiry_date"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Invalid expiry-reference dates") from error
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("Expiry reference has no observation timezone")
    digest = reference.get("catalog_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid expiry catalogue hash")
    return observed, expiry


def expiry_features(as_of, references=()):
    """Use only a same-session catalogue snapshot observed by ``as_of``."""
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("Analysis cutoff must be timezone-aware")
    session_day = as_of.astimezone(IST).date()
    eligible = []
    for reference in references:
        observed, expiry = _validated_reference(reference)
        if (observed <= as_of and observed.astimezone(IST).date() == session_day and
                expiry >= session_day):
            eligible.append((observed, expiry, reference))
    unknown = {
        "expiry_date": None, "calendar_days_to_expiry": None,
        "is_expiry_day": None, "is_pre_expiry_session": None,
        "expiry_reference_observed_at_utc": None,
        "expiry_reference_source": None, "expiry_catalog_sha256": None,
    }
    if not eligible:
        return unknown
    observed, expiry, reference = max(eligible, key=lambda item: item[0])
    cursor = session_day + timedelta(days=1)
    next_session = None
    while cursor <= expiry:
        if regular_session_date(cursor)[0]:
            next_session = cursor
            break
        cursor += timedelta(days=1)
    return {
        "expiry_date": expiry.isoformat(),
        "calendar_days_to_expiry": (expiry - session_day).days,
        "is_expiry_day": expiry == session_day,
        "is_pre_expiry_session": next_session == expiry and expiry != session_day,
        "expiry_reference_observed_at_utc": observed.astimezone(timezone.utc).isoformat(),
        "expiry_reference_source": SOURCE,
        "expiry_catalog_sha256": reference["catalog_sha256"],
    }

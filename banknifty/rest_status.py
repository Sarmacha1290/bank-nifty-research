"""Bounded, credential-free diagnostics for the separate REST receipt sampler."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from uuid import uuid4


STAGES = frozenset({"instrument catalogue", "client creation", "sampling", "finished"})
OUTCOMES = frozenset({"running", "completed", "stopped", "market_closed",
                      "access_error", "archive_error", "preparation_failed", "unexpected_error"})
FIELDS = frozenset({"version", "attempt_id", "started_at_utc", "updated_at_utc",
                    "requested_minutes", "stage", "outcome", "saved",
                    "history_saved", "cleanup_ok", "time_basis"})
BASIS = "laptop_receipt_no_exchange_timestamp"


def validate_rest_status(status):
    """Never accept arbitrary exception strings or extra fields into diagnostics."""
    if not isinstance(status, dict) or set(status) != FIELDS:
        raise ValueError("Invalid REST attempt diagnostic")
    if (type(status["version"]) is not int or status["version"] != 1 or
            not isinstance(status["attempt_id"], str) or
            re.fullmatch(r"[0-9a-f]{32}", status["attempt_id"]) is None or
            type(status["requested_minutes"]) is not int or status["requested_minutes"] not in (1, 5, 90, 390) or
            type(status["saved"]) is not int or not 0 <= status["saved"] <= 5000 or
            status["stage"] not in STAGES or status["outcome"] not in OUTCOMES or
            type(status["history_saved"]) is not bool or type(status["cleanup_ok"]) is not bool or
            status["time_basis"] != BASIS):
        raise ValueError("Invalid REST attempt diagnostic")
    for key in ("started_at_utc", "updated_at_utc"):
        value = status[key]
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError("Invalid REST diagnostic time")
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError("REST diagnostic time must be UTC")
    return status


def new_rest_status(minutes):
    stamp = datetime.now(timezone.utc).isoformat()
    return validate_rest_status({"version": 1, "attempt_id": uuid4().hex,
        "started_at_utc": stamp, "updated_at_utc": stamp, "requested_minutes": minutes,
        "stage": "instrument catalogue", "outcome": "running", "saved": 0,
        "history_saved": False, "cleanup_ok": True, "time_basis": BASIS})


def save_rest_status(data_dir, status):
    validate_rest_status(status)
    folder = Path(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    # Unique temporary names avoid clobbering another window's pending write.
    temporary = folder / ("rest-status-" + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(status, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(folder / "rest-sampling-status.json")
    finally:
        temporary.unlink(missing_ok=True)


def load_rest_status(data_dir):
    path = Path(data_dir) / "rest-sampling-status.json"
    if not path.exists():
        return None
    with path.open("rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("REST diagnostic exceeds its size limit")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate REST diagnostic field")
            result[key] = value
        return result
    try:
        return validate_rest_status(json.loads(raw, object_pairs_hook=unique))
    except (TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid REST attempt diagnostic") from exc


def rest_status_note(data_dir):
    """A saved running state must never imply a recorder is still alive."""
    try:
        status = load_rest_status(data_dir)
    except (OSError, ValueError):
        return "Last REST attempt details are unavailable; no current connection is established."
    if status is None:
        return "No saved REST attempt diagnostic."
    state = ("last progress only; completion unconfirmed" if status["outcome"] == "running"
             else status["outcome"].replace("_", " "))
    warnings = ""
    if status["outcome"] != "running" and not status["history_saved"]:
        warnings += " Attempt history was not saved."
    if not status["cleanup_ok"]:
        warnings += " Client cleanup failed in this attempt."
    return (f"Last saved REST attempt at {status['updated_at_utc']}: {state}, "
            f"stage {status['stage']}, {status['saved']} confirmed saved observations. "
            "This is a historical diagnostic, not a current connection or freshness check." + warnings)

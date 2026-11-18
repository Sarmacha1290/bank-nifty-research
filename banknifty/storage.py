"""Local append-only audit events with credential-labelled fields rejected."""
from datetime import datetime, timezone
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def record_event(database, event_type, payload):
    def check(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if any(word in key.lower() for word in ("token", "authorization", "secret", "password", "api_key")):
                    raise ValueError("Credentials must never be stored in audit records.")
                check(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                check(item)
    check(payload)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    identifier = uuid4().hex
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, recorded_at TEXT, event_type TEXT, payload TEXT, sha256 TEXT)")
            connection.execute("INSERT INTO events VALUES (?,?,?,?,?)", (
                identifier, datetime.now(timezone.utc).isoformat(), event_type,
                encoded, sha256(encoded.encode()).hexdigest()))
    return identifier

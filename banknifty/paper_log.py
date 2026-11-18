"""Append-only experimental rule assessments fixed before later outcomes exist."""

from contextlib import closing
import csv
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import sqlite3

from .agents.luna import LunaAssessment
from .feed import IndexTick
from .feed_technical import build_feed_evidence
from .feed_quality import (MAX_RESEARCH_ENDPOINT_AGE_SECONDS, SAMPLED_SOURCE,
                           accepted_close_age_by_end, inspect_sampled_minutes)
from .market_calendar import regular_session
from .core import prepare_windows
from .groww import IST
from .tick_history import load_index_ticks


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _utc(value):
    return value.astimezone(timezone.utc).isoformat()


def _freeze_origin(ticks, assessments):
    """Rebuild the decision from its original receipt cutoff, then freeze inputs."""
    origin, cutoff = assessments[0].as_of, assessments[0].generated_at
    if any(item.generated_at != cutoff for item in assessments):
        raise ValueError("One paper origin requires one information cutoff")
    selected = []
    for tick in ticks:
        if not isinstance(tick, IndexTick):
            raise ValueError("Frozen origin requires validated index updates")
        for stamp in (tick.exchange_at, tick.received_at):
            if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("Frozen input timestamps must be timezone-aware")
        minute_end = tick.exchange_at.astimezone(IST).replace(second=0, microsecond=0) + timedelta(minutes=1)
        if (origin - timedelta(minutes=61) <= tick.exchange_at < origin and
                tick.exchange_at <= tick.received_at <= cutoff and
                tick.received_at <= minute_end + timedelta(seconds=5)):
            if isinstance(tick.value, bool) or not isinstance(tick.value, (int, float)) or not isfinite(tick.value) or tick.value <= 0:
                raise ValueError("Frozen input values must be finite and positive")
            selected.append(tick)
    selected.sort(key=lambda tick: (tick.exchange_at, tick.received_at))
    try:
        _, candles, snapshot, rebuilt = build_feed_evidence(selected, generated_at=cutoff)
    except ValueError as exc:
        raise ValueError(f"Frozen origin requires eligible point-in-time evidence: {exc}") from None
    by_horizon = {item.horizon_minutes: item for item in rebuilt}
    if snapshot.as_of != origin or any(
            item.horizon_minutes not in by_horizon or
            item.to_dict() != by_horizon[item.horizon_minutes].to_dict() for item in assessments):
        raise ValueError("Assessment does not match its frozen point-in-time evidence")
    inputs = [{"symbol": tick.symbol, "source": tick.source,
               "exchange_at_utc": _utc(tick.exchange_at),
               "received_at_utc": _utc(tick.received_at), "value": float(tick.value)}
              for tick in selected]
    endpoint = selected[-1]
    return {"schema_version": 1, "as_of_utc": _utc(origin), "cutoff_utc": _utc(cutoff),
            "price": snapshot.current_price, "source": snapshot.source,
            "symbol": snapshot.symbol, "endpoint_exchange_at_utc": _utc(endpoint.exchange_at),
            "endpoint_received_at_utc": _utc(endpoint.received_at),
            "endpoint_age_seconds": (origin - endpoint.exchange_at).total_seconds(),
            "input_sha256": sha256(_canonical(inputs).encode("utf-8")).hexdigest(),
            "input_ticks": inputs}


def _checked_origin(assessment):
    """Validate the self-contained frozen evidence; absent legacy evidence stays absent."""
    frozen = assessment.get("frozen_origin")
    if frozen is None:
        return None
    try:
        if frozen["schema_version"] != 1:
            raise ValueError("unsupported schema")
        price = frozen["price"]
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not isfinite(price) or price <= 0:
            raise ValueError("invalid price")
        inputs = frozen["input_ticks"]
        if not inputs or sha256(_canonical(inputs).encode("utf-8")).hexdigest() != frozen["input_sha256"]:
            raise ValueError("input hash mismatch")
        origin = datetime.fromisoformat(frozen["as_of_utc"])
        cutoff = datetime.fromisoformat(frozen["cutoff_utc"])
        if origin != datetime.fromisoformat(assessment["as_of"]) or cutoff != datetime.fromisoformat(assessment["generated_at"]):
            raise ValueError("cutoff mismatch")
        ticks = [IndexTick(row["symbol"], datetime.fromisoformat(row["exchange_at_utc"]),
                           datetime.fromisoformat(row["received_at_utc"]), row["value"], row["source"])
                 for row in inputs]
        # Rebuilding verifies continuity, the endpoint, source and receipt availability;
        # it never reads later rows from the mutable archive.
        _, _, snapshot, _ = build_feed_evidence(ticks, generated_at=cutoff)
        endpoint = ticks[-1]
        if (snapshot.as_of != origin or snapshot.current_price != price or
                snapshot.source != frozen["source"] or snapshot.symbol != frozen["symbol"] or
                frozen["source"] != assessment["source"] or frozen["symbol"] != assessment["symbol"] or
                _utc(endpoint.exchange_at) != frozen["endpoint_exchange_at_utc"] or
                _utc(endpoint.received_at) != frozen["endpoint_received_at_utc"] or
                frozen["endpoint_age_seconds"] != (origin - endpoint.exchange_at).total_seconds() or
                any(not origin - timedelta(minutes=61) <= tick.exchange_at < origin or
                    not tick.exchange_at <= tick.received_at <= cutoff or
                    tick.received_at > tick.exchange_at.astimezone(IST).replace(second=0, microsecond=0) + timedelta(minutes=1, seconds=5)
                    for tick in ticks)):
            raise ValueError("frozen input mismatch")
    except (KeyError, TypeError, IndexError, ValueError, OverflowError) as exc:
        raise ValueError("Stored frozen paper origin failed validation") from exc
    return frozen


def record_paper_assessments(database, assessments, *, recorded_at=None, evidence_ticks=None):
    """Store rule scores with immutable original inputs; never store synthetic replay.

    Callers may pass the original evidence. Otherwise the local archive is read
    with each assessment's original generation cutoff, never today's cutoff.
    """
    rows = list(assessments)
    recorded_at = recorded_at or datetime.now(timezone.utc)
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at must be timezone-aware")
    if any(not isinstance(item, LunaAssessment) for item in rows):
        raise ValueError("Expected Technical Agent assessments")
    if len({item.horizon_minutes for item in rows}) != len(rows):
        raise ValueError("Duplicate assessment horizon")
    results = {"inserted": 0, "duplicates": 0, "ineligible": 0}
    if not rows:
        return results
    origin = rows[0].as_of
    if any(item.as_of != origin or item.symbol != "NSE-BANKNIFTY" or
           item.source != SAMPLED_SOURCE or item.confidence is not None
           for item in rows):
        raise ValueError("Paper log accepts only one real Groww sampled-index origin")
    if any(item.generated_at > recorded_at or item.generated_at < item.as_of
           for item in rows):
        raise ValueError("Assessment timing is inconsistent with recording")
    age = (recorded_at - origin).total_seconds()
    if age < 0 or age > 120:
        raise ValueError("Paper assessment must be recorded within 120 seconds of its origin")
    session = regular_session(origin.astimezone(IST).date())
    windows = {row["horizon_minutes"]: row for row in
               prepare_windows(origin, session, (item.horizon_minutes for item in rows))}
    path = Path(database)
    frozen = _freeze_origin(
        load_index_ticks(path, start=origin - timedelta(minutes=61), end=origin)
        if evidence_ticks is None else evidence_ticks, rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS paper_rule_assessments (
                symbol TEXT NOT NULL, source TEXT NOT NULL, as_of_utc TEXT NOT NULL,
                horizon_minutes INTEGER NOT NULL, recorded_at_utc TEXT NOT NULL,
                payload TEXT NOT NULL, sha256 TEXT NOT NULL,
                PRIMARY KEY (symbol, source, as_of_utc, horizon_minutes))""")
            for item in rows:
                window = windows[item.horizon_minutes]
                if (window["status"] != "ready" or item.bias == "unavailable" or
                        item.score is None or recorded_at > origin + timedelta(minutes=item.horizon_minutes)):
                    results["ineligible"] += 1
                    continue
                current = dict(item.to_dict(), frozen_origin=frozen)
                encoded = _canonical(current)
                digest = sha256(encoded.encode("utf-8")).hexdigest()
                key = (item.symbol, item.source, origin.astimezone(timezone.utc).isoformat(), item.horizon_minutes)
                existing = connection.execute(
                    "SELECT payload, sha256 FROM paper_rule_assessments WHERE symbol=? AND source=? AND as_of_utc=? AND horizon_minutes=?",
                    key).fetchone()
                if existing is not None:
                    if sha256(existing[0].encode("utf-8")).hexdigest() != existing[1]:
                        raise ValueError("Stored paper assessment failed its integrity check")
                    previous = json.loads(existing[0])
                    _checked_origin(previous)
                    previous.pop("generated_at", None)
                    current.pop("generated_at", None)
                    # A later repeated click can use a different calculation time,
                    # but must use identical actual input ticks and origin price.
                    if previous.get("frozen_origin") is not None:
                        previous["frozen_origin"].pop("cutoff_utc", None)
                    current["frozen_origin"] = dict(current["frozen_origin"])
                    current["frozen_origin"].pop("cutoff_utc", None)
                    if previous != current:
                        raise ValueError("Conflicting paper assessment at the same origin")
                    results["duplicates"] += 1
                    continue
                connection.execute("INSERT INTO paper_rule_assessments VALUES (?,?,?,?,?,?,?)",
                                   (*key, recorded_at.astimezone(timezone.utc).isoformat(), encoded, digest))
                results["inserted"] += 1
    return results


def count_paper_assessments(database):
    path = Path(database)
    if not path.exists():
        return 0
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_rule_assessments'").fetchone() is None:
            return 0
        return connection.execute("SELECT COUNT(*) FROM paper_rule_assessments").fetchone()[0]


def resolve_paper_outcomes(database, *, evaluated_at=None, grace_minutes=5):
    """Fix near-end sampled-feed outcomes after a delivery grace period.

    A missing minute becomes an explicit missing_data result. Existing results
    are never changed; they describe the archive as it stood at evaluation.
    """
    evaluated_at = evaluated_at or datetime.now(timezone.utc)
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("evaluated_at must be timezone-aware")
    if not isinstance(grace_minutes, int) or grace_minutes < 0:
        raise ValueError("grace_minutes must be a nonnegative integer")
    path = Path(database)
    result = {"resolved": 0, "pending": 0, "already_resolved": 0, "missing_data": 0}
    if not path.exists():
        return result
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_rule_assessments'").fetchone() is None:
            return result
        assessments = connection.execute(
            "SELECT symbol, source, as_of_utc, horizon_minutes, payload, sha256 "
            "FROM paper_rule_assessments ORDER BY as_of_utc, horizon_minutes").fetchall()
        for row in assessments:
            if sha256(row[4].encode("utf-8")).hexdigest() != row[5]:
                raise ValueError("Stored paper assessment failed its integrity check")
            _checked_origin(json.loads(row[4]))
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS paper_rule_outcomes (
                symbol TEXT NOT NULL, source TEXT NOT NULL, as_of_utc TEXT NOT NULL,
                horizon_minutes INTEGER NOT NULL, evaluated_at_utc TEXT NOT NULL,
                target_at_utc TEXT NOT NULL, status TEXT NOT NULL,
                realized_return_pct REAL, realized_direction TEXT, direction_hit INTEGER,
                assessment_sha256 TEXT NOT NULL,
                PRIMARY KEY (symbol, source, as_of_utc, horizon_minutes))""")
            cache = {}
            for symbol, source, as_of_text, horizon, payload, digest in assessments:
                key = (symbol, source, as_of_text, horizon)
                existing = connection.execute(
                    "SELECT 1 FROM paper_rule_outcomes WHERE symbol=? AND source=? AND as_of_utc=? AND horizon_minutes=?",
                    key).fetchone()
                if existing:
                    result["already_resolved"] += 1
                    continue
                origin = datetime.fromisoformat(as_of_text)
                assessment = json.loads(payload)
                frozen = _checked_origin(assessment)
                target = origin + timedelta(minutes=horizon)
                if evaluated_at < target + timedelta(minutes=grace_minutes):
                    result["pending"] += 1
                    continue
                session = regular_session(origin.astimezone(IST).date())
                date_key = (symbol, source, session.open.date())
                if frozen is not None and date_key not in cache:
                    ticks = load_index_ticks(path, symbol=symbol, start=session.open, end=session.close)
                    timely = []
                    for tick in ticks:
                        local = tick.exchange_at.astimezone(IST)
                        minute_end = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
                        if (tick.exchange_at <= tick.received_at <= evaluated_at and
                                tick.received_at <= minute_end + timedelta(seconds=5)):
                            timely.append(tick)
                    quality, accepted = inspect_sampled_minutes(timely)
                    cache[date_key] = (accepted, accepted_close_age_by_end(quality))
                value = None
                if frozen is None:
                    status = "legacy_unfrozen_origin"
                else:
                    candles, close_ages = cache[date_key]
                    by_end = {candle.end.astimezone(timezone.utc): candle for candle in candles}
                    status = prepare_windows(origin, session, (horizon,))[0]["status"]
                    # Only future coverage is resolved from the archive. The
                    # origin denominator and its quality are frozen already.
                    if status == "ready" and any(origin + timedelta(minutes=minute) not in by_end
                                                 for minute in range(1, horizon + 1)):
                        status = "missing_data"
                    if status == "ready":
                        target_age = close_ages.get(target.astimezone(timezone.utc))
                        if target_age is None or target_age > MAX_RESEARCH_ENDPOINT_AGE_SECONDS:
                            status = "weak_target_edge"
                        else:
                            value = 100.0 * (by_end[target].close / frozen["price"] - 1.0)
                direction = None if value is None else "up" if value > 0 else "down" if value < 0 else "flat"
                bias = assessment["bias"]
                hit = None if value is None or bias == "neutral" else int(
                    (direction == "up") if bias == "bullish" else (direction == "down"))
                connection.execute("INSERT INTO paper_rule_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                   (*key, evaluated_at.astimezone(timezone.utc).isoformat(),
                                    target.astimezone(timezone.utc).isoformat(), status,
                                    value, direction, hit, digest))
                result["resolved"] += 1
                if status == "missing_data":
                    result["missing_data"] += 1
    return result


def paper_outcome_summary(database):
    """Return per-horizon label coverage and directional hit rate, if present."""
    path = Path(database)
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_rule_outcomes'").fetchone() is None:
            return {}
        rows = connection.execute("""SELECT o.horizon_minutes, o.status, o.direction_hit,
            a.payload, a.sha256 FROM paper_rule_outcomes AS o
            JOIN paper_rule_assessments AS a USING (symbol, source, as_of_utc, horizon_minutes)
            ORDER BY o.horizon_minutes""").fetchall()
    result = {}
    for horizon, status, hit, payload, digest in rows:
        if sha256(payload.encode("utf-8")).hexdigest() != digest:
            raise ValueError("Stored paper assessment failed its integrity check")
        frozen = _checked_origin(json.loads(payload))
        item = result.setdefault(horizon, {"resolved": 0, "labelled": 0,
            "directional_calls": 0, "directional_hits": 0, "directional_hit_rate": None,
            "legacy_unscored": 0})
        item["resolved"] += 1
        if frozen is None:
            item["legacy_unscored"] += 1
            continue
        item["labelled"] += int(status == "ready")
        item["directional_calls"] += int(status == "ready" and hit is not None)
        item["directional_hits"] += int(status == "ready" and hit == 1)
    for item in result.values():
        if item["directional_calls"]:
            item["directional_hit_rate"] = item["directional_hits"] / item["directional_calls"]
    return result


def export_paper_report_csv(database, destination):
    """Export fixed paper assessments and later outcomes with explicit status."""
    source, target = Path(database), Path(destination)
    if source.resolve() == target.resolve() or (
        source.exists() and target.exists() and source.samefile(target)
    ):
        raise ValueError("Report destination must not overwrite the database")
    rows = []
    if source.exists():
        with closing(sqlite3.connect(source)) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_rule_assessments'").fetchone():
                has_outcomes = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_rule_outcomes'").fetchone() is not None
                if has_outcomes:
                    rows = connection.execute("""SELECT a.symbol, a.source, a.as_of_utc,
                        a.horizon_minutes, a.recorded_at_utc, a.payload, a.sha256,
                        o.target_at_utc, o.status, o.realized_return_pct,
                        o.realized_direction, o.direction_hit, o.evaluated_at_utc
                        FROM paper_rule_assessments AS a LEFT JOIN paper_rule_outcomes AS o
                        ON a.symbol=o.symbol AND a.source=o.source AND a.as_of_utc=o.as_of_utc
                        AND a.horizon_minutes=o.horizon_minutes
                        ORDER BY a.as_of_utc, a.horizon_minutes""").fetchall()
                else:
                    rows = [(*row, None, None, None, None, None, None) for row in connection.execute(
                        "SELECT symbol, source, as_of_utc, horizon_minutes, recorded_at_utc, payload, sha256 "
                        "FROM paper_rule_assessments ORDER BY as_of_utc, horizon_minutes").fetchall()]
    def excel_safe(value):
        if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@")) or
                                       value.startswith(("\t", "\r", "\n"))):
            return "'" + value
        return value
    output = []
    for row in rows:
        if sha256(row[5].encode("utf-8")).hexdigest() != row[6]:
            raise ValueError("Stored paper assessment failed its integrity check")
        assessment = json.loads(row[5])
        frozen = _checked_origin(assessment)
        target_at = row[7] or (datetime.fromisoformat(row[2]) + timedelta(minutes=row[3])).isoformat()
        outcome = (row[8] or "pending", row[9], row[10], row[11]) if frozen else (
            "legacy_unfrozen_origin", None, None, None)
        output.append((row[0], row[1], row[2], row[3], row[4], assessment["bias"],
                       assessment["score"], assessment["formula_version"],
                       target_at, *outcome, row[12],
                       frozen["price"] if frozen else None,
                       frozen["input_sha256"] if frozen else None,
                       frozen["cutoff_utc"] if frozen else None))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("symbol", "source", "as_of_utc", "horizon_minutes", "recorded_at_utc",
                         "bias", "rules_score_not_probability", "formula_version", "target_at_utc",
                         "outcome_status", "realized_return_pct", "realized_direction",
                         "direction_hit", "evaluated_at_utc", "frozen_origin_price",
                         "input_sha256", "information_cutoff_utc"))
        for row in output:
            writer.writerow([excel_safe(value) for value in row])
    return len(output)

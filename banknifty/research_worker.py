"""Manually enabled, bounded local archive analysis; no market-data transport.

Only the existing signal journal and a last-status checkpoint are written.
There is no fill-recording interface, service registration or startup hook.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import threading
import time

from .market_archive import read_market_snapshot
from .options_data import QualityPolicy, canonical_fingerprint
from .options_ledger import OptionsLedger
from .options_pipeline import PipelinePolicy, PipelineSession, closed_boundary
from .options_strategy import ResearchPolicy

WORKER_VERSION = "local-archive-worker-v1"
MAX_DURATION_SECONDS = 8 * 60 * 60
MAX_STATE_BYTES = 400_000
MAX_CHECKPOINT = 1024


def _utcnow():
    return datetime.now(timezone.utc)


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Worker timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _stamp(value):
    return _aware(value).isoformat()


def _parse_stamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid worker timestamp")
    return _aware(datetime.fromisoformat(value))


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _stamp(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _canonical(value):
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    try:
        return hashlib.sha256(_canonical(value).encode()).hexdigest()
    except RecursionError:
        raise ValueError("Worker state nesting exceeds limits") from None


def _hex(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate state key")
        result[key] = value
    return result


def _read_json(path, limit):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Missing, linked or oversized worker state")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Worker state exceeds limit")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite state value")))
    except RecursionError:
        raise ValueError("Worker state nesting exceeds limits") from None
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 32:
            raise ValueError("Worker state nesting exceeds limits")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


@dataclass(frozen=True)
class WorkerConfig:
    enabled: bool = False
    archive_kind: str = "market"
    duration_seconds: float = 300
    poll_interval_seconds: float = 1
    decision_delay_seconds: float = 1
    strategy: str = "DEBIT_SPREAD"
    research_policy: ResearchPolicy = ResearchPolicy()
    quality_policy: QualityPolicy = QualityPolicy()
    pipeline_policy: PipelinePolicy = PipelinePolicy()

    def __post_init__(self):
        if type(self.enabled) is not bool or self.archive_kind not in {"market", "synthetic"}:
            raise ValueError("Explicit enabled flag and supported archive kind required")
        for name, limit in (("duration_seconds", MAX_DURATION_SECONDS), ("poll_interval_seconds", 60), ("decision_delay_seconds", 59)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0 or value > limit:
                raise ValueError(f"Invalid bounded {name}")
        if self.poll_interval_seconds < 0.05:
            raise ValueError("Polling faster than 50 milliseconds is unsupported")
        if self.strategy not in {"LONG_OPTION", "DEBIT_SPREAD", "CREDIT_SPREAD"}:
            raise ValueError("Unsupported research strategy")
        if not isinstance(self.research_policy, ResearchPolicy) or not isinstance(self.quality_policy, QualityPolicy) or not isinstance(self.pipeline_policy, PipelinePolicy):
            raise ValueError("Typed research, quote quality and pipeline policies are required")
        if self.decision_delay_seconds > self.pipeline_policy.max_decision_delay_seconds:
            raise ValueError("Decision delay must stay within the pipeline decision gate")

    @property
    def fingerprint(self):
        return _sha(asdict(self))


def _policy(cls, data):
    if not isinstance(data, dict) or set(data) != {f.name for f in fields(cls)}:
        raise ValueError("Worker policy schema mismatch")
    converted = dict(data)
    for field in fields(cls):
        if "Decimal" in str(field.type) and converted[field.name] is not None:
            try:
                converted[field.name] = Decimal(str(converted[field.name]))
            except InvalidOperation:
                raise ValueError("Invalid worker policy decimal") from None
        elif "datetime" in str(field.type) and converted[field.name] is not None:
            converted[field.name] = _parse_stamp(converted[field.name])
    return cls(**converted)


def config_from_dict(data):
    if not isinstance(data, dict) or set(data) != {f.name for f in fields(WorkerConfig)}:
        raise ValueError("Worker configuration schema mismatch")
    converted = dict(data)
    for name, cls in (("research_policy", ResearchPolicy), ("quality_policy", QualityPolicy), ("pipeline_policy", PipelinePolicy)):
        converted[name] = _policy(cls, converted[name])
    return WorkerConfig(**converted)


_STATE_FIELDS = {"worker_version", "config", "config_fingerprint", "namespace", "last_seen_at", "last_boundary",
                 "last_cutoff", "last_attempt_id", "last_signal_id", "last_status", "reasons", "checkpoint"}


def validate_worker_state(envelope):
    """Return validated last-status payload; safe for backup/read-only consumers."""
    if not isinstance(envelope, dict) or set(envelope) != {"schema_version", "payload", "sha256"} or type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
        raise ValueError("Unsupported worker state envelope")
    payload = envelope["payload"]
    if not isinstance(payload, dict) or set(payload) != _STATE_FIELDS or payload["worker_version"] != WORKER_VERSION:
        raise ValueError("Unsupported worker state fields")
    if not _hex(envelope["sha256"]) or _sha(payload) != envelope["sha256"]:
        raise ValueError("Worker state integrity mismatch")
    config = config_from_dict(payload["config"])
    if payload["config_fingerprint"] != config.fingerprint or payload["namespace"] != "archive-worker:" + config.archive_kind:
        raise ValueError("Worker configuration fingerprint mismatch")
    for name in ("last_seen_at", "last_boundary", "last_cutoff"):
        if payload[name] is not None:
            _parse_stamp(payload[name])
    for name in ("last_attempt_id", "last_signal_id"):
        if payload[name] is not None and not _hex(payload[name]):
            raise ValueError("Invalid worker audit identity")
    if not isinstance(payload["last_status"], str) or not 1 <= len(payload["last_status"]) <= 80:
        raise ValueError("Invalid worker status")
    if not isinstance(payload["reasons"], list) or len(payload["reasons"]) > 100 or any(not isinstance(s, str) or len(s) > 400 for s in payload["reasons"]):
        raise ValueError("Invalid worker reasons")
    pairs = payload["checkpoint"]
    if not isinstance(pairs, list) or len(pairs) > MAX_CHECKPOINT or any(not isinstance(pair, list) or len(pair) != 2 or not all(_hex(x) for x in pair) for pair in pairs):
        raise ValueError("Invalid worker pipeline checkpoint")
    if len({pair[0] for pair in pairs}) != len(pairs):
        raise ValueError("Duplicate worker checkpoint identity")
    return payload


def read_worker_status(storage_dir, *, as_of=None):
    """Read status only; heartbeat freshness does not prove an active process."""
    path = Path(storage_dir) / "worker-state.json"
    if not path.exists():
        return {"status": "not_started", "heartbeat_fresh": False, "label": "LOCAL ARCHIVE ANALYSIS ONLY"}
    try:
        payload = validate_worker_state(_read_json(path, MAX_STATE_BYTES))
        now = _aware(as_of or _utcnow())
        age = None if payload["last_seen_at"] is None else (now - _parse_stamp(payload["last_seen_at"])).total_seconds()
        freshness = max(10, 2 * payload["config"]["poll_interval_seconds"])
        return {"status": payload["last_status"], "heartbeat_fresh": age is not None and 0 <= age <= freshness,
                "last_seen_at": payload["last_seen_at"], "last_cutoff": payload["last_cutoff"],
                "last_signal_id": payload["last_signal_id"], "config_fingerprint": payload["config_fingerprint"],
                "archive_kind": payload["config"]["archive_kind"], "reasons": payload["reasons"],
                "label": "LOCAL ARCHIVE ANALYSIS ONLY", "actual_fills_created": 0}
    except (ValueError, TypeError, OSError, KeyError, OverflowError):
        return {"status": "invalid_checkpoint", "heartbeat_fresh": False,
                "label": "LOCAL ARCHIVE ANALYSIS ONLY", "reasons": ["Worker checkpoint validation failed; analysis blocked"]}


@dataclass(frozen=True)
class WorkerStep:
    status: str
    attempt_id: str | None = None
    signal_id: str | None = None
    reasons: tuple[str, ...] = ()
    actual_fills_created: int = 0


class _PublicationBlocked(Exception):
    def __init__(self, status, now, reasons):
        self.status, self.now, self.reasons = status, now, reasons


class ArchiveResearchWorker:
    def __init__(self, storage_dir, config=WorkerConfig(), *, clock=_utcnow, monotonic=time.monotonic):
        self.storage_dir, self.config = Path(storage_dir), config
        if not isinstance(config, WorkerConfig):
            raise TypeError("Expected WorkerConfig")
        self.clock, self.monotonic = clock, monotonic
        self.archive_path = self.storage_dir / (config.archive_kind + "-archive.sqlite3")
        self.ledger_path = self.storage_dir / "options-ledger.sqlite3"
        self.state_path = self.storage_dir / "worker-state.json"
        self._started = self._last_monotonic = None
        self._terminal = None
        self._invalid_state = False
        self.state = {"worker_version": WORKER_VERSION, "config": _jsonable(asdict(config)), "config_fingerprint": config.fingerprint,
                      "namespace": "archive-worker:" + config.archive_kind, "last_seen_at": None, "last_boundary": None,
                      "last_cutoff": None, "last_attempt_id": None, "last_signal_id": None,
                      "last_status": "not_started", "reasons": [], "checkpoint": []}
        if config.enabled and self.state_path.exists():
            try:
                previous = validate_worker_state(_read_json(self.state_path, MAX_STATE_BYTES))
                if previous["namespace"] == self.state["namespace"]:
                    for name in ("last_seen_at", "last_boundary", "last_cutoff", "last_attempt_id", "last_signal_id", "checkpoint"):
                        self.state[name] = previous[name]
            except (ValueError, TypeError, OSError, KeyError, OverflowError):
                self._invalid_state = True
        self.session = PipelineSession(self.state["checkpoint"], namespace=self.state["namespace"])

    def _save(self, status, now, reasons=(), *, attempt_id=None, signal_id=None):
        self.state.update(last_status=status, reasons=list(reasons)[:100])
        if self.state["last_seen_at"] is None or now >= _parse_stamp(self.state["last_seen_at"]):
            self.state["last_seen_at"] = _stamp(now)
        if attempt_id is not None:
            self.state["last_attempt_id"] = attempt_id
        if signal_id is not None:
            self.state["last_signal_id"] = signal_id
        self.state["checkpoint"] = [list(pair) for pair in self.session.checkpoint()[-MAX_CHECKPOINT:]]
        envelope = {"schema_version": 1, "payload": self.state, "sha256": _sha(self.state)}
        validate_worker_state(envelope)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        pending = self.storage_dir / "worker-state.pending"
        pending.write_text(_canonical(envelope), encoding="utf-8")
        pending.replace(self.state_path)
        return WorkerStep(status, attempt_id, signal_id, tuple(reasons))

    def _paused(self):
        try:
            value = _read_json(self.storage_dir / "workspace-state.json", 32000)
            allowed = {"version", "kind", "anchor", "import_id", "extension", "settings", "imported_at", "paused"}
            if (not isinstance(value, dict) or set(value) - allowed or type(value.get("version")) is not int
                    or value["version"] != 1 or type(value.get("paused")) is not bool
                    or value.get("kind") not in {"empty", "synthetic", "import"}
                    or ("settings" in value and not isinstance(value["settings"], dict))):
                raise ValueError("Invalid workspace pause state")
            if value["kind"] == "synthetic":
                _parse_stamp(value["anchor"])
            elif value["kind"] == "import":
                if not _hex(value.get("import_id")) or value.get("extension") not in {".json", ".csv"}:
                    raise ValueError("Invalid workspace import identity")
                _parse_stamp(value["imported_at"])
            if value.get("settings", {}).get("archive_kind", "market") not in {"market", "synthetic"}:
                raise ValueError("Invalid workspace archive kind")
            return value["paused"], "entries_paused" if value["paused"] else None
        except (ValueError, TypeError, KeyError, OSError):
            return True, "workspace_pause_state_missing_or_invalid"

    def _existing(self, boundary):
        if not self.ledger_path.exists():
            return None
        for signal in reversed(OptionsLedger(self.ledger_path, read_only=True).list_signals()):
            worker = signal.get("worker", {})
            if (isinstance(worker, dict) and worker.get("version") == WORKER_VERSION
                    and worker.get("namespace") == self.state["namespace"] and worker.get("boundary") == _stamp(boundary)):
                return signal
        return None

    def _audit(self, boundary, cutoff, now, status, reasons, *, decision=None, snapshot=None, cancel_check=None):
        attempt_id = canonical_fingerprint(WORKER_VERSION, self.state["namespace"], boundary, self.config.fingerprint)
        if decision is not None and decision.signal is not None and status in {"candidate", "no_trade"}:
            payload = decision.signal.to_dict()
            payload["pipeline_signal_id"] = payload["signal_id"]
            payload["signal_id"] = attempt_id
        else:
            payload = {"signal_id": attempt_id, "status": "no_trade", "as_of": _stamp(cutoff), "legs": [],
                       "reasons": list(reasons), "label": "local_archive_unavailable" if snapshot is None else snapshot.label,
                       "confidence": None, "trading_allowed": False,
                       "input_fingerprint": decision.input_fingerprint if decision is not None else canonical_fingerprint(attempt_id, reasons)}
        payload["worker"] = {"version": WORKER_VERSION, "namespace": self.state["namespace"], "boundary": _stamp(boundary),
                             "cutoff": _stamp(cutoff), "config_fingerprint": self.config.fingerprint,
                             "config": _jsonable(asdict(self.config)), "attempt_status": status,
                             "archive_mode": "observed", "archive_fingerprint": getattr(snapshot, "fingerprint", None),
                             "manifest_ids": list(getattr(snapshot, "manifest_ids", ())), "actual_fills_created": 0}
        if decision is not None:
            payload["worker"]["pipeline"] = decision.to_dict()
        committed_at = [now]
        def recording_clock():
            current, elapsed_clock = _aware(self.clock()), self.monotonic()
            if (type(elapsed_clock) not in (int, float) or not math.isfinite(elapsed_clock)
                    or elapsed_clock < self._last_monotonic or current < committed_at[0]):
                raise _PublicationBlocked("clock_regression", now, ("Clock moved backward before journal insertion; publication blocked",))
            self._last_monotonic = elapsed_clock
            committed_at[0] = current
            return current
        def publication_guard():
            paused, reason = self._paused()
            cancelled = cancel_check is not None and cancel_check()
            # Filesystem/control callbacks may themselves take time. Evaluate
            # the deadline using a clock sampled after those reads complete.
            current = recording_clock()
            if cancelled:
                raise _PublicationBlocked("cancelled", current, ("Cancelled before journal insertion",))
            if paused:
                raise _PublicationBlocked("paused", current, (reason,))
            if (current > boundary + timedelta(seconds=self.config.pipeline_policy.max_decision_delay_seconds)
                    or self._last_monotonic - self._started >= self.config.duration_seconds):
                raise _PublicationBlocked("missed_window", current, ("Journal preparation completed after the decision window; entry blocked",))
        try:
            OptionsLedger(self.ledger_path).append_signal(attempt_id, payload, recorded_at=now,
                recorded_at_clock=recording_clock,
                pre_commit=publication_guard if decision is not None and status in {"candidate", "no_trade"} else None)
        except _PublicationBlocked as blocked:
            if blocked.status in {"cancelled", "clock_regression"}:
                self._terminal = self._save(blocked.status, blocked.now, blocked.reasons)
                return self._terminal
            return self._audit(boundary, cutoff, blocked.now, blocked.status, blocked.reasons,
                               decision=decision, snapshot=snapshot)
        self.state["last_boundary"], self.state["last_cutoff"] = _stamp(boundary), _stamp(cutoff)
        return self._save(status, committed_at[0], reasons, attempt_id=attempt_id, signal_id=attempt_id)

    def step(self, *, cancelled=False, cancel_check=None):
        if not self.config.enabled:
            return WorkerStep("inactive", reasons=("Explicit enable flag required; no files changed",))
        if self._invalid_state:
            return WorkerStep("invalid_checkpoint", reasons=("Checkpoint validation failed; existing state preserved",))
        if self._terminal is not None:
            return self._terminal
        now, elapsed_clock = _aware(self.clock()), self.monotonic()
        if type(elapsed_clock) not in (int, float) or not math.isfinite(elapsed_clock):
            raise ValueError("Monotonic clock must be finite")
        if self._started is None:
            self._started = elapsed_clock
        regression = ((self._last_monotonic is not None and elapsed_clock < self._last_monotonic)
                      or (self.state["last_seen_at"] is not None and now < _parse_stamp(self.state["last_seen_at"])))
        self._last_monotonic = elapsed_clock
        if regression:
            self._terminal = self._save("clock_regression", now, ("Clock moved backward; restart after clock review",))
            return self._terminal
        if cancelled or (cancel_check is not None and cancel_check()):
            self._terminal = self._save("cancelled", now, ("Local worker cancelled; existing fills unchanged",))
            return self._terminal
        if elapsed_clock - self._started >= self.config.duration_seconds:
            self._terminal = self._save("duration_complete", now, ("Configured bounded duration reached",))
            return self._terminal
        boundary = closed_boundary(now)
        cutoff = boundary + timedelta(seconds=self.config.decision_delay_seconds)
        if now < cutoff:
            return self._save("waiting_boundary", now)
        if self.state["last_boundary"] is not None and boundary <= _parse_stamp(self.state["last_boundary"]):
            return self._save("boundary_already_processed", now)
        previous = self._existing(boundary)
        if previous is not None:
            self.state["last_boundary"], self.state["last_cutoff"] = _stamp(boundary), previous["worker"]["cutoff"]
            return self._save("boundary_already_processed", now, ("Immutable journal attempt recovered after restart",),
                              attempt_id=previous["signal_id"], signal_id=previous["signal_id"])
        deadline = boundary + timedelta(seconds=self.config.pipeline_policy.max_decision_delay_seconds)
        if now > deadline:
            return self._audit(boundary, cutoff, now, "missed_window", ("Missed decision window; no retrospective entry",))
        paused, reason = self._paused()
        if paused:
            return self._audit(boundary, cutoff, now, "paused", (reason,))
        try:
            snapshot = read_market_snapshot(self.archive_path, cutoff, mode="observed", start_at=boundary-timedelta(minutes=11))
        except (ValueError, OSError):
            return self._audit(boundary, cutoff, now, "archive_unavailable", ("Archive missing or invalid; no price was substituted",))
        emission = self.session.process(snapshot.underlying, snapshot.contracts, snapshot.quotes,
            as_of=cutoff, strategy=self.config.strategy, research_policy=self.config.research_policy,
            quality_policy=self.config.quality_policy, pipeline_policy=self.config.pipeline_policy,
            conflict_events=snapshot.conflicts)
        if not emission.emitted:
            self.state["last_boundary"], self.state["last_cutoff"] = _stamp(boundary), _stamp(cutoff)
            return self._save("boundary_already_processed", now, (emission.reason,))
        after = _aware(self.clock())
        after_monotonic = self.monotonic()
        if type(after_monotonic) not in (int, float) or not math.isfinite(after_monotonic) or after_monotonic < elapsed_clock or after < now:
            self._terminal = self._save("clock_regression", now, ("Clock moved backward during analysis; publication blocked",))
            return self._terminal
        self._last_monotonic = after_monotonic
        if cancelled or (cancel_check is not None and cancel_check()):
            self._terminal = self._save("cancelled", after, ("Cancelled before journal commit",))
            return self._terminal
        paused, reason = self._paused()
        if paused:
            return self._audit(boundary, cutoff, after, "paused", (reason,), decision=emission.decision, snapshot=snapshot)
        if after > deadline or after_monotonic - self._started >= self.config.duration_seconds:
            return self._audit(boundary, cutoff, after, "missed_window", ("Analysis completed after its decision window; entry blocked",),
                               decision=emission.decision, snapshot=snapshot)
        return self._audit(boundary, cutoff, after, emission.decision.status, emission.decision.reasons,
                           decision=emission.decision, snapshot=snapshot, cancel_check=cancel_check)

    def run(self, cancel_event=None):
        if not self.config.enabled:
            return self.step()
        cancellation = cancel_event or threading.Event()
        with _worker_lease(self.storage_dir):
            while True:
                result = self.step(cancel_check=cancellation.is_set)
                if result.status in {"inactive", "invalid_checkpoint", "cancelled", "duration_complete", "clock_regression"}:
                    return result
                remaining = self.config.duration_seconds - (self.monotonic() - self._started)
                cancellation.wait(max(0, min(self.config.poll_interval_seconds, remaining)))


@contextmanager
def _worker_lease(storage_dir):
    """Nonblocking process lease, released by the OS after exit or a crash."""
    path = Path(storage_dir) / "worker.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0"); stream.flush()
        stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Another local archive worker is already running") from None
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manually enabled bounded local archive research; no broker connection")
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--storage-dir", default="data/options-research")
    parser.add_argument("--archive-kind", choices=("market", "synthetic"), default="market")
    parser.add_argument("--duration-seconds", type=float, default=300)
    parser.add_argument("--poll-interval-seconds", type=float, default=1)
    parser.add_argument("--decision-delay-seconds", type=float, default=1)
    parser.add_argument("--strategy", choices=("LONG_OPTION", "DEBIT_SPREAD", "CREDIT_SPREAD"), default="DEBIT_SPREAD")
    parser.add_argument("--horizon-minutes", type=int, default=30)
    args = parser.parse_args(argv)
    config = WorkerConfig(args.enable, args.archive_kind, args.duration_seconds, args.poll_interval_seconds,
                          args.decision_delay_seconds, args.strategy, ResearchPolicy(horizon_minutes=args.horizon_minutes))
    worker = ArchiveResearchWorker(args.storage_dir, config)
    try:
        result = worker.run()
    except KeyboardInterrupt:
        result = worker.step(cancelled=True)
    print(_canonical(asdict(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

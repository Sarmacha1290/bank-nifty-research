"""Local options research controller and a tab in the existing desktop app.

No client, credentials, broker or order interface is accepted. Imported source
timestamps never verify a live feed. Manual journal actions are explicit.
"""
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import gc
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

from banknifty.options_data import (ContractMetadata, OptionQuote, RawProvenance,
    select_contract_version, validate_quote)

SCHEMA_HELP = """Import options JSON or CSV, maximum 2 MB / 2,000 quotes.
JSON: schema_version=1, contracts=[...], quotes=[...], optional underlying_price.
Contracts: contract_id, underlying, exchange, option_type (CALL/PUT), strike,
expiry_at, lot_size, tick_size, effective_from, effective_until, known_at, listed.
Quotes: contract_id, source_at, received_at, bid, ask; optional bid_size, ask_size,
last, open_interest, oi_source_at, sequence. Use timezone-aware ISO timestamps,
decimal prices as strings and null for unknown values. Missing OI is not zero.
CSV repeats contract metadata alongside each quote. Save the JSON schema example.
All imports are REPLAY / UNVERIFIED IMPORT. Candles cannot establish option fills.
Do not import passwords, API keys, tokens, account details or order instructions.
"""
DISCLAIMER = ("Unvalidated research; confidence unavailable. Manual fills record only "
              "what you report. No orders are placed. A planned stop cannot guarantee "
              "a loss cap. Margin and partial-fill exposure are separate.")
RESULT_KEYS = frozenset({"pipeline", "replay", "evaluation", "explanation", "updated_at"})
RESULT_LIMIT = 4 * 1024 * 1024


class WorkspaceError(ValueError):
    pass


class OperationCancelled(WorkspaceError):
    pass


class WorkspaceBusy(WorkspaceError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat()


def _datetime(value, name="Timestamp"):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise WorkspaceError(f"{name} must be an ISO timestamp with timezone") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise WorkspaceError(f"{name} needs a timezone")
    return result


def _decimal(value, name, *, optional=False):
    if value is None or str(value).strip() == "":
        if optional:
            return None
        raise WorkspaceError(f"{name} is required")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise WorkspaceError(f"{name} must be a finite number") from None
    if not result.is_finite():
        raise WorkspaceError(f"{name} must be a finite number")
    return result


def _integer(value, name, *, optional=False):
    number = _decimal(value, name, optional=optional)
    if number is None:
        return None
    if number != number.to_integral_value():
        raise WorkspaceError(f"{name} must be a whole number")
    return int(number)


def _jsonable(value):
    if hasattr(value, "to_dict"):
        return _jsonable(value.to_dict())
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, bytes):
        return {"sha256": sha256(value).hexdigest(), "bytes": len(value)}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _dump(value):
    return json.dumps(_jsonable(value), sort_keys=True, indent=2, allow_nan=False)


def _strict_json(text):
    """Reject ambiguous, nonfinite or deeply nested saved JSON before use."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate object field")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("Nonfinite JSON constant")

    try:
        result = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
        pending = [(result, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 40:
                raise ValueError("Saved JSON nesting exceeds its bound")
            if isinstance(value, dict):
                pending.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                pending.extend((item, depth + 1) for item in value)
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Nonfinite JSON number")
        return result
    except (ValueError, TypeError, RecursionError):
        # Never include ignored raw content or duplicate field values in errors.
        raise WorkspaceError("Saved JSON failed strict validation") from None


def format_signal(signal, *, direction_basis="manual"):
    lines = [f"{signal.label.upper()} | {signal.status.upper()} | UNVALIDATED RESEARCH",
        f"{signal.strategy} / {signal.direction}; decision {_iso(signal.as_of)}; horizon {signal.horizon_minutes} min",
        "Confidence: unavailable. " + ("Direction follows the frozen data-driven rule; it is unvalidated." if direction_basis == "pipeline" else "Direction is your hypothesis, not a prediction.")]
    for leg in signal.legs:
        lines.append(f"{leg.side} {leg.contract_id} at observed {'ask' if leg.side == 'BUY' else 'bid'} {leg.price}; "
                     f"expiry {_iso(leg.contract.expiry_at)}, dated lot {leg.contract.lot_size}")
    lines += [f"Quoted net premium per unit: {signal.entry_net}",
        f"Suggested research units: {signal.quantity if signal.quantity is not None else 'UNAVAILABLE'}",
        f"Expiry scenario bounds: {_dump(signal.expiry_bounds) if signal.expiry_bounds else 'UNAVAILABLE until explicit sizing and fees'}",
        f"Planned stop P&L/unit: {signal.planned_stop_pnl_per_unit}; target: {signal.planned_target_pnl_per_unit}",
        "Stops are not guaranteed caps. Partial fills may leave an unhedged short."]
    lines.extend(str(reason) for reason in (*signal.reasons, *signal.sizing_reasons))
    lines.append(f"Frozen signal ID: {signal.signal_id}")
    return "\n".join(lines)


def format_replay(result):
    return (f"{result.label.upper()} | {result.status.upper()}\nHYPOTHETICAL REPLAY, NOT ACTUAL FILLS\n"
        f"Entry: {result.entry_at}; exit: {result.exit_at}; reason: {result.exit_reason}\n"
        f"Units: {result.quantity}; simulated net P&L: {result.net_pnl if result.net_pnl is not None else 'UNAVAILABLE'}\n"
        f"Fees: {result.fees_total}; actual fills created: {result.actual_fills_created}\n"
        + "\n".join(str(item) for item in (*result.reasons, *result.gaps)))


def _without_provenance(value):
    if isinstance(value, dict):
        return {key: _without_provenance(item) for key, item in value.items() if key != "provenance"}
    if isinstance(value, list):
        return [_without_provenance(item) for item in value]
    return value


def _reject_credential_fields(value, depth=0):
    if depth > 40:
        raise WorkspaceError("Research evidence nesting exceeds its bound")
    forbidden = {"token", "access_token", "api_key", "api_secret", "password", "authorization", "secret", "credentials", "private_key"}
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower().replace("-", "_") in forbidden:
                raise WorkspaceError("Credential-labelled fields are not research evidence")
            _reject_credential_fields(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _reject_credential_fields(item, depth + 1)


def _validate_summary(payload):
    if not isinstance(payload, dict) or set(payload) - RESULT_KEYS:
        raise WorkspaceError("Invalid results payload")
    _reject_credential_fields(payload)
    from banknifty.options_pipeline import PipelineDecision
    from banknifty.options_replay import ReplayResult
    from banknifty.options_evaluation import EvaluationReport
    from banknifty.explanation_service import ExplanationResult
    for key, model in (("pipeline", PipelineDecision), ("replay", ReplayResult),
                       ("evaluation", EvaluationReport), ("explanation", ExplanationResult)):
        value = payload.get(key)
        if value is None:
            continue
        allowed = {field.name for field in fields(model)} | ({"text"} if key == "explanation" else set())
        legacy_optional = {"data_gate_passed", "conflict_events"} if key == "pipeline" else set()
        if not isinstance(value, dict) or not allowed - legacy_optional <= set(value) <= allowed:
            raise WorkspaceError("Analysis summary has unsupported or missing fields")
    if "updated_at" in payload:
        _datetime(payload["updated_at"])


def _read_results(storage_dir):
    """Read bounded diagnostic summaries; never deserialize executable objects."""
    path = Path(storage_dir) / "analysis-results.json"
    if not path.exists():
        return {}, None
    try:
        if path.is_symlink() or path.stat().st_size > RESULT_LIMIT:
            raise ValueError("Invalid results file")
        envelope = _strict_json(path.read_text(encoding="utf-8"))
        if not isinstance(envelope, dict) or set(envelope) != {"version", "payload", "sha256"} or envelope["version"] != 1:
            raise ValueError("Invalid results schema")
        payload = envelope["payload"]
        _validate_summary(payload)
        digest = sha256(_dump(payload).encode("utf-8")).hexdigest()
        if digest != envelope["sha256"]:
            raise ValueError("Results integrity failed")
        return payload, None
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        return {}, "Saved analysis summary failed validation; rerun the offline workflow."


def read_analysis_results(storage_dir):
    """Public read-only validation hook; malformed summaries are not absence."""
    result, error = _read_results(storage_dir)
    if error:
        raise WorkspaceError(error)
    return result


def _validate_run_payload(payload):
    required = {"replay": {"kind", "signal", "result", "policy", "quality_policy", "quotes", "quote_fingerprints", "timestamp_basis", "availability_times"},
        "pipeline": {"kind", "decision", "archive_mode", "manifest_ids"},
        "evaluation": {"kind", "config", "result"}}
    optional = {"conflict_events"} if isinstance(payload, dict) and payload.get("kind") in {"replay", "pipeline"} else set()
    if (not isinstance(payload, dict) or payload.get("kind") not in required
            or not required[payload["kind"]] <= set(payload) <= required[payload["kind"]] | optional):
        raise WorkspaceError("Research run payload does not match its declared schema")
    _reject_credential_fields(payload)
    def schema(value, model, extra=(), legacy_optional=()):
        allowed = {field.name for field in fields(model)} | set(extra)
        if not isinstance(value, dict) or not allowed - set(legacy_optional) <= set(value) <= allowed:
            raise WorkspaceError("Frozen research object has unsupported or missing fields")
    if "conflict_events" in payload:
        from banknifty.market_archive import ArchiveConflict
        if not isinstance(payload["conflict_events"], list):
            raise WorkspaceError("Conflict evidence must be an explicit list")
        for conflict in payload["conflict_events"]:
            ArchiveConflict.from_dict(conflict)
    if payload["kind"] == "replay":
        from banknifty.options_strategy import ResearchSignal
        from banknifty.options_replay import ReplayResult, ReplayPolicy
        from banknifty.options_data import QualityPolicy
        schema(payload["signal"], ResearchSignal, ("risk_notice",))
        schema(payload["result"], ReplayResult)
        schema(payload["policy"], ReplayPolicy)
        schema(payload["quality_policy"], QualityPolicy)
        if payload["signal"]["trading_allowed"] is not False or payload["result"]["actual_fills_created"] != 0:
            raise WorkspaceError("Research runs cannot authorize trading or create fills")
        if (not isinstance(payload["quotes"], list) or not isinstance(payload["quote_fingerprints"], list)
                or len(payload["quotes"]) != len(payload["quote_fingerprints"]) or not isinstance(payload["timestamp_basis"], str)):
            raise WorkspaceError("Replay input records are incomplete")
        for quote, digest in zip(payload["quotes"], payload["quote_fingerprints"]):
            schema(quote, OptionQuote)
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise WorkspaceError("Replay input fingerprint is malformed")
            _datetime(quote["received_at"])
            if quote["source_at"] is not None:
                _datetime(quote["source_at"])
        availability = payload["availability_times"]
        if availability is not None:
            if not isinstance(availability, dict):
                raise WorkspaceError("Observed availability must be an explicit mapping")
            for stamp in availability.values():
                _datetime(stamp)
    elif payload["kind"] == "pipeline":
        from banknifty.options_pipeline import PipelineDecision
        schema(payload["decision"], PipelineDecision, legacy_optional=("data_gate_passed", "conflict_events"))
        if payload["archive_mode"] not in {"observed", "reconstructed"} or not isinstance(payload["manifest_ids"], list):
            raise WorkspaceError("Pipeline archive lineage is missing")
        if payload["decision"]["trading_allowed"] is not False:
            raise WorkspaceError("Pipeline research cannot authorize trading")
    else:
        from banknifty.options_evaluation import FrozenExperimentConfig, EvaluationReport
        schema(payload["config"], FrozenExperimentConfig)
        FrozenExperimentConfig.from_dict(payload["config"])
        schema(payload["result"], EvaluationReport)
        if payload["config"] != payload["result"]["config"] or payload["result"]["validated"] is not False or payload["result"]["actual_fills_created"] != 0:
            raise WorkspaceError("Evaluation must preserve its frozen configuration and unvalidated execution basis")


def _research_run_snapshot(storage_dir):
    """Read independent committed records; staging is never published evidence."""
    folder = Path(storage_dir) / "research-runs"
    if not folder.exists():
        return [], 0
    if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
        raise WorkspaceError("Invalid research run archive directory")
    files = list(folder.glob("*.json"))
    if len(files) > 1000:
        raise WorkspaceError("Research run archive exceeds its validation bound")
    runs, invalid, total = [], 0, 0
    for path in files:
        try:
            if path.is_symlink() or path.is_junction() or not path.is_file():
                raise ValueError("Invalid research run file")
            size = path.stat().st_size
            total += size
            if size > 8 * 1024 * 1024:
                raise ValueError("Invalid research run file")
            value = _strict_json(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or set(value) != {"version", "run_id", "recorded_at", "payload", "sha256"} or value["version"] != 1:
                raise ValueError("Invalid run schema")
            body = {key: value[key] for key in ("version", "run_id", "recorded_at", "payload")}
            run_id = sha256(_dump(value["payload"]).encode()).hexdigest()
            if value["run_id"] != run_id or path.stem != run_id or value["sha256"] != sha256(_dump(body).encode()).hexdigest():
                raise ValueError("Run checksum mismatch")
            _validate_run_payload(value["payload"])
            _datetime(value["recorded_at"])
            runs.append(value)
        except (ValueError, TypeError, KeyError, OSError, RecursionError):
            # A damaged record is not evidence, but cannot invalidate independent
            # previously committed records. Do not rename/delete or echo its data.
            invalid += 1
        if total > 64 * 1024 * 1024:
            raise WorkspaceError("Research run archive exceeds its validation bound")
    return sorted(runs, key=lambda item: (item["recorded_at"], item["run_id"])), invalid


def read_research_runs(storage_dir):
    """Strict validation for writes/backups; never silently package damaged data."""
    runs, invalid = _research_run_snapshot(storage_dir)
    if invalid:
        raise WorkspaceError("Research run integrity validation failed")
    return runs


def _read_research_history(storage_dir):
    """Recover valid history while exposing bounded, content-free diagnostics."""
    try:
        runs, invalid = _research_run_snapshot(storage_dir)
    except (ValueError, OSError):
        return [], "Immutable research run archive failed validation; historical results are unavailable."
    message = (f"{invalid} saved research record(s) failed validation and were excluded; "
               "valid historical runs remain available. Backup requires all committed records to validate.") if invalid else None
    return runs, message


def research_status(storage_dir, *, as_of=None):
    """Read-only, JSON-safe public snapshot for desktop/browser consumers.

    Opening this snapshot creates no files or connections. The saved research
    clock never substitutes for current freshness. Raw provider records and
    local paths are excluded from the public result.
    """
    instant = _datetime(as_of or _now())
    directory = Path(storage_dir)
    controller = OptionsWorkspaceController(directory, clock=lambda: instant)
    cached, cache_error = _read_results(directory)
    runs, run_error = controller.saved_runs, controller.runs_error
    if run_error:
        cached = {}
    quality = controller.quality_summary(as_of=instant)
    alerts, signals, exposures = [], [], {}
    def alert(code, message, severity="warning", subject="workspace"):
        identity = sha256(f"{subject}:{code}".encode()).hexdigest()
        alerts.append({"id": identity, "code": code, "severity": severity, "subject": subject, "message": message})
    if cache_error:
        alert("analysis_summary_invalid", cache_error)
    if run_error:
        alert("research_runs_invalid", run_error)
    if controller.paused:
        alert("entries_paused", "New entries paused; existing exposure review remains available.", "info")
    if "STALE" in quality["label"]:
        alert("current_quotes_stale", "Saved quotes are stale for current decisions.")
    if "RECEIPT-ONLY" in quality["label"]:
        alert("source_time_unknown", "Receipt time does not establish exchange freshness.")
    ledger_path = directory / "options-ledger.sqlite3"
    if ledger_path.exists():
        try:
            from banknifty.options_ledger import read_ledger
            ledger = read_ledger(ledger_path)
            signals = ledger["signals"]
            exposures = ledger["exposures"]
        except (ValueError, OSError, sqlite3.DatabaseError):
            alert("ledger_unavailable", "Manual ledger could not be validated; positions are unavailable.", "critical")
    latest = signals[-1] if signals else None
    if latest:
        try:
            elapsed = instant >= _datetime(latest["as_of"]) + timedelta(minutes=latest["horizon_minutes"])
        except (TypeError, ValueError, KeyError):
            elapsed = True
        latest = dict(latest, horizon_elapsed=elapsed, usable_for_live_trading=False)
        if elapsed:
            alert("research_horizon_elapsed", "This saved research window has ended; it is not a current entry.", subject=latest["signal_id"])
    for identity, exposure in exposures.items():
        if exposure.get("status") == "unhedged_short":
            alert("unhedged_short", "Reported manual fills leave an unhedged short; spread loss bounds do not apply.", "critical", identity)
    explanation = cached.get("explanation")
    if explanation and (not latest or explanation.get("input_fingerprint") != latest.get("input_fingerprint")):
        explanation = None
        alert("explanation_input_changed", "The saved explanation belongs to different inputs; generate it again for the current research result.")
    replay_run = next((item["payload"] for item in reversed(runs) if item["payload"]["kind"] == "replay"), None)
    latest_replay = replay_run["result"] if replay_run and latest and replay_run["signal"]["input_fingerprint"] == latest.get("input_fingerprint") else None
    archive = {"status": "empty", "label": "No archived corpus"}
    archive_path = controller.archive_path
    if archive_path.exists():
        try:
            from banknifty.market_archive import MarketArchive
            archive = MarketArchive(archive_path, read_only=True).inventory()
        except (ValueError, OSError, sqlite3.DatabaseError):
            archive = {"status": "unavailable", "label": "Archive failed validation"}
            alert("archive_unavailable", "The archived corpus could not be inspected.")
    from banknifty.provider_adapters import provider_readiness
    from banknifty.research_worker import read_worker_status
    return _jsonable(_without_provenance({"schema_version": 1, "as_of": instant,
        "evidence_quality": quality, "paused": controller.paused, "provider_readiness": provider_readiness(),
        "archive": archive, "latest_signal": latest,
        "latest_replay": latest_replay,
        "research_runs": [{"run_id": run["run_id"], "kind": run["payload"]["kind"], "recorded_at": run["recorded_at"]} for run in runs],
        "research_runs_status": "partial" if run_error and runs else "invalid" if run_error else "available" if runs else "empty",
        "pipeline": cached.get("pipeline"), "evaluation": cached.get("evaluation"),
        "explanation": explanation, "summary_updated_at": cached.get("updated_at"),
        "worker": read_worker_status(directory, as_of=instant),
        "contract_metadata": [leg.get("contract", {}) for leg in latest.get("legs", [])] if latest else [],
        "exposures": exposures, "alerts": alerts, "limitations": DISCLAIMER,
        "live_connection_active": False, "automatic_order_capability": False}))


@dataclass(frozen=True)
class WorkspaceMarket:
    contracts: tuple
    quotes: tuple
    label: str
    decision_at: datetime
    underlying_price: Decimal | None
    imported_at: datetime
    availability_times: tuple | None = None
    conflict_events: tuple = ()


def synthetic_market(as_of):
    """Invented contracts and unit lot sizes, never current NSE metadata."""
    as_of = _datetime(as_of)
    known = as_of - timedelta(days=1)
    metadata = RawProvenance("invented demo catalogue", b"synthetic-options-v1", known, "synthetic")
    contracts, quotes = [], []
    for kind in ("CALL", "PUT"):
        for strike in (49900, 50000, 50100):
            identity = f"SYNTH-{kind}-{strike}"
            contracts.append(ContractMetadata(identity, "SYNTH-BANKNIFTY", "SYNTHETIC",
                kind, Decimal(str(strike)), as_of + timedelta(days=7), 1, Decimal("0.05"),
                known, as_of + timedelta(days=7), known, True, metadata))
            premium = {49900: 160, 50000: 100, 50100: 60} if kind == "CALL" else {49900: 60, 50000: 100, 50100: 160}
            for step in range(745):
                time = as_of - timedelta(seconds=1) if step == 0 else as_of + timedelta(seconds=step * 5)
                move = Decimal(step) / Decimal(20) * (1 if kind == "CALL" else -1)
                bid = Decimal(premium[strike]) + move
                raw = _dump({"contract_id": identity, "step": step, "bid": bid}).encode("utf-8")
                quotes.append(OptionQuote(identity, time, time, bid, bid + Decimal("1"),
                    RawProvenance("invented demo quotes", raw, time, "synthetic"),
                    10000, 10000, bid + Decimal("0.5"), 10000, time, step))
    return WorkspaceMarket(tuple(contracts), tuple(quotes), "SYNTHETIC / INVENTED DATA",
                           as_of, Decimal("50000"), as_of)


def sample_import_document(as_of=None):
    market = synthetic_market(as_of or _now())
    contract = market.contracts[1]
    quote = next(item for item in market.quotes if item.contract_id == contract.contract_id)
    def selected(item):
        return {key: value for key, value in _jsonable(item).items() if key != "provenance"}
    return {"schema_version": 1, "underlying_price": "50000",
            "contracts": [selected(contract)], "quotes": [selected(quote)]}


class OptionsWorkspaceController:
    """Pure local orchestration. Cancellation prevents an uncommitted result."""
    def __init__(self, storage_dir, *, clock=_now):
        self.storage_dir = Path(storage_dir)
        self.clock = clock
        self.market = self.latest_signal = self.latest_replay = None
        self.settings = {}
        self.paused = False
        self.restore_message = "No options data loaded. Try the labelled synthetic example."
        self._lock = threading.Lock()
        self._cancel = self._operation = self._state = None
        self._committed = False
        self._restore()
        self.saved_results, self.results_error = _read_results(self.storage_dir)
        self.latest_pipeline = self.latest_evaluation = None
        self.saved_runs, self.runs_error = _read_research_history(self.storage_dir)
        if self.runs_error:
            self.saved_results = {}
            self.restore_message = self.runs_error + " Manual ledger preserved."
        elif self.saved_runs:
            self.restore_message = "Saved offline results restored for historical review; current entries require new validation."

    def status_snapshot(self):
        return research_status(self.storage_dir, as_of=self.clock())

    def _save_results(self, **changes):
        if set(changes) - RESULT_KEYS:
            raise WorkspaceError("Unsupported analysis summary")
        payload = dict(self.saved_results, **_without_provenance(_jsonable(changes)), updated_at=_iso(self.clock()))
        _validate_summary(payload)
        envelope = {"version": 1, "payload": payload, "sha256": sha256(_dump(payload).encode()).hexdigest()}
        text = _dump(envelope)
        if len(text.encode()) > RESULT_LIMIT:
            raise WorkspaceError("Analysis summary exceeds its bound; reduce the experiment size")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        pending = self.storage_dir / "analysis-results.pending"
        pending.write_text(text, encoding="utf-8")
        pending.replace(self.storage_dir / "analysis-results.json")
        self.saved_results, self.results_error = payload, None

    def _save_run(self, kind, **payload):
        body_payload = _jsonable(dict(payload, kind=kind))
        _validate_run_payload(body_payload)
        run_id = sha256(_dump(body_payload).encode()).hexdigest()
        body = {"version": 1, "run_id": run_id, "recorded_at": _iso(self.clock()), "payload": body_payload}
        record = dict(body, sha256=sha256(_dump(body).encode()).hexdigest())
        encoded = _dump(record).encode()
        if len(encoded) > 8 * 1024 * 1024:
            raise WorkspaceError("Research run exceeds 8 MiB; reduce the experiment size")
        existing = read_research_runs(self.storage_dir)
        same = next((run for run in existing if run["run_id"] == run_id), None)
        if same:
            self.saved_runs = existing
            return same
        if len(existing) >= 1000:
            raise WorkspaceError("Research run limit reached; archive the workspace before adding more")
        folder = self.storage_dir / "research-runs"
        if sum(path.stat().st_size for path in folder.glob("*.json")) + len(encoded) > 64 * 1024 * 1024:
            raise WorkspaceError("Research run archive would exceed 64 MiB; archive the workspace before adding more")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (run_id + ".json")
        pending = folder / (run_id + "." + uuid4().hex + ".pending")
        try:
            # Readers and backups only consider *.json. Flush the complete file
            # before publishing it, then link atomically without replacing an
            # existing immutable hash name (including concurrent identical saves).
            with pending.open("xb") as stream:
                if stream.write(encoded) != len(encoded):
                    raise OSError("Incomplete research run staging write")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(pending, path)
            except FileExistsError:
                existing = read_research_runs(self.storage_dir)
                same = next((run for run in existing if run["run_id"] == run_id), None)
                if same is None:
                    raise WorkspaceError("Research run publication conflicted with existing evidence") from None
                self.saved_runs = existing
                return same
        finally:
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                # A crash or failed cleanup may leave staging; it is ignored.
                pass
        self.saved_runs = sorted(existing + [record], key=lambda item: (item["recorded_at"], item["run_id"]))
        return record

    @property
    def archive_path(self):
        name = "synthetic-archive.sqlite3" if self.settings.get("archive_kind") == "synthetic" else "market-archive.sqlite3"
        return self.storage_dir / name

    def archive(self, *, read_only=True):
        from banknifty.market_archive import MarketArchive
        if read_only and not self.archive_path.exists():
            raise WorkspaceError("Import an archived corpus or load the synthetic corpus first")
        return MarketArchive(self.archive_path, read_only=read_only)

    def _select_archive(self, kind):
        self.settings["archive_kind"] = kind
        state = dict(self._state or {"version": 1, "kind": "empty"}, settings=self.settings)
        self._save_state(state)

    def _clear_current_results(self, *, evaluation=False):
        self.latest_signal = self.latest_replay = self.latest_pipeline = None
        changes = {"pipeline": None, "replay": None, "explanation": None}
        if evaluation:
            self.latest_evaluation = None
            changes["evaluation"] = None
        if self.saved_results:
            self._save_results(**changes)

    def archive_diagnostics(self):
        from banknifty.provider_adapters import provider_readiness
        inventory = self.archive().inventory() if self.archive_path.exists() else {"status": "empty"}
        return {"archive_kind": self.settings.get("archive_kind", "market"), "inventory": inventory,
            "provider_readiness": provider_readiness(), "live_connection_active": False,
            "clock_note": "Reconstructed mode uses claimed original timestamps; observed mode respects when records were locally available.",
            "live_gate": "A suitable market session and separately authorized connection are still required."}

    def import_archive(self, path):
        def action(cancel):
            from banknifty.market_archive import MarketArchive
            self._check(cancel)
            archive = MarketArchive(self.storage_dir / "market-archive.sqlite3")
            def save():
                result = archive.import_file(path, imported_at=self.clock(), source="manual-normalized-import")
                self._select_archive("market")
                self._clear_current_results(evaluation=True)
                return {"import": result, "inventory": archive.inventory()}
            return self._commit(cancel, save)
        return self._run("archive-import", action)

    def export_archive_template(self, path):
        target = self._export_target(path, ".json")
        def action(cancel):
            cutoff = self.clock().replace(second=0, microsecond=0)
            market = synthetic_market(cutoff)
            document = {"schema_version": 2,
                "contracts": [_without_provenance(_jsonable(item)) for item in market.contracts],
                "quotes": [_without_provenance(_jsonable(item)) for item in market.quotes if item.received_at <= cutoff],
                "underlying": []}
            for minute in range(10):
                for second in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 59):
                    timestamp = cutoff - timedelta(minutes=10-minute) + timedelta(seconds=second)
                    document["underlying"].append({"symbol": "SYNTH-BANKNIFTY", "value": str(50000 + minute),
                        "source_at": _iso(timestamp), "received_at": _iso(timestamp), "sequence": minute * 60 + second})
            return self._commit(cancel, lambda: target.write_text(_dump(document), encoding="utf-8"))
        return self._run("corpus-template", action)

    def load_archive_demo(self):
        """Separate invented multi-session corpus; no current exchange assertions."""
        def action(cancel):
            from banknifty.market_archive import MarketArchive, ImportManifest
            from banknifty.options_pipeline import UnderlyingObservation
            dates, day = [], self.clock().date() - timedelta(days=1)
            while len(dates) < 6:
                if day.weekday() < 5:
                    dates.append(day)
                day -= timedelta(days=1)
            contracts, quotes, observations = [], [], []
            for index, day in enumerate(sorted(dates)):
                self._check(cancel)
                start = datetime(day.year, day.month, day.day, 3, 50, tzinfo=timezone.utc)
                boundary = start + timedelta(minutes=10)
                market = synthetic_market(boundary)
                suffix = day.isoformat()
                # Distinct dated synthetic symbols prevent invented catalogue rows
                # from masquerading as a listed contract on another session.
                from dataclasses import replace
                for contract in market.contracts:
                    contracts.append(replace(contract, contract_id=contract.contract_id + "-" + suffix,
                                             expiry_at=start + timedelta(hours=6), effective_until=start + timedelta(hours=6)))
                for quote in market.quotes:
                    quotes.append(replace(quote, contract_id=quote.contract_id + "-" + suffix))
                for minute in range(75):
                    for second in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 59):
                        instant = start + timedelta(minutes=minute, seconds=second)
                        value = Decimal("50000") + Decimal(minute) * (1 if index % 2 == 0 else -1) + Decimal(second) / 100
                        raw = _dump({"symbol": "SYNTH-BANKNIFTY", "value": value, "source_at": instant}).encode()
                        observations.append(UnderlyingObservation("SYNTH-BANKNIFTY", value, instant, instant,
                            RawProvenance("invented-corpus", raw, instant, "synthetic"), minute * 60 + second))
            manifest = ImportManifest(source="invented-corpus-v1", imported_at=self.clock(),
                raw_payload=_dump({"demo": "invented-corpus-v1", "dates": [d.isoformat() for d in dates]}).encode(),
                format="synthetic", evidence_kind="synthetic")
            archive = MarketArchive(self.storage_dir / "synthetic-archive.sqlite3")
            def save():
                result = archive.append_batch(contracts=contracts, quotes=quotes, underlying=observations, manifest=manifest)
                self._select_archive("synthetic")
                self._clear_current_results(evaluation=True)
                return {"import": result, "inventory": archive.inventory(), "decision_at": _iso(boundary),
                        "start_at": _iso(datetime.combine(min(dates), datetime.min.time(), timezone.utc)),
                        "end_at": _iso(start + timedelta(minutes=75)), "label": "SYNTHETIC / SIX INVENTED SESSIONS"}
            return self._commit(cancel, save)
        return self._run("archive-demo", action)

    def run_data_pipeline(self, settings, *, as_of, archive_mode="reconstructed"):
        def action(cancel):
            if self.paused:
                raise WorkspaceError("New entries are paused; diagnostics and evaluation remain available")
            from banknifty.options_pipeline import run_pipeline
            cutoff = _datetime(as_of, "Data-driven decision cutoff")
            if cutoff > self.clock():
                raise WorkspaceError("A decision cannot be recorded before its cutoff")
            archive = self.archive()
            snapshot = archive.query(as_of=cutoff, mode=archive_mode, start_at=cutoff - timedelta(minutes=15))
            policy = self.research_policy(settings)
            decision = run_pipeline(snapshot.underlying, snapshot.contracts, snapshot.quotes, as_of=cutoff,
                strategy=settings.get("strategy", "LONG_OPTION"), research_policy=policy,
                conflict_events=snapshot.conflicts)
            self._check(cancel)
            replay_inputs = archive.query(as_of=cutoff + timedelta(minutes=policy.horizon_minutes + 2), mode=archive_mode,
                start_at=cutoff - timedelta(seconds=5))
            ledger = self.ledger() if decision.signal is not None else None
            def save():
                if decision.signal is not None:
                    ledger.append_signal(decision.signal.signal_id, decision.signal, recorded_at=self.clock())
                self.latest_signal, self.latest_pipeline, self.latest_replay = decision.signal, decision, None
                self.market = WorkspaceMarket(replay_inputs.contracts, replay_inputs.quotes, snapshot.label,
                    cutoff, decision.assessment.reference_price if decision.assessment else None, self.clock(),
                    replay_inputs.availability_by_fingerprint if archive_mode == "observed" else None,
                    replay_inputs.conflicts)
                self._save_run("pipeline", decision=decision, archive_mode=archive_mode,
                    manifest_ids=snapshot.manifest_ids, conflict_events=snapshot.conflicts)
                self._save_results(pipeline=decision, replay=None, explanation=None)
                return decision
            return self._commit(cancel, save)
        return self._run("data-driven-analysis", action)

    def evaluate_archive(self, settings, experiment):
        def action(cancel):
            from banknifty.options_evaluation import FrozenExperimentConfig, evaluate_archive
            from banknifty.options_replay import ReplayPolicy
            config = FrozenExperimentConfig(name=experiment.get("name", "Local offline experiment"),
                start_at=_datetime(experiment.get("start_at"), "Experiment start"),
                end_at=_datetime(experiment.get("end_at"), "Experiment end"),
                strategy=settings.get("strategy", "LONG_OPTION"), archive_mode=experiment.get("archive_mode", "reconstructed"),
                research_policy=self.research_policy(settings), replay_policy=ReplayPolicy(
                    manual_latency_seconds=_integer(settings.get("manual_latency"), "Manual latency"),
                    fees_total=_decimal(settings.get("total_fees"), "Replay fees", optional=True),
                    slippage_per_leg=_decimal(settings.get("slippage_per_leg"), "Replay slippage", optional=True)),
                decision_interval_minutes=_integer(experiment.get("decision_interval_minutes", 15), "Decision interval"),
                min_train_sessions=_integer(experiment.get("min_train_sessions", 3), "Training sessions"),
                validation_sessions=_integer(experiment.get("validation_sessions", 1), "Validation sessions"),
                holdout_sessions=_integer(experiment.get("holdout_sessions", 1), "Holdout sessions"),
                max_decisions=200)
            result = evaluate_archive(self.archive(), config, cancelled=cancel.is_set)
            self._check(cancel)
            def save():
                self._save_run("evaluation", config=result.config.to_dict(), result=result)
                self._save_results(evaluation=result)
                self.latest_evaluation = result
                return result
            return self._commit(cancel, save)
        return self._run("offline-evaluation", action)

    def explain_latest(self):
        def action(cancel):
            if self.latest_signal is None:
                raise WorkspaceError("Run a research workflow first")
            from banknifty.explanation_service import local_explanation
            explanation = local_explanation(self.latest_signal)
            self._commit(cancel, lambda: self._save_results(explanation=explanation))
            return explanation
        return self._run("local-explanation", action)

    @staticmethod
    def research_policy(settings):
        from banknifty.options_strategy import ResearchPolicy
        optional = {name: _decimal(settings.get(name), name, optional=True) for name in
            ("capital", "max_risk_fraction", "total_fees", "margin_per_lot", "stop_loss_fraction", "take_profit_fraction", "spread_width")}
        return ResearchPolicy(horizon_minutes=_integer(settings.get("horizon_minutes", 30), "Horizon"),
            max_contracts=_integer(settings.get("max_contracts"), "Maximum lots", optional=True), **optional)

    @property
    def busy(self):
        with self._lock:
            return self._operation is not None

    def cancel(self):
        with self._lock:
            if self._cancel is not None and not self._committed:
                self._cancel.set()
                return True
            return False

    def _run(self, operation, action):
        with self._lock:
            if self._operation is not None:
                raise WorkspaceBusy("An operation is already running; wait or cancel it")
            self._operation = operation
            self._committed = False
            cancellation = self._cancel = threading.Event()
        try:
            return action(cancellation)
        finally:
            with self._lock:
                self._operation = self._cancel = None
                self._committed = False

    @staticmethod
    def _check(cancellation):
        if cancellation.is_set():
            raise OperationCancelled("Cancelled before saving the result")

    def _commit(self, cancellation, action):
        # A cancellation either wins before this barrier or reports that the
        # commit is already underway. No false 'cancelled' promise after it.
        with self._lock:
            self._check(cancellation)
            self._committed = True
        return action()

    def _export_target(self, path, extension):
        target = Path(path).resolve()
        if target.suffix.lower() != extension:
            raise WorkspaceError(f"Choose a {extension} export filename")
        project = Path(__file__).resolve().parents[1]
        if target.is_relative_to(self.storage_dir.resolve()) or target.is_relative_to(project):
            raise WorkspaceError("Export outside the application and its internal data folder")
        if Path(path).is_symlink():
            raise WorkspaceError("Export to a regular local file, not a symbolic link")
        return target

    def _save_state(self, state):
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        state = dict(state, paused=self.paused)
        pending = self.storage_dir / "workspace-state.pending"
        pending.write_text(_dump(state), encoding="utf-8")
        pending.replace(self.storage_dir / "workspace-state.json")
        self._state = state

    def _restore(self):
        path = self.storage_dir / "workspace-state.json"
        if not path.exists():
            return
        try:
            if path.stat().st_size > 32000:
                raise WorkspaceError("Saved state exceeds limit")
            state = _strict_json(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not isinstance(state.get("settings", {}), dict):
                raise WorkspaceError("Saved workspace must be an object")
            if set(state) - {"version", "kind", "anchor", "import_id", "extension", "settings", "imported_at", "paused"}:
                raise WorkspaceError("Unsupported saved workspace fields")
            if state.get("version") != 1 or type(state.get("paused", False)) is not bool:
                raise WorkspaceError("Unsupported saved workspace version")
            self.settings = dict(state.get("settings", {}))
            self.paused = state.get("paused", False)
            if state["kind"] == "synthetic":
                self.market = synthetic_market(_datetime(state["anchor"]))
            elif state["kind"] == "import":
                identity, extension = state["import_id"], state["extension"]
                if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
                    raise WorkspaceError("Invalid saved import identifier")
                if extension not in (".json", ".csv"):
                    raise WorkspaceError("Unsupported saved import format")
                from banknifty.options_import import load_options_file
                imported = load_options_file(self.storage_dir / "imports" / (identity + extension),
                                             imported_at=_datetime(state["imported_at"]))
                if imported.file_sha256 != identity:
                    raise WorkspaceError("Saved import integrity check failed")
                self.market = self._market_from_import(imported)
            elif state["kind"] != "empty":
                raise WorkspaceError("Unsupported saved dataset type")
            self._state = state
            self.restore_message = "Restored local research inputs; current freshness is checked again."
        except (ValueError, TypeError, KeyError, OSError):
            self.market, self.settings, self.paused = None, {}, True
            self.restore_message = "Saved inputs failed validation; new entries paused. Import again. Ledger preserved."

    def set_paused(self, paused):
        if type(paused) is not bool:
            raise WorkspaceError("Pause state must be boolean")
        def action(cancel):
            def save():
                self.paused = paused
                self._save_state(self._state or {"version": 1, "kind": "empty", "settings": self.settings})
                return paused
            return self._commit(cancel, save)
        return self._run("pause", action)

    def load_demo(self):
        def action(cancel):
            now = self.clock()
            market = synthetic_market(now)
            def save():
                self._save_state({"version": 1, "kind": "synthetic", "anchor": _iso(now), "settings": self.settings})
                self._clear_current_results()
                self.market = market
                return market
            return self._commit(cancel, save)
        return self._run("demo", action)

    @staticmethod
    def _market_from_import(imported):
        cutoff = min((q.received_at for q in imported.quotes), default=imported.imported_at)
        return WorkspaceMarket(imported.contracts, imported.quotes, imported.label,
                               cutoff, imported.underlying_price, imported.imported_at)

    def import_file(self, path):
        def action(cancel):
            from banknifty.options_import import load_options_file
            source = Path(path)
            imported = load_options_file(source, imported_at=self.clock())
            self._check(cancel)
            extension = source.suffix.lower()
            if extension not in (".json", ".csv"):
                raise WorkspaceError("Choose a JSON or CSV options dataset")
            with source.open("rb") as stream:
                content = stream.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024 or sha256(content).hexdigest() != imported.file_sha256:
                raise WorkspaceError("Import changed while reading; choose the file again")
            self._check(cancel)
            folder = self.storage_dir / "imports"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / (imported.file_sha256 + extension)
            if not target.exists():
                with target.open("xb") as stream:
                    stream.write(content)
            elif target.stat().st_size != len(content) or sha256(target.read_bytes()).hexdigest() != imported.file_sha256:
                raise WorkspaceError("Existing archived import failed integrity validation; current inputs retained")
            self._check(cancel)
            market = self._market_from_import(imported)
            def save():
                self._save_state({"version": 1, "kind": "import", "import_id": imported.file_sha256,
                    "extension": extension, "imported_at": _iso(imported.imported_at), "settings": self.settings})
                self._clear_current_results()
                self.market = market
                return market
            return self._commit(cancel, save)
        return self._run("import", action)

    def quality_summary(self, *, as_of=None):
        if self.market is None:
            return {"label": "NO DATA", "accepted": 0, "total": 0, "reasons": ("no_dataset",)}
        cutoff = as_of or self.clock()
        # Use the exact decision selector, including duplicate/conflict handling.
        from banknifty.options_strategy import point_in_time_inputs
        _, selected, conflicts = point_in_time_inputs(self.market.contracts, self.market.quotes, cutoff)
        current = {quote.contract_id: quote for quote in selected}
        accepted, reasons = 0, set(conflicts)
        for identity, quote in current.items():
            contract = select_contract_version(self.market.contracts, identity, cutoff)
            if contract is None:
                reasons.add("no_effective_contract_metadata")
                continue
            result = validate_quote(quote, contract, cutoff)
            accepted += int(result.accepted)
            reasons.update(result.reasons)
        receipt_only = any(item.source_at is None for item in current.values())
        label = "RECEIPT-ONLY / UNKNOWN SOURCE FRESHNESS" if receipt_only else self.market.label
        if reasons.intersection(("stale_source", "stale_receipt")):
            label += " / STALE FOR CURRENT USE"
        if not current:
            reasons.add("no_quotes_at_cutoff")
        return {"label": label, "accepted": accepted, "total": len(current), "reasons": tuple(sorted(reasons))}

    def evaluate(self, settings):
        def action(cancel):
            if self.paused:
                raise WorkspaceError("New research entries are paused. Exposure and exit review remain available.")
            if self.market is None:
                raise WorkspaceError("Load an options dataset first")
            from banknifty.options_strategy import ResearchPolicy, generate_signal
            configured = dict(self.settings, **settings)
            cutoff = _datetime(configured.get("as_of") or self.market.decision_at, "Decision cutoff")
            underlying = _decimal(configured.get("underlying_price") or self.market.underlying_price, "Underlying reference")
            optional = {name: _decimal(configured.get(name), name, optional=True) for name in
                ("capital", "max_risk_fraction", "total_fees", "margin_per_lot", "stop_loss_fraction", "take_profit_fraction", "spread_width")}
            policy = ResearchPolicy(horizon_minutes=_integer(configured.get("horizon_minutes", 30), "Horizon"),
                max_contracts=_integer(configured.get("max_contracts"), "Maximum lots", optional=True), **optional)
            quotes = tuple(q for q in self.market.quotes if q.received_at <= cutoff and
                (q.source_at is None or q.source_at <= cutoff) and q.provenance.captured_at <= cutoff)
            signal = generate_signal(self.market.contracts, quotes, as_of=cutoff, underlying_price=underlying,
                direction=configured.get("direction", "NEUTRAL"), strategy=configured.get("strategy", "LONG_OPTION"), policy=policy)
            self._check(cancel)
            ledger = self.ledger()
            def save():
                ledger.append_signal(signal.signal_id, signal, recorded_at=self.clock())
                self._clear_current_results()
                self.settings, self.latest_signal = configured, signal
                if self._state:
                    self._save_state(dict(self._state, settings=configured))
                return signal
            return self._commit(cancel, save)
        return self._run("research", action)

    def replay(self, *, manual_latency_seconds, fees, slippage_per_leg):
        def action(cancel):
            if self.market is None or self.latest_signal is None:
                raise WorkspaceError("Create a research result from the loaded dataset first")
            from banknifty.options_replay import ReplayPolicy, replay_signal
            signal = self.latest_signal
            policy = ReplayPolicy(manual_latency_seconds=_integer(manual_latency_seconds, "Manual latency"),
                horizon_seconds=signal.horizon_minutes * 60,
                fees_total=_decimal(fees, "Replay fees"),
                slippage_per_leg=_decimal(slippage_per_leg, "Replay slippage per leg"),
                quantity=signal.quantity,
                stop_loss_per_unit=abs(signal.planned_stop_pnl_per_unit) if signal.planned_stop_pnl_per_unit is not None else None,
                target_profit_per_unit=signal.planned_target_pnl_per_unit)
            availability = dict(self.market.availability_times) if self.market.availability_times is not None else None
            result = replay_signal(signal, self.market.quotes, policy=policy, cancelled=cancel.is_set,
                availability_times=availability, conflict_events=self.market.conflict_events)
            self._check(cancel)
            identities = {leg.contract_id for leg in signal.legs}
            end = signal.as_of + timedelta(seconds=signal.horizon_minutes * 60 + policy.manual_latency_seconds + policy.max_exit_wait_seconds)
            inputs = tuple(q for q in self.market.quotes if q.contract_id in identities and signal.as_of - timedelta(seconds=10) <= q.received_at <= end)
            def save():
                self._save_run("replay", signal=signal, result=result, policy=policy,
                    quality_policy=result.quality_policy, quotes=inputs,
                    quote_fingerprints=[q.fingerprint for q in inputs],
                    conflict_events=self.market.conflict_events,
                    availability_times={q.fingerprint: availability[q.fingerprint] for q in inputs if q.fingerprint in availability} if availability is not None else None,
                    timestamp_basis="Separate original source_at and received_at; no source timestamp inferred from receipt")
                self._save_results(replay=result)
                self.latest_replay = result
                return result
            return self._commit(cancel, save)
        return self._run("replay", action)

    def ledger(self):
        from banknifty.options_ledger import OptionsLedger
        return OptionsLedger(self.storage_dir / "options-ledger.sqlite3")

    def manual_fill(self, *, signal_id, contract_id, side, quantity, price, occurred_at,
                    event_id=None, external_fill_id=None, fees=None):
        def action(cancel):
            self._check(cancel)
            # Recording an actual execution remains available while entries are paused:
            # it is an audit action, not authorisation to initiate a new position.
            ledger = self.ledger()
            units, actual_price = _integer(quantity, "Filled units"), _decimal(price, "Actual fill price")
            actual_time, actual_fees = _datetime(occurred_at, "Actual fill time"), _decimal(fees, "Actual fees", optional=True)
            return self._commit(cancel, lambda: ledger.record_fill(event_id or uuid4().hex, signal_id, contract_id, side,
                units, actual_price, actual_time, self.clock(), external_fill_id=external_fill_id or None, fees=actual_fees))
        return self._run("manual-fill", action)

    def void_manual_fill(self, event_id, reason):
        if not reason or not str(reason).strip():
            raise WorkspaceError("A correction reason is required")
        def action(cancel):
            ledger = self.ledger()
            return self._commit(cancel, lambda: ledger.correct_fill(
                uuid4().hex, event_id, None, recorded_at=self.clock(), reason=str(reason).strip()))
        return self._run("correction", action)

    def manual_fill_for_correction(self, event_id):
        """Return the latest reported values while preserving original identity."""
        events = self.ledger().events()
        original = next((event["payload"] for event in events
                         if event["event_id"] == event_id and event["kind"] == "fill"), None)
        if original is None:
            raise WorkspaceError("Choose an original manual fill event ID from the journal")
        latest = original
        for event in events:
            if event["kind"] == "correction" and event["payload"]["target_event_id"] == event_id:
                latest = event["payload"]["replacement"] or original
        return dict(latest)

    def replace_manual_fill(self, event_id, *, price, occurred_at, fees, reason):
        """Append a price/time/fee correction; contract, side and units stay fixed."""
        if not reason or not str(reason).strip():
            raise WorkspaceError("A correction reason is required")
        def action(cancel):
            replacement = self.manual_fill_for_correction(event_id)
            replacement.update(price=_decimal(price, "Corrected actual price"),
                occurred_at=_datetime(occurred_at, "Corrected actual time"),
                fees=_decimal(fees, "Corrected total fees", optional=True))
            ledger = self.ledger()
            return self._commit(cancel, lambda: ledger.correct_fill(uuid4().hex, event_id, replacement,
                recorded_at=self.clock(), reason=str(reason).strip()))
        return self._run("replacement-correction", action)

    def export_report(self, path):
        def action(cancel):
            self._check(cancel)
            target = self._export_target(path, ".json")
            runs, runs_error = _read_research_history(self.storage_dir)
            document = _dump({"schema_version": 1, "exported_at": self.clock(),
                "label": self.quality_summary()["label"], "disclaimer": DISCLAIMER,
                "new_entries_paused": self.paused, "signal": self.latest_signal, "hypothetical_replay": self.latest_replay,
                "saved_signals": self.ledger().list_signals(), "manual_events": self.ledger().events(),
                "research_runs": runs, "research_runs_warning": runs_error,
                "saved_analysis": {} if runs_error else self.saved_results})
            self._commit(cancel, lambda: target.write_text(document, encoding="utf-8"))
            return target
        return self._run("export", action)

    def export_template(self, path):
        target = self._export_target(path, ".json")
        def action(cancel):
            document = _dump(sample_import_document(self.clock()))
            return self._commit(cancel, lambda: target.write_text(document, encoding="utf-8"))
        return self._run("schema-export", action)

    def export_ledger(self, path):
        target = self._export_target(path, ".csv")
        return self._run("ledger-export", lambda cancel: self._commit(cancel, lambda: self.ledger().export_csv(target)))

    def export_html(self, path):
        def action(cancel):
            from banknifty.options_report import write_report
            self._check(cancel)
            signals = self.ledger().list_signals()
            target = self._export_target(path, ".html")
            runs, runs_error = _read_research_history(self.storage_dir)
            sections = {
                "Evidence": {**self.quality_summary(), "new_entries_paused": self.paused,
                    "research_runs_warning": runs_error, "limitations": DISCLAIMER},
                "Research": format_signal(self.latest_signal) if self.latest_signal else "No current research result",
                "Hypothetical replay": _jsonable(self.latest_replay) if self.latest_replay else next(
                    (run["payload"] for run in reversed(runs) if run["payload"]["kind"] == "replay"), "No replay result"),
                "Offline pipeline and evaluation": {} if runs_error else self.saved_results,
                "Manual exposures": {s["signal_id"]: self.ledger().exposure(s["signal_id"]) for s in signals},
                "Manual audit": _without_provenance(self.ledger().events())}
            return self._commit(cancel, lambda: write_report(target, title="Bank Nifty options research", sections=sections))
        return self._run("html-export", action)


def create_options_panel(parent, *, storage_dir):
    return OptionsResearchPanel(parent, storage_dir=storage_dir)


import tkinter as tk
from tkinter import filedialog, ttk
import queue


class OptionsResearchPanel(ttk.Frame):
    def __init__(self, parent, *, storage_dir):
        super().__init__(parent, padding=8)
        self.controller = OptionsWorkspaceController(storage_dir)
        self._results = queue.Queue()
        self._worker = None
        self._correction_window = None
        self._destroyed = False
        self._poll_id = self._freshness_id = None
        self.notice = tk.StringVar(value=self.controller.restore_message)
        self.status = tk.StringVar()
        self.variables = {}
        ttk.Label(self, text="Options research & manual journal", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self, textvariable=self.status, wraplength=980).pack(anchor="w", pady=(4, 2))
        ttk.Label(self, text=DISCLAIMER, wraplength=980, font=("Segoe UI", 9)).pack(anchor="w")
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=5)
        self.action_buttons = []
        for title, action in (("Synthetic example", self.load_demo), ("Import data", self.import_data),
                              ("Schema", self.show_schema), ("Export JSON", self.export_report), ("Export HTML", self.export_html)):
            button = ttk.Button(toolbar, text=title, command=action)
            button.pack(side="left", padx=(0, 4))
            self.action_buttons.append(button)
        self.pause_button = ttk.Button(toolbar, text="Resume new entries" if self.controller.paused else "Pause new entries", command=self.toggle_pause)
        self.pause_button.pack(side="left", padx=(0, 4))
        self.action_buttons.append(self.pause_button)
        ttk.Button(toolbar, text="Cancel task", command=self.cancel).pack(side="left")
        pages = ttk.Notebook(self)
        pages.pack(fill="both", expand=True)
        research, journal = ttk.Frame(pages, padding=5), ttk.Frame(pages, padding=5)
        pages.add(research, text="Manual hypothesis & replay")
        pages.add(journal, text="Manual fills & audit")
        data_page = ttk.Frame(pages, padding=5)
        pages.add(data_page, text="Data & evaluation")
        controls = ttk.Frame(research)
        controls.pack(fill="x")
        definitions = [("strategy", "Strategy", "LONG_OPTION", ("LONG_OPTION", "DEBIT_SPREAD", "CREDIT_SPREAD")),
            ("direction", "Directional hypothesis", "NEUTRAL", ("BULLISH", "BEARISH", "NEUTRAL")),
            ("horizon_minutes", "Horizon minutes", "30", None),
            ("underlying_price", "Underlying reference", "", None),
            ("as_of", "Decision time (ISO + zone)", "", None),
            ("spread_width", "Spread width (optional)", "", None),
            ("capital", "Your capital (optional)", "", None),
            ("max_risk_fraction", "Risk fraction, e.g. .01", "", None),
            ("max_contracts", "Maximum lots (optional)", "", None),
            ("total_fees", "Estimated total fees", "", None),
            ("margin_per_lot", "Margin per lot (optional)", "", None),
            ("stop_loss_fraction", "Planned stop fraction", "", None),
            ("take_profit_fraction", "Planned target fraction", "", None),
            ("manual_latency", "Replay manual latency sec", "", None),
            ("slippage_per_leg", "Replay slippage per leg", "", None)]
        for index, (name, title, initial, values) in enumerate(definitions):
            row, column = divmod(index, 5)
            group = ttk.Frame(controls)
            group.grid(row=row, column=column, sticky="ew", padx=(0, 7), pady=(0, 3))
            controls.columnconfigure(column, weight=1)
            ttk.Label(group, text=title, font=("Segoe UI", 8)).pack(anchor="w")
            variable = tk.StringVar(value=str(self.controller.settings.get(name, initial)))
            self.variables[name] = variable
            widget = (ttk.Combobox(group, textvariable=variable, values=values, state="readonly", width=16)
                      if values else ttk.Entry(group, textvariable=variable, width=17))
            widget.pack(fill="x")
        actions = ttk.Frame(research)
        actions.pack(fill="x", pady=4)
        for title, action in (("Evaluate research", self.evaluate), ("Replay quote path", self.replay)):
            button = ttk.Button(actions, text=title, command=action)
            button.pack(side="left", padx=(0, 4))
            self.action_buttons.append(button)
        ttk.Label(actions, text="Blank risk inputs leave sizing unavailable.", font=("Segoe UI", 8)).pack(side="left", padx=4)
        self.output = tk.Text(research, height=8, wrap="word", font=("Consolas", 9), state="disabled")
        self.output.pack(fill="both", expand=True)
        self._build_journal(journal)
        self._build_analysis(data_page)
        ttk.Label(self, textvariable=self.notice, wraplength=980, font=("Segoe UI", 9)).pack(fill="x", pady=(4, 0))
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._refresh_market_fields(restore=True)
        self.refresh_status()
        self._poll_id = self.after(75, self._poll)

    def _build_journal(self, page):
        ttk.Label(page, text="Record only executions you made elsewhere, in occurrence order. Units are not lots.", wraplength=950).pack(anchor="w")
        form = ttk.Frame(page)
        form.pack(fill="x", pady=5)
        self.fill_vars = {}
        for index, (name, title) in enumerate((("signal_id", "Signal ID"), ("contract_id", "Contract ID"),
                ("side", "BUY / SELL"), ("quantity", "Units"), ("price", "Actual price"),
                ("occurred_at", "Actual time + timezone"), ("external_fill_id", "Fill reference (optional)"),
                ("fees", "Actual total fees (optional)"))):
            group = ttk.Frame(form)
            group.grid(row=index // 4, column=index % 4, sticky="ew", padx=(0, 8), pady=3)
            form.columnconfigure(index % 4, weight=1)
            ttk.Label(group, text=title, font=("Segoe UI", 8)).pack(anchor="w")
            variable = tk.StringVar(value="BUY" if name == "side" else "")
            self.fill_vars[name] = variable
            widget = (ttk.Combobox(group, textvariable=variable, values=("BUY", "SELL"), state="readonly", width=17)
                      if name == "side" else ttk.Entry(group, textvariable=variable, width=20))
            widget.pack(fill="x")
        row = ttk.Frame(page)
        row.pack(fill="x", pady=4)
        for title, action in (("Record my actual fill", self.record_fill), ("Refresh journal", self.refresh_journal),
                              ("Export audit CSV", self.export_ledger)):
            button = ttk.Button(row, text=title, command=action)
            button.pack(side="left", padx=(0, 5))
            self.action_buttons.append(button)
        correction = ttk.Frame(page)
        correction.pack(fill="x", pady=3)
        self.correction_id, self.correction_reason = tk.StringVar(), tk.StringVar()
        ttk.Label(correction, text="Fill event ID:").pack(side="left")
        ttk.Entry(correction, textvariable=self.correction_id, width=22).pack(side="left", padx=4)
        ttk.Label(correction, text="Reason:").pack(side="left")
        ttk.Entry(correction, textvariable=self.correction_reason, width=25).pack(side="left", padx=4)
        button = ttk.Button(correction, text="Void entry", command=self.correct_fill)
        button.pack(side="left")
        self.action_buttons.append(button)
        button = ttk.Button(correction, text="Correct details", command=self.edit_fill)
        button.pack(side="left", padx=4)
        self.action_buttons.append(button)
        self.journal_output = tk.Text(page, height=10, wrap="word", font=("Consolas", 9), state="disabled")
        self.journal_output.pack(fill="both", expand=True, pady=4)

    def _build_analysis(self, page):
        ttk.Label(page, text="Data-driven rules use archived index observations and actual option-book records. "
            "Manual direction/spot fields are ignored; strategy, horizon and risk settings are shared.",
            wraplength=970, font=("Segoe UI", 9)).pack(anchor="w")
        toolbar = ttk.Frame(page)
        toolbar.pack(fill="x", pady=5)
        for label, callback in (("Synthetic corpus", self.load_corpus_demo), ("Import corpus", self.import_corpus),
                ("Readiness & archive", self.diagnose_archive), ("Run data-driven", self.run_pipeline),
                ("Evaluate corpus", self.evaluate_corpus), ("Explain locally", self.explain_latest)):
            button = ttk.Button(toolbar, text=label, command=callback)
            button.pack(side="left", padx=(0, 4))
            self.action_buttons.append(button)
        form = ttk.Frame(page)
        form.pack(fill="x", pady=3)
        self.experiment_vars = {}
        for index, (name, label, initial) in enumerate((
                ("as_of", "Data-driven cutoff (ISO + zone)", ""),
                ("start_at", "Experiment start (ISO + zone)", ""),
                ("end_at", "Experiment end (ISO + zone)", ""),
                ("name", "Frozen experiment name", "Local offline experiment"),
                ("archive_mode", "History availability", "reconstructed"),
                ("decision_interval_minutes", "Decision interval minutes", "15"),
                ("min_train_sessions", "Minimum training sessions", "3"),
                ("validation_sessions", "Validation sessions", "1"),
                ("holdout_sessions", "Untouched holdout sessions", "1"))):
            group = ttk.Frame(form)
            group.grid(row=index // 3, column=index % 3, sticky="ew", padx=(0, 8), pady=2)
            form.columnconfigure(index % 3, weight=1)
            ttk.Label(group, text=label, font=("Segoe UI", 8)).pack(anchor="w")
            variable = tk.StringVar(value=initial)
            self.experiment_vars[name] = variable
            widget = (ttk.Combobox(group, textvariable=variable, values=("reconstructed", "observed"), state="readonly", width=28)
                      if name == "archive_mode" else ttk.Entry(group, textvariable=variable, width=30))
            widget.pack(fill="x")
        controls = ttk.Frame(page)
        controls.pack(fill="x", pady=4)
        ttk.Button(controls, text="Browser companion instructions", command=lambda: self._text(self.analysis_output,
            "Run Start Browser Companion.cmd in this application folder.\nThe optional local browser is read-only and binds only to this laptop.\n"
            "It shows archived results, current freshness checks and manual exposure. It is not public/mobile hosting and cannot place orders.")).pack(side="left")
        button = ttk.Button(controls, text="Corpus JSON example", command=self.export_corpus_template)
        button.pack(side="left", padx=5)
        self.action_buttons.append(button)
        ttk.Label(controls, text="Live connection: disabled. AI network requests: disabled.", font=("Segoe UI", 9)).pack(side="left", padx=10)
        self.analysis_output = tk.Text(page, height=8, wrap="word", font=("Consolas", 9), state="disabled")
        self.analysis_output.pack(fill="both", expand=True)
        saved = self.controller.saved_results
        self._text(self.analysis_output, "SAVED HISTORICAL RESULTS — current freshness must be checked again.\n" + _dump(saved) if saved else
            "Import a normalized corpus with underlying observations, dated contracts and option books.\n"
            "Or load the separately stored synthetic corpus to exercise the complete offline workflow.\n"
            "Reconstructed imports remain unverified; a trained or calibrated model is not assumed.")
        self._seen_alerts = set()

    def load_corpus_demo(self):
        def done(result):
            for key in ("start_at", "end_at"):
                self.experiment_vars[key].set(result[key])
            self.experiment_vars["as_of"].set(result["decision_at"])
            self._text(self.analysis_output, _dump(result))
            self.notice.set("Six invented sessions loaded into a separate synthetic archive. No real feed or performance validated.")
        self._start(self.controller.load_archive_demo, done)

    def import_corpus(self):
        path = filedialog.askopenfilename(parent=self, title="Import normalized market corpus",
            filetypes=(("Normalized corpus", "*.json *.csv"),))
        if not path:
            self.notice.set("Corpus import cancelled; existing archive retained.")
            return
        def done(result):
            inventory = result["inventory"]
            if inventory.get("dates"):
                self.experiment_vars["start_at"].set(inventory["dates"][0] + "T00:00:00+00:00")
                self.experiment_vars["end_at"].set(inventory["latest_received_at"])
                latest = _datetime(inventory["latest_received_at"])
                self.experiment_vars["as_of"].set(_iso(latest.replace(second=0, microsecond=0)))
            self._text(self.analysis_output, _dump(result))
            self.notice.set("Corpus import reviewed. Quarantined records cannot become research evidence; reconstructed times remain unverified.")
        self._start(lambda: self.controller.import_archive(path), done)

    def export_corpus_template(self):
        path = filedialog.asksaveasfilename(parent=self, title="Save invented corpus schema example",
            defaultextension=".json", initialfile="invented-market-schema.json", filetypes=(("JSON", "*.json"),))
        if path:
            self._start(lambda: self.controller.export_archive_template(path), lambda result:
                self.notice.set("Invented corpus schema saved. Replace records with correctly sourced data; imports remain unverified replay."))

    def diagnose_archive(self):
        def action():
            return {"diagnostics": self.controller.archive_diagnostics(), "current_status": self.controller.status_snapshot()}
        def done(result):
            self._text(self.analysis_output, _dump(result))
            alerts = result["current_status"]["alerts"]
            new = [alert for alert in alerts if alert["id"] not in self._seen_alerts]
            self._seen_alerts.update(alert["id"] for alert in alerts)
            self.notice.set(new[0]["message"] if new else "Read-only readiness refreshed. No new warning; no connection attempted.")
        self._start(action, done)

    def run_pipeline(self):
        settings = {key: value.get().strip() for key, value in self.variables.items()}
        cutoff = self.experiment_vars["as_of"].get().strip()
        mode = self.experiment_vars["archive_mode"].get()
        def done(result):
            output = f"{result.label.upper()} | {result.status}\nData-driven rule; no calibrated confidence.\n"
            if result.assessment:
                output += _dump(result.assessment) + "\n"
            output += format_signal(result.signal, direction_basis="pipeline") if result.signal else "\n".join(result.reasons)
            self._text(self.analysis_output, output)
            if result.signal:
                self.fill_vars["signal_id"].set(result.signal.signal_id)
            self.notice.set("Frozen data-driven result saved. No actual fill or order created.")
        self._start(lambda: self.controller.run_data_pipeline(settings, as_of=cutoff, archive_mode=mode), done)

    def evaluate_corpus(self):
        settings = {key: value.get().strip() for key, value in self.variables.items()}
        experiment = {key: value.get().strip() for key, value in self.experiment_vars.items()}
        self._start(lambda: self.controller.evaluate_archive(settings, experiment), lambda result:
            (self._text(self.analysis_output, _dump(result)), self.notice.set("Offline evaluation saved with held-out partitions and unavailable outcomes. Synthetic results never validate a model.")))

    def explain_latest(self):
        self._start(self.controller.explain_latest, lambda result:
            (self._text(self.analysis_output, result.text), self.notice.set("Application-owned explanation of frozen facts. No AI API request was made.")))

    @staticmethod
    def _text(widget, value):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _start(self, action, done):
        if self._worker is not None:
            self.notice.set("A task is already running. Repeated clicks do not start another.")
            return
        # Retired Tk cycles must be finalized on the Tk thread, before a new
        # worker can trigger cyclic garbage collection while parsing records.
        gc.collect()
        for button in self.action_buttons:
            button.configure(state="disabled")
        self.notice.set("Working locally. No broker connection or order request is made.")
        def work():
            try:
                self._results.put((True, action(), done))
            except Exception as error:
                self._results.put((False, error, done))
        self._worker = threading.Thread(target=work, daemon=True, name="options-research-local")
        self._worker.start()

    def _poll(self):
        if self._destroyed:
            return
        try:
            success, value, done = self._results.get_nowait()
        except queue.Empty:
            pass
        else:
            self._worker = None
            for button in self.action_buttons:
                button.configure(state="normal")
            if success:
                done(value)
            else:
                self.notice.set(f"{type(value).__name__}: {str(value)[:240]}")
            self.refresh_status(schedule=False)
        if not self._destroyed:
            self._poll_id = self.after(75, self._poll)

    def cancel(self):
        if self.controller.cancel():
            self.notice.set("Cancellation requested. Already committed audit events remain preserved.")
        else:
            self.notice.set("No cancellable task is running.")

    def toggle_pause(self):
        def done(paused):
            self.pause_button.configure(text="Resume new entries" if paused else "Pause new entries")
            self.notice.set("New entries paused. Existing exposure, exit review and actual-fill recording remain available."
                            if paused else "New research entries resumed; live trading remains unavailable.")
        self._start(lambda: self.controller.set_paused(not self.controller.paused), done)

    def _refresh_market_fields(self, *, restore=False):
        if self.controller.market:
            self.variables["as_of"].set(self.controller.settings.get("as_of") if restore and self.controller.settings.get("as_of")
                else _iso(self.controller.market.decision_at))
            self.variables["underlying_price"].set(self.controller.settings.get("underlying_price")
                if restore and self.controller.settings.get("underlying_price") else str(self.controller.market.underlying_price or ""))

    def refresh_status(self, *, schedule=True):
        quality = self.controller.quality_summary()
        paused = "NEW ENTRIES PAUSED | " if self.controller.paused else ""
        self.status.set(f"{paused}{quality['label']} | Current checks: {quality['accepted']}/{quality['total']} books. "
            + ("; ".join(quality["reasons"][:3]) if quality["reasons"] else "Live validation pending."))
        if schedule and not self._destroyed:
            self._freshness_id = self.after(1000, self.refresh_status)

    def load_demo(self):
        def done(market):
            self._refresh_market_fields()
            self.notice.set("Invented contracts and quote path loaded. Choose a directional hypothesis. No real position exists.")
            self._text(self.output, f"{market.label}\n6 invented contracts; 745 quote snapshots each.\n" + DISCLAIMER)
        self._start(self.controller.load_demo, done)

    def import_data(self):
        path = filedialog.askopenfilename(parent=self, title="Import option quotes and dated metadata",
                                         filetypes=(("Options data", "*.json *.csv"),))
        if not path:
            self.notice.set("Import cancelled; existing inputs retained.")
            return
        def done(market):
            self._refresh_market_fields()
            self.notice.set(f"Imported {len(market.quotes)} quotes. Source timestamps remain unverified; set your replay cutoff.")
            self._text(self.output, market.label + "\n" + SCHEMA_HELP)
        self._start(lambda: self.controller.import_file(path), done)

    def show_schema(self):
        self._text(self.output, SCHEMA_HELP)
        path = filedialog.asksaveasfilename(parent=self, title="Save invented JSON schema example (optional)",
                defaultextension=".json", initialfile="options-schema-example.json", filetypes=(("JSON", "*.json"),))
        if path:
            self._start(lambda: self.controller.export_template(path),
                lambda result: self.notice.set("Saved an invented schema example. Replace it with correctly sourced, dated records."))

    def evaluate(self):
        settings = {name: variable.get().strip() for name, variable in self.variables.items()}
        def done(signal):
            self._text(self.output, format_signal(signal))
            self.fill_vars["signal_id"].set(signal.signal_id)
            self.notice.set("Immutable research result saved. No order or actual fill created. Confidence unavailable.")
        self._start(lambda: self.controller.evaluate(settings), done)

    def replay(self):
        latency = self.variables["manual_latency"].get()
        fees, slippage = self.variables["total_fees"].get(), self.variables["slippage_per_leg"].get()
        self._start(lambda: self.controller.replay(manual_latency_seconds=latency, fees=fees, slippage_per_leg=slippage),
            lambda result: (self._text(self.output, format_replay(result)), self.notice.set("Hypothetical replay completed. Actual manual fills unchanged.")))

    def record_fill(self):
        values = {name: variable.get().strip() for name, variable in self.fill_vars.items()}
        self._start(lambda: self.controller.manual_fill(**values),
            lambda result: (self._text(self.journal_output, _dump(result)), self.notice.set("Reported fill recorded. Review exposure; partial spreads may be unhedged.")))

    def refresh_journal(self):
        def action():
            ledger = self.controller.ledger()
            signals = ledger.list_signals()
            return {"saved_signals": signals,
                "exposure": {item["signal_id"]: ledger.exposure(item["signal_id"]) for item in signals},
                "manual_events": ledger.events()}
        self._start(action, lambda result: (self._text(self.journal_output, _dump(result)), self.notice.set("Journal and exposure refreshed from local events.")))

    def correct_fill(self):
        identity, reason = self.correction_id.get().strip(), self.correction_reason.get().strip()
        self._start(lambda: self.controller.void_manual_fill(identity, reason),
            lambda result: (self._text(self.journal_output, _dump(result)), self.notice.set("Correction appended; original fill preserved in audit history.")))

    def edit_fill(self):
        if self._correction_window is not None and self._correction_window.winfo_exists():
            self._correction_window.lift()
            return
        identity = self.correction_id.get().strip()
        def show(fill):
            dialog = self._correction_window = tk.Toplevel(self)
            dialog.title("Correct a reported manual fill")
            dialog.transient(self.winfo_toplevel())
            frame = ttk.Frame(dialog, padding=16)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text=f"{fill['side']} {fill['quantity']} units of {fill['contract_id']}\n"
                      "Contract, side and units stay fixed; original entry is preserved.", wraplength=520).pack(anchor="w")
            values = {}
            for key, label, initial in (("price", "Corrected actual price", format(Decimal(fill["price"]), "f")),
                    ("occurred_at", "Corrected time (ISO + timezone)", fill["occurred_at"]),
                    ("fees", "Corrected total fees (blank means unknown)", format(Decimal(fill["fees"]), "f") if fill.get("fees") is not None else ""),
                    ("reason", "Reason for correction", self.correction_reason.get())):
                ttk.Label(frame, text=label).pack(anchor="w", pady=(7, 1))
                values[key] = tk.StringVar(value=initial)
                ttk.Entry(frame, textvariable=values[key], width=52).pack(fill="x")
            self.correction_dialog_vars = values
            def save():
                fields = {key: variable.get().strip() for key, variable in values.items()}
                def done(result):
                    self._text(self.journal_output, _dump(result))
                    self.notice.set("Replacement correction appended. Dependent closes retained; review actual reported P&L.")
                    if dialog.winfo_exists():
                        dialog.destroy()
                self._start(lambda: self.controller.replace_manual_fill(identity, **fields), done)
            self.correction_dialog_save = save
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=(12, 0))
            ttk.Button(row, text="Append replacement correction", command=save).pack(side="left")
            ttk.Button(row, text="Cancel", command=dialog.destroy).pack(side="left", padx=6)
        self._start(lambda: self.controller.manual_fill_for_correction(identity), show)

    def export_report(self):
        path = filedialog.asksaveasfilename(parent=self, title="Export options research", defaultextension=".json",
                                          initialfile="options-research.json", filetypes=(("JSON", "*.json"),))
        if not path:
            self.notice.set("Export cancelled; nothing changed.")
            return
        self._start(lambda: self.controller.export_report(path), lambda result: self.notice.set("Exported labelled research and manual journal evidence."))

    def export_ledger(self):
        path = filedialog.asksaveasfilename(parent=self, title="Export manual audit events", defaultextension=".csv",
                                          initialfile="options-audit.csv", filetypes=(("CSV", "*.csv"),))
        if path:
            self._start(lambda: self.controller.export_ledger(path), lambda result: self.notice.set("Exported manual audit CSV."))

    def export_html(self):
        path = filedialog.asksaveasfilename(parent=self, title="Export portable research report", defaultextension=".html",
            initialfile="options-research.html", filetypes=(("HTML", "*.html"),))
        if path:
            self._start(lambda: self.controller.export_html(path),
                lambda result: self.notice.set("Saved portable HTML report. It is a local snapshot, not a hosted live app."))

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        self._destroyed = True
        self.controller.cancel()
        for timer in (self._poll_id, self._freshness_id):
            if timer:
                try:
                    self.after_cancel(timer)
                except tk.TclError:
                    pass
        self.correction_dialog_save = None
        for name in ("variables", "fill_vars", "experiment_vars", "correction_dialog_vars"):
            values = getattr(self, name, None)
            if isinstance(values, dict):
                values.clear()
        self.notice = self.status = self.correction_id = None

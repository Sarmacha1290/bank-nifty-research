"""Portable, integrity-checked local backup of application and market data."""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED

from .backup import backup_database


# A backup can be shared later. Include reviewed project artifacts only; a
# filename/extension blacklist can miss a credential saved as an innocent name.
ROOT_FILES = frozenset({
    "PROVIDER_MAPPING.md", "PIPELINE_SPEC.md", "EVALUATION_GUIDE.md", "OPTIONAL_AI.md",
    "WORKER_GUIDE.md", "CAPTURE_GUIDE.md", "REQUIREMENTS_CHECKLIST.md", "Start Browser Companion.cmd",
    "OPTIONS_GUIDE.md", "OPTIONS_IMPORT_SCHEMA.md", "IMPLEMENTATION_REPORT.md",
    "run_tests.py", "Run Offline Tests.cmd",
    "project.json", "project-map.html", "README.md", "PROJECT_SPEC.md", "PROJECT_PARTS.md",
    "PROJECT_ROADMAP.md", "INDICATOR_STATUS.md",
    "DAILY_RANGE_SPEC.md", "AGENT_ROLES.md", "SECURITY_REVIEW.md", ".gitignore",
    "dashboard.py", "connect_groww.py", "collect_groww.py", "demo.py",
    "import_nse_weights.py", "import_institutional_flows.py", "import_market_events.py",
    "Start Bank Nifty.pyw", "Start Bank Nifty.cmd",
    "banknifty-daily-paper-range-reference.csv",
    "banknifty-daily-reference-evaluation.csv",
    "banknifty-nse-daily-ohlc.csv", "banknifty-nse-groww-range-audit.csv",
})

CODE_FILES = frozenset({
    'banknifty/rest_status.py', 'tests/test_rest_connection_status.py', 'tests/test_rest_connection_ui.py',
    'tests/test_runner_tk_cleanup.py',
    'banknifty/provider_adapters.py', 'banknifty/market_archive.py',
    'banknifty/options_pipeline.py', 'banknifty/options_evaluation.py',
    'banknifty/browser_companion.py', 'banknifty/explanation_service.py',
    'banknifty/research_worker.py',
    'banknifty/market_capture.py', 'tests/test_market_capture.py',
    'tests/test_provider_adapters.py', 'tests/test_market_archive.py',
    'tests/test_options_pipeline.py', 'tests/test_options_evaluation.py',
    'tests/test_browser_companion.py', 'tests/test_explanation_service.py',
    'tests/test_research_worker.py', 'tests/test_runner_portability.py',
    'tests/test_research_backup.py',
    'banknifty/options_data.py',
    'banknifty/options_import.py',
    'banknifty/options_ledger.py',
    'banknifty/options_replay.py',
    'banknifty/options_report.py',
    'banknifty/options_strategy.py',
    'banknifty/options_workspace.py',
    'banknifty/offline_runtime.py',
    'tests/test_options_application.py',
    'tests/test_options_backup.py',
    'tests/test_options_boundary.py',
    'tests/test_options_data.py',
    'tests/test_options_futures.py',
    'tests/test_options_import.py',
    'tests/test_options_ledger.py',
    'tests/test_options_replay.py',
    'tests/test_options_strategy.py',
    'tests/test_options_workspace.py',
    'tests/test_paper_origin.py',

    "banknifty/__init__.py", "banknifty/agents/__init__.py",
    "banknifty/agents/bull_bear.py", "banknifty/agents/index_contribution.py",
    "banknifty/agents/luna.py", "banknifty/agents/master.py",
    "banknifty/institutional_flows.py",
    "banknifty/agents/options_futures.py", "banknifty/agents/risk.py",
    "banknifty/agents/verification.py", "banknifty/alignment.py",
    "banknifty/alpha_evaluation.py", "banknifty/indicator_library.py",
    "banknifty/candle_patterns.py", "banknifty/candle_charts.py",
    "banknifty/expiry_context.py",
    "banknifty/expiry_regime_review.py",
    "banknifty/event_context.py",
    "banknifty/bank_move_review.py",
    "banknifty/backtest.py", "banknifty/backup.py", "banknifty/capture_backup.py",
    "banknifty/archive_retry.py",
    "banknifty/collect_activity.py", "banknifty/constituents.py",
    "banknifty/capture_trial_history.py",
    "banknifty/core.py", "banknifty/daily_paper.py",
    "banknifty/daily_range.py", "banknifty/daily_reference.py",
    "banknifty/depth_alignment.py", "banknifty/depth_feed.py",
    "banknifty/depth_history.py", "banknifty/depth_spikes.py",
    "banknifty/derivative_catalog.py", "banknifty/equity_feed.py",
    "banknifty/futures.py", "banknifty/futures_capture.py",
    "banknifty/futures_history.py", "banknifty/futures_review.py",
    "banknifty/equity_history.py", "banknifty/evaluation.py",
    "banknifty/feed.py", "banknifty/feed_quality.py",
    "banknifty/feed_quality_report.py", "banknifty/feed_technical.py",
    "banknifty/groww.py", "banknifty/groww_feed_security.py", "banknifty/history.py",
    "banknifty/history_audit.py", "banknifty/market_activity.py",
    "banknifty/market_activity_history.py", "banknifty/market_calendar.py",
    "banknifty/nifty50_probe.py", "banknifty/rest_sampler.py",
    "banknifty/nse_weights.py", "banknifty/official_daily.py",
    "banknifty/option_capture.py", "banknifty/option_history.py",
    "banknifty/option_snapshot_review.py",
    "banknifty/paper_log.py",
    "banknifty/paper_pipeline.py", "banknifty/parallel_capture.py",
    "banknifty/project_backup.py", "banknifty/project_map.py",
    "banknifty/readiness.py",
    "banknifty/research_row_audit.py",
    "banknifty/session_arm.py", "banknifty/session_diagnostics.py",
    "banknifty/storage.py", "banknifty/technical_features.py",
    "banknifty/tick_history.py", "banknifty/training_dataset.py",
    "banknifty/training_readiness.py", "banknifty/trial_report.py",
    "banknifty/workspace.py",
    "tests/test_alignment.py", "tests/test_backtest.py", "tests/test_backup.py",
    "tests/test_archive_retry.py",
    "tests/test_alpha_evaluation.py", "tests/test_indicator_library.py",
    "tests/test_candle_patterns.py", "tests/test_candle_charts.py",
    "tests/test_expiry_context.py",
    "tests/test_expiry_regime_review.py",
    "tests/test_event_context.py",
    "tests/test_bank_move_review.py",
    "tests/test_bull_bear.py", "tests/test_combined_connection.py",
    "tests/test_constituent_feed.py",
    "tests/test_connection_check.py",
    "tests/test_capture_trial_history.py", "tests/test_capture_backup.py",
    "tests/test_core.py", "tests/test_daily_paper.py", "tests/test_daily_range.py",
    "tests/test_daily_reference.py", "tests/test_dashboard.py",
    "tests/test_depth_alignment.py", "tests/test_depth_feed.py",
    "tests/test_evaluation.py", "tests/test_feed.py",
    "tests/test_futures.py", "tests/test_futures_capture.py",
    "tests/test_futures_review.py",
    "tests/test_feed_quality.py", "tests/test_feed_quality_report.py",
    "tests/test_groww.py", "tests/test_groww_feed_security.py",
    "tests/test_history.py", "tests/test_history_audit.py",
    "tests/test_index_contribution.py", "tests/test_luna.py",
    "tests/test_institutional_flows.py",
    "tests/test_market_activity.py", "tests/test_master.py",
    "tests/test_nifty50_probe.py", "tests/test_rest_sampler.py",
    "tests/test_nse_weights.py", "tests/test_official_daily.py",
    "tests/test_options_futures.py", "tests/test_option_capture.py",
    "tests/test_option_snapshot_review.py",
    "tests/test_paper_log.py",
    "tests/test_parallel_capture.py", "tests/test_project_backup.py",
    "tests/test_project_map.py",
    "tests/test_readiness.py", "tests/test_session_arm.py",
    "tests/test_research_row_audit.py",
    "tests/test_session_diagnostics.py", "tests/test_technical_features.py",
    "tests/test_training_dataset.py", "tests/test_training_readiness.py",
    "tests/test_verification_risk.py",
    "tests/test_workspace.py", "tests/test_trial_report.py",
})


def _approved_file(relative):
    parts = relative.parts
    if len(parts) == 1:
        return parts[0] in ROOT_FILES
    if parts[0] in ("banknifty", "tests"):
        return relative.as_posix() in CODE_FILES
    return parts in {
        ("reference", "nse-bank-weights-2026-09-30.json"),
        ("reference", "fii-dii-nse-2026-10-01-user-supplied.csv"),
        ("reference", "fii-dii-combined-2026-10-01-user-supplied.csv"),
        ("deliverables", "Bank_Nifty_Project_Architecture_2026-10-04-v4.pptx"),
        ("deliverables", "Bank_Nifty_Complete_Project_Roadmap_2026-10-04.docx"),
        ("deliverables", "Bank_Nifty_Project_Map_Preview.png"),
    }


_OPTIONS_ROOT = PurePosixPath("data/options-research")
_OPTIONS_STATE_LIMIT = 32_000
_OPTIONS_IMPORT_LIMIT = 100
_OPTIONS_LEDGER_LIMIT = 64 * 1024 * 1024
_OPTIONS_SETTINGS = frozenset({"strategy", "direction", "horizon_minutes", "underlying_price",
    "as_of", "spread_width", "capital", "max_risk_fraction", "max_contracts", "total_fees",
    "margin_per_lot", "stop_loss_fraction", "take_profit_fraction", "manual_latency", "slippage_per_leg",
    "archive_kind"})


def _hash_import_name(name):
    relative = PurePosixPath(name)
    return (relative.suffix in (".json", ".csv") and len(relative.stem) == 64
            and all(char in "0123456789abcdef" for char in relative.stem)
            and relative.name == name)


def _approved_options_file(relative):
    parts = relative.parts
    return (parts[:2] == _OPTIONS_ROOT.parts
            and ((len(parts) == 3 and parts[-1] in {"workspace-state.json", "options-ledger.sqlite3",
                "market-archive.sqlite3", "synthetic-archive.sqlite3", "analysis-results.json", "worker-state.json"})
                 or (len(parts) == 4 and parts[2] == "imports" and _hash_import_name(parts[3]))
                 or (len(parts) == 4 and parts[2] == "research-runs" and parts[3].endswith(".json")
                     and _hash_import_name(parts[3]))))


def _safe_options_path(project, relative, *, directory=False):
    """Reject symlinks/junctions in every options path component before reading."""
    current = project
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("Options backup source contains a symlink or junction")
        if current.exists() and not current.resolve().is_relative_to(project):
            raise ValueError("Options backup source escaped the project folder")
    if current.exists() and not (current.is_dir() if directory else current.is_file()):
        raise ValueError("Options backup source has an unexpected file type")
    return current


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Options state contains duplicate JSON keys")
        result[key] = value
    return result


def _options_timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Options state timestamp is invalid")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Options state timestamp needs a timezone")
    return stamp


def _validate_options_state(raw):
    if len(raw) > _OPTIONS_STATE_LIMIT:
        raise ValueError("Options workspace state exceeds its size limit")
    try:
        state = json.loads(raw, object_pairs_hook=_unique_json_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite options state")))
        common = {"version", "kind", "settings", "paused"}
        extra = {"empty": set(), "synthetic": {"anchor"},
                 "import": {"import_id", "extension", "imported_at"}}
        if (not isinstance(state, dict) or state.get("kind") not in extra
                or type(state.get("version")) is not int or state["version"] != 1
                or type(state.get("paused", False)) is not bool
                or set(state) - (common | extra[state["kind"]])
                or not {"version", "kind"}.issubset(state)
                or not extra[state["kind"]].issubset(state)):
            raise ValueError("Options workspace state schema is invalid")
        settings = state.get("settings", {})
        if not isinstance(settings, dict) or set(settings) - _OPTIONS_SETTINGS:
            raise ValueError("Options workspace contains unsupported settings")
        for name, value in settings.items():
            if value is None or value == "":
                continue
            if name == "strategy":
                if value not in {"LONG_OPTION", "DEBIT_SPREAD", "CREDIT_SPREAD"}:
                    raise ValueError("Options strategy setting is invalid")
            elif name == "direction":
                if value not in {"BULLISH", "BEARISH", "NEUTRAL"}:
                    raise ValueError("Options direction setting is invalid")
            elif name == "archive_kind":
                if value not in {"market", "synthetic"}:
                    raise ValueError("Options archive selection is invalid")
            elif name == "as_of":
                _options_timestamp(value)
            else:
                if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 100:
                    raise ValueError("Options numeric setting is invalid")
                number = Decimal(str(value))
                if not number.is_finite() or number < 0 or number > Decimal("1000000000000000"):
                    raise ValueError("Options numeric setting is outside bounds")
                if name in {"horizon_minutes", "max_contracts", "manual_latency"} and number != number.to_integral_value():
                    raise ValueError("Options integer setting is invalid")
                if name not in {"total_fees", "slippage_per_leg", "manual_latency"} and number <= 0:
                    raise ValueError("Options numeric setting must be positive")
                if name == "max_risk_fraction" and number > 1:
                    raise ValueError("Options risk fraction exceeds one")
        if state["kind"] == "synthetic":
            _options_timestamp(state["anchor"])
        elif state["kind"] == "import":
            if not _hash_import_name(str(state["import_id"]) + str(state["extension"])):
                raise ValueError("Options saved import identity is invalid")
            if not isinstance(state["import_id"], str) or len(state["import_id"]) != 64:
                raise ValueError("Options saved import identity is invalid")
            _options_timestamp(state["imported_at"])
        return state
    except (UnicodeError, json.JSONDecodeError, InvalidOperation, TypeError, KeyError, RecursionError) as error:
        raise ValueError("Options workspace state is malformed") from error


def _snapshot_options_ledger(source, snapshot):
    """Use a read-only source; validate event hashes only in the temporary copy."""
    from .options_ledger import validate_ledger
    if source.stat().st_size > _OPTIONS_LEDGER_LIMIT:
        raise ValueError("Options ledger exceeds the bounded 64 MiB backup limit")
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live:
            pages = live.execute("PRAGMA page_count").fetchone()[0]
            page_size = live.execute("PRAGMA page_size").fetchone()[0]
            if pages * page_size > _OPTIONS_LEDGER_LIMIT:
                raise ValueError("Options ledger snapshot exceeds the backup size limit")
            with closing(sqlite3.connect(snapshot)) as copied:
                live.backup(copied)
                if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Options ledger SQLite integrity check failed")
                objects = {(kind, name) for kind, name in copied.execute(
                    "SELECT type, name FROM sqlite_master")}
                expected = {("table", "options_events"), ("index", "sqlite_autoindex_options_events_1"),
                            ("trigger", "options_events_no_update"), ("trigger", "options_events_no_delete")}
                if objects != expected:
                    raise ValueError("Options ledger contains an unsupported database schema")
                if copied.execute("SELECT COUNT(*) FROM options_events").fetchone()[0] > 100_000:
                    raise ValueError("Options ledger event count exceeds the backup limit")
        return validate_ledger(snapshot)["events"]
    except sqlite3.DatabaseError as error:
        raise ValueError("Options ledger cannot be backed up") from error


def _snapshot_market_archive(source, snapshot):
    """Copy SQLite consistently, then validate every archived record read-only."""
    from .market_archive import validate_archive
    limit = 64 * 1024 * 1024
    if source.stat().st_size > limit:
        raise ValueError("Market archive exceeds the bounded 64 MiB backup limit")
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live:
            if live.execute("PRAGMA page_count").fetchone()[0] * live.execute("PRAGMA page_size").fetchone()[0] > limit:
                raise ValueError("Market archive exceeds the bounded 64 MiB backup limit")
            with closing(sqlite3.connect(snapshot)) as copied:
                live.backup(copied)
                if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Market archive SQLite integrity check failed")
        return validate_archive(snapshot)
    except sqlite3.DatabaseError as error:
        raise ValueError("Market archive cannot be backed up") from error


def _research_backup_files(project, scratch):
    """Validate only documented fixed files; never admit arbitrary sidecars."""
    from .options_workspace import read_analysis_results, read_research_runs
    files, summary = {}, {}
    root = _safe_options_path(project, _OPTIONS_ROOT, directory=True)
    for name in ("market-archive.sqlite3", "synthetic-archive.sqlite3"):
        path = _safe_options_path(project, _OPTIONS_ROOT / name)
        if path.exists():
            snapshot = Path(scratch) / name
            summary[name] = _snapshot_market_archive(path, snapshot)
            files[(_OPTIONS_ROOT / name).as_posix()] = snapshot.read_bytes()
    for name, limit in (("analysis-results.json", 4 * 1024 * 1024), ("worker-state.json", 1024 * 1024)):
        path = _safe_options_path(project, _OPTIONS_ROOT / name)
        if not path.exists():
            continue
        if path.stat().st_size > limit:
            raise ValueError("Research state exceeds the backup size limit")
        raw = path.read_bytes()
        parsed = json.loads(raw, object_pairs_hook=_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite research state")))
        if name == "analysis-results.json":
            read_analysis_results(root)
        else:
            from .research_worker import validate_worker_state
            validate_worker_state(parsed)
        if path.read_bytes() != raw:
            raise ValueError("Research state changed during backup; retry after the operation")
        files[(_OPTIONS_ROOT / name).as_posix()] = raw
        summary[name] = True
    runs = _safe_options_path(project, _OPTIONS_ROOT / "research-runs", directory=True)
    if runs.exists():
        approved = sorted(runs.glob("*.json"))
        if len(approved) > 1000:
            raise ValueError("Research runs exceed the bounded 1000-file backup limit")
        total = 0
        for path in approved:
            if not _hash_import_name(path.name):
                raise ValueError("Research run filename is invalid")
            path = _safe_options_path(project, _OPTIONS_ROOT / "research-runs" / path.name)
            size = path.stat().st_size
            total += size
            if size > 8 * 1024 * 1024 or total > 64 * 1024 * 1024:
                raise ValueError("Research runs exceed the backup size limit")
        validated = read_research_runs(root)
        # Validate the exact bytes that enter the backup, even if a run changes
        # between the initial directory validation and this copy.
        from .options_workspace import _dump
        by_id = {item["run_id"]: item for item in validated}
        for path in approved:
            raw = path.read_bytes()
            parsed = json.loads(raw, object_pairs_hook=_unique_json_object)
            if path.stem not in by_id or _dump(parsed) != _dump(by_id[path.stem]):
                raise ValueError("Research run changed during backup")
            files[(_OPTIONS_ROOT / "research-runs" / path.name).as_posix()] = raw
        summary["research_runs"] = len(approved)
    return files, summary


def _options_backup_files(project, scratch):
    """Only fixed state/ledger paths and bounded, validated hash-named imports.

    Preserve all approved imports, including prior datasets retained for saved
    signals. Arbitrary files, pending state, logs and sidecar credentials are
    excluded. Hash-shaped but malformed imports fail rather than disappearing.
    """
    from .options_import import MAX_BYTES, parse_options_bytes
    root = _safe_options_path(project, _OPTIONS_ROOT, directory=True)
    if not root.exists():
        return {}, {"state": False, "imports": 0, "ledger_events": None}
    files = {}
    summary = {"state": False, "imports": 0, "ledger_events": None}
    state_path = _safe_options_path(project, _OPTIONS_ROOT / "workspace-state.json")
    state = raw_state = None
    if state_path.exists():
        if state_path.stat().st_size > _OPTIONS_STATE_LIMIT:
            raise ValueError("Options workspace state exceeds its size limit")
        with state_path.open("rb") as stream:
            raw_state = stream.read(_OPTIONS_STATE_LIMIT + 1)
        state = _validate_options_state(raw_state)
        files[(_OPTIONS_ROOT / state_path.name).as_posix()] = raw_state
        summary["state"] = True
    ledger_path = _safe_options_path(project, _OPTIONS_ROOT / "options-ledger.sqlite3")
    if ledger_path.exists():
        snapshot = Path(scratch) / "options-ledger.sqlite3"
        summary["ledger_events"] = _snapshot_options_ledger(ledger_path, snapshot)
        files[(_OPTIONS_ROOT / ledger_path.name).as_posix()] = snapshot.read_bytes()
    imports = _safe_options_path(project, _OPTIONS_ROOT / "imports", directory=True)
    if imports.exists():
        approved = sorted((path for path in imports.iterdir() if _hash_import_name(path.name)), key=lambda p: p.name)
        if len(approved) > _OPTIONS_IMPORT_LIMIT:
            raise ValueError("Options imports exceed the bounded 100-file backup limit")
        for entry in approved:
            path = _safe_options_path(project, _OPTIONS_ROOT / "imports" / entry.name)
            if path.stat().st_size > MAX_BYTES:
                raise ValueError("Options import exceeds its 2 MiB size limit")
            with path.open("rb") as stream:
                content = stream.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES or sha256(content).hexdigest() != path.stem:
                raise ValueError("Options import filename/content hash mismatch")
            imported = parse_options_bytes(content, format=path.suffix[1:],
                                           imported_at=datetime(1970, 1, 1, tzinfo=timezone.utc))
            if imported.file_sha256 != path.stem:
                raise ValueError("Options import content integrity check failed")
            files[(_OPTIONS_ROOT / "imports" / path.name).as_posix()] = content
            summary["imports"] += 1
    if state and state["kind"] == "import":
        referenced = (_OPTIONS_ROOT / "imports" / (state["import_id"] + state["extension"])).as_posix()
        if referenced not in files:
            raise ValueError("Options workspace refers to a missing validated import")
    if raw_state is not None:
        with state_path.open("rb") as stream:
            if stream.read(_OPTIONS_STATE_LIMIT + 1) != raw_state:
                raise ValueError("Options workspace changed during backup; retry after the current operation")
    research_files, research_summary = _research_backup_files(project, scratch)
    files.update(research_files)
    summary.update(research_summary)
    return files, summary


def backup_project(project_dir, destination):
    """Save code, docs, exports and a consistent SQLite snapshot in one ZIP."""
    project, target = Path(project_dir).resolve(), Path(destination).resolve()
    if not (project / "project.json").is_file():
        raise ValueError("Expected a Bank Nifty project folder")
    if target.is_relative_to(project) or target.exists():
        raise ValueError("Choose a new backup ZIP outside the project folder")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        snapshot = Path(scratch) / "market-data.sqlite3"
        # Check every path component before opening the historical database.
        # A data-folder junction must fail before the first snapshot read.
        history = _safe_options_path(project, PurePosixPath("data/market-data.sqlite3"))
        backup_database(history, snapshot)
        files = {}
        for path in project.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(project)
            if not _approved_file(relative):
                continue
            if not path.resolve().is_relative_to(project):
                raise ValueError("Backup source escaped the project folder")
            files[relative.as_posix()] = path.read_bytes()
        files["data/market-data.sqlite3"] = snapshot.read_bytes()
        options_files, options_summary = _options_backup_files(project, scratch)
        files.update(options_files)
        manifest = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "credential_files_excluded": True,
                    "file_sha256": {name: sha256(data).hexdigest()
                                    for name, data in sorted(files.items())}}
        handle = tempfile.NamedTemporaryFile(prefix="banknifty-project-", suffix=".zip",
                                             dir=target.parent, delete=False)
        temporary = Path(handle.name)
        handle.close()
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
                for name, data in sorted(files.items()):
                    archive.writestr(name, data)
                archive.writestr("backup-manifest.json", json.dumps(manifest, indent=2))
            with ZipFile(temporary) as archive:
                if archive.testzip() is not None:
                    raise ValueError("Backup ZIP integrity check failed")
                for name, expected in manifest["file_sha256"].items():
                    if sha256(archive.read(name)).hexdigest() != expected:
                        raise ValueError(f"Backup hash mismatch: {name}")
                restored = Path(scratch) / "restored.sqlite3"
                restored.write_bytes(archive.read("data/market-data.sqlite3"))
                with closing(sqlite3.connect(restored)) as connection:
                    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Restored market database integrity check failed")
            if target.exists():
                raise ValueError("Backup destination appeared during creation")
            os.rename(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return {"files": len(files), "bytes": target.stat().st_size,
            "sha256": sha256(target.read_bytes()).hexdigest(), "options_workspace": options_summary}


def rehearse_project_restore(backup_zip, *, max_uncompressed_bytes=4_000_000_000):
    """Rebuild a backup in temporary storage, check every hash and open SQLite.

    This never changes the current project. A self-contained ZIP can establish
    internal consistency and readability, not independent source authenticity.
    """
    source = Path(backup_zip)
    if not source.is_file():
        raise ValueError("Choose an existing project backup ZIP")
    if (not isinstance(max_uncompressed_bytes, int) or
            isinstance(max_uncompressed_bytes, bool) or max_uncompressed_bytes <= 0):
        raise ValueError("Invalid restore rehearsal size limit")
    with ZipFile(source) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)) or len(names) > 1501:
            raise ValueError("Backup has duplicate or excessive entries")
        if "backup-manifest.json" not in names:
            raise ValueError("Backup manifest is missing")
        manifest_info = archive.getinfo("backup-manifest.json")
        if manifest_info.file_size > 1_000_000:
            raise ValueError("Backup manifest is too large")
        try:
            manifest = json.loads(archive.read(manifest_info))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Backup manifest is unreadable") from error
        expected = manifest.get("file_sha256") if isinstance(manifest, dict) else None
        if (not isinstance(expected, dict) or
                manifest.get("credential_files_excluded") is not True or
                not {"project.json", "data/market-data.sqlite3"}.issubset(expected)):
            raise ValueError("Backup manifest has missing required fields")
        if set(names) != set(expected) | {"backup-manifest.json"}:
            raise ValueError("Backup entries do not match the manifest")
        total = 0
        for info in infos:
            name = info.filename
            if info.is_dir() or "\\" in name or name.startswith("/"):
                raise ValueError("Backup contains an unsafe path")
            relative = PurePosixPath(name)
            if name != relative.as_posix() or any(part in ("", ".", "..") for part in relative.parts):
                raise ValueError("Backup contains an unsafe path")
            mode = stat.S_IFMT(info.external_attr >> 16)
            if mode not in (0, stat.S_IFREG):
                raise ValueError("Backup contains a non-file entry")
            if name != "backup-manifest.json":
                legacy_decks = {
                    "deliverables/Bank_Nifty_Project_Architecture_2026-10-04-v2.pptx",
                    "deliverables/Bank_Nifty_Project_Architecture_2026-10-04-v3.pptx",
                }
                if not (_approved_file(relative) or _approved_options_file(relative)
                        or name in {"data/market-data.sqlite3"} | legacy_decks):
                    raise ValueError("Backup contains an unapproved file")
                digest = expected[name]
                if (not isinstance(digest, str) or len(digest) != 64 or
                        any(char not in "0123456789abcdef" for char in digest)):
                    raise ValueError("Backup contains an invalid hash")
            total += info.file_size
            if total > max_uncompressed_bytes:
                raise ValueError("Backup exceeds the restore rehearsal size limit")
        with tempfile.TemporaryDirectory(prefix="banknifty-restore-check-") as temporary:
            restored_root = Path(temporary).resolve()
            for name in expected:
                destination = (restored_root / PurePosixPath(name)).resolve()
                if not destination.is_relative_to(restored_root):
                    raise ValueError("Backup path escaped the temporary restore folder")
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = sha256()
                with archive.open(name) as incoming, destination.open("wb") as outgoing:
                    while block := incoming.read(1024 * 1024):
                        outgoing.write(block)
                        digest.update(block)
                if digest.hexdigest() != expected[name]:
                    raise ValueError(f"Backup hash mismatch: {name}")
            try:
                project = json.loads((restored_root / "project.json").read_text(encoding="utf-8"))
                if not isinstance(project, dict) or not isinstance(project.get("version"), str):
                    raise ValueError("Restored project metadata is invalid")
                with closing(sqlite3.connect(restored_root / "data" / "market-data.sqlite3")) as connection:
                    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Restored market database failed integrity check")
                validation_dir = restored_root / "options-validation"
                validation_dir.mkdir()
                _, options_summary = _options_backup_files(restored_root, validation_dir)
            except (OSError, UnicodeError, json.JSONDecodeError, sqlite3.DatabaseError) as error:
                raise ValueError("Restored project files could not be opened") from error
    return {"files": len(expected), "uncompressed_bytes": total,
            "project_version": project["version"], "database_integrity": "ok",
            "options_workspace": options_summary}

"""One-row-per-session research coverage report, without forecasting claims."""
import csv
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile

from .alignment import audit_alignment
from .equity_history import load_equity_ticks
from .feed_quality_report import summarize_recorded_feed
from .groww import IST
from .market_calendar import regular_session
from .nse_weights import load_reference_weights
from .option_history import load_option_chain_snapshots
from .tick_history import load_index_ticks


def _session_activity_counts(connection, session, expected_symbols):
    """Count saved observations in the regular session without loading the archive."""
    start = session.open.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    end = session.close.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    placeholders = ",".join("?" for _ in expected_symbols)
    result = {"bank_quote_rows_received_during_session": 0,
              "bank_quote_sweeps_completed_during_session": 0,
              "bank_depth_updates_exchanged_during_session": 0,
              "banks_with_depth_updates": 0}
    if "bank_quotes" in tables:
        result["bank_quote_rows_received_during_session"] = connection.execute(
            f"SELECT COUNT(*) FROM bank_quotes WHERE received_at_utc >= ? AND received_at_utc < ? "
            f"AND symbol IN ({placeholders})", (start, end, *expected_symbols)).fetchone()[0]
    if "bank_quote_sweeps" in tables:
        result["bank_quote_sweeps_completed_during_session"] = connection.execute(
            "SELECT COUNT(*) FROM bank_quote_sweeps WHERE completed_at_utc >= ? "
            "AND completed_at_utc < ?", (start, end)).fetchone()[0]
    if "bank_depth_ticks" in tables:
        depth_rows, depth_banks = connection.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT symbol) FROM bank_depth_ticks "
            f"WHERE exchange_at_utc >= ? AND exchange_at_utc < ? "
            f"AND symbol IN ({placeholders})", (start, end, *expected_symbols)).fetchone()
        result["bank_depth_updates_exchanged_during_session"] = depth_rows
        result["banks_with_depth_updates"] = depth_banks
    return result


def build_session_diagnostics(database, reference_path):
    """Summarize observed dates; zero coverage never becomes invented data."""
    reference = load_reference_weights(reference_path)
    expected_symbols = ["NSE-" + row["symbol"] for row in reference["constituents"]]
    feed = summarize_recorded_feed(database)
    options = load_option_chain_snapshots(database)
    option_days = [datetime.fromisoformat(row["received_at_utc"]).astimezone(IST).date()
                   for row in options]
    result = []
    if not feed:
        return result
    with closing(sqlite3.connect(database)) as connection:
        for day_row in feed:
            session = regular_session(datetime.fromisoformat(day_row["date_ist"]).date())
            index_ticks = load_index_ticks(database, start=session.open, end=session.close)
            bank_ticks = load_equity_ticks(database, start=session.open, end=session.close,
                                           symbols=expected_symbols)
            alignment = audit_alignment(index_ticks, bank_ticks, expected_symbols)
            result.append({
                "session_date_ist": day_row["date_ist"],
                "expected_minutes": day_row["expected_minutes"],
                "observed_index_minutes": day_row["observed_minutes"],
                "accepted_sampled_index_minutes": day_row["accepted_minutes"],
                "rejected_sampled_index_minutes": day_row["rejected_minutes"],
                "missing_whole_index_minutes": day_row["missing_whole_minutes"],
                "index_raw_updates": len(index_ticks),
                "bank_raw_updates": len(bank_ticks),
                "banks_with_updates": len({tick.symbol for tick in bank_ticks}),
                "expected_banks_from_dated_reference": len(expected_symbols),
                "index_minutes_with_all_banks_available": alignment["all_banks_fresh_minutes"],
                "trial_bank_quote_max_lag_seconds": alignment["trial_max_lag_seconds"],
                "option_chain_snapshots_received_that_day": option_days.count(session.open.date()),
                **_session_activity_counts(connection, session, expected_symbols),
                "bank_list_as_of_date": reference["as_of_date"],
                "interpretation": "coverage_only_no_forecast_or_weight_validity_claim",
            })
    return result


def export_session_diagnostics_csv(database, reference_path, destination):
    source, target = Path(database), Path(destination)
    if target.resolve() == source.resolve() or (
        target.exists() and source.exists() and target.samefile(source)
    ):
        raise ValueError("Diagnostics destination must not overwrite the archive database")
    rows = build_session_diagnostics(database, reference_path)
    if not rows:
        raise ValueError("No recorded regular-session index data to summarize")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", suffix=".csv",
                                         prefix=".banknifty-session-", dir=target.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(rows)

"""Receipt-bounded diagnostics for one local index capture attempt."""

from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
import sqlite3
from statistics import median

from .feed import IndexTick
from .feed_quality import inspect_sampled_minutes
from .groww import IST
from .market_calendar import regular_market_status


def summarize_tick_timestamps(ticks, started_at, finished_at):
    """Describe stored times, with input order treated only as a rowid-order proxy.

    Receipt gaps include both window edges. Prices are deliberately unused: an
    unchanged price with advancing timestamps is not evidence of a stale feed.
    """
    for boundary in (started_at, finished_at):
        if boundary.tzinfo is None or boundary.utcoffset() is None:
            raise ValueError("Trial bounds must be timezone-aware")
    if finished_at < started_at:
        raise ValueError("Trial end precedes start")
    saved = [tick for tick in ticks if started_at <= tick.received_at <= finished_at]
    ages = sorted((tick.received_at - tick.exchange_at).total_seconds() for tick in saved)
    receipts = sorted(tick.received_at for tick in saved)
    edges = [started_at, *receipts, finished_at]
    gaps = [(right - left).total_seconds() for left, right in zip(edges, edges[1:])]
    duration = (finished_at - started_at).total_seconds()
    return {
        "saved_updates_in_receipt_window": len(saved),
        "p95_receipt_age_seconds": round(ages[ceil(0.95 * len(ages)) - 1], 3) if ages else None,
        "age_percentile_method": "nearest_rank_signed_receipt_minus_exchange",
        "receipt_age_over_5s_updates": sum(age > 5 for age in ages),
        "receipt_age_over_15s_updates": sum(age > 15 for age in ages),
        "future_exchange_timestamp_updates": sum(age < 0 for age in ages),
        "exchange_more_than_5s_ahead_updates": sum(age < -5 for age in ages),
        "start_silence_seconds": round((receipts[0] - started_at).total_seconds(), 3) if receipts else duration,
        "end_silence_seconds": round((finished_at - receipts[-1]).total_seconds(), 3) if receipts else duration,
        "max_receipt_gap_seconds": round(max(gaps), 3),
        "warning_gaps_over_5s": sum(gap > 5 for gap in gaps),
        "stale_gaps_over_15s": sum(gap > 15 for gap in gaps),
        "receipt_clock_regressions": sum(right.received_at < left.received_at
                                         for left, right in zip(saved, saved[1:])),
        "out_of_order_exchange_updates": sum(right.exchange_at < left.exchange_at
                                             for left, right in zip(saved, saved[1:])),
        "sequence_basis": "SQLite rowid order proxy, not independently recorded arrival order",
        "limitations": (
            "Receipt times use the laptop wall clock; no monotonic receipts were archived.",
            "Duplicate exchange timestamps were not saved; these are gaps between saved unique rows, not all feed messages.",
            "Rowid order cannot prove network arrival order or recover observations outside the receipt window.",
            "Stored timestamps do not independently authenticate provider freshness or market truth.",
        ),
    }

def summarize_index_trial(database, started_at, finished_at, *, symbol="NSE-BANKNIFTY"):
    """Describe saved updates received during this attempt, never an inferred feed stream.

    Returns None when there are no saved updates in the receipt window. Duplicate
    feed messages are excluded because the raw archive keeps one row per exchange
    timestamp; the collector's separate duplicate counter remains authoritative.
    """
    for boundary in (started_at, finished_at):
        if boundary.tzinfo is None or boundary.utcoffset() is None:
            raise ValueError("Trial bounds must be timezone-aware")
    if finished_at < started_at:
        raise ValueError("Trial end precedes start")
    path = Path(database)
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_ticks'"
        ).fetchone() is None:
            return None
        rows = connection.execute(
            "SELECT symbol, exchange_at_utc, received_at_utc, value, source "
            "FROM index_ticks WHERE symbol=? AND received_at_utc>=? AND received_at_utc<=? "
            "ORDER BY rowid",
            (symbol, started_at.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
             finished_at.astimezone(timezone.utc).isoformat(timespec="milliseconds")),
        ).fetchall()
    if not rows:
        return None
    ticks = [IndexTick(row[0], datetime.fromisoformat(row[1]),
                       datetime.fromisoformat(row[2]), row[3], row[4]) for row in rows]
    timestamp_diagnostics = summarize_tick_timestamps(ticks, started_at, finished_at)
    ticks.sort(key=lambda tick: tick.exchange_at)
    # A late backlog can be received during this attempt while describing an
    # earlier market minute. Keep it in the saved-update and lag diagnostics,
    # but do not let it satisfy the current trial's quality/completion gate.
    current_exchange_ticks = [tick for tick in ticks
                              if started_at <= tick.exchange_at <= finished_at]
    quality, _ = inspect_sampled_minutes(current_exchange_ticks)
    statuses = Counter(row.status for row in quality)
    # A capture begun or stopped mid-minute cannot claim full-window coverage
    # for those edge minutes, even if their available samples pass the rule.
    minute = started_at.astimezone(IST).replace(second=0, microsecond=0)
    if minute < started_at:
        minute += timedelta(minutes=1)
    complete_starts = set()
    while minute + timedelta(minutes=1) <= finished_at:
        if regular_market_status(minute) == "regular_session":
            complete_starts.add(minute)
        minute += timedelta(minutes=1)
    full_quality = [row for row in quality if row.start in complete_starts]
    lags = [(tick.received_at - tick.exchange_at).total_seconds() for tick in ticks]
    return {
        "saved_updates": len(ticks),
        "timestamp_diagnostics": timestamp_diagnostics,
        "first_exchange_ist": ticks[0].exchange_at.astimezone(IST).isoformat(),
        "last_exchange_ist": ticks[-1].exchange_at.astimezone(IST).isoformat(),
        "median_receipt_lag_seconds": round(median(lags), 3),
        "max_receipt_lag_seconds": round(max(lags), 3),
        "exchange_time_ahead_updates": sum(lag < 0 for lag in lags),
        "observed_minutes": len(quality),
        "accepted_minutes": statuses["accepted"],
        "full_minutes": len(complete_starts),
        "full_observed_minutes": len(full_quality),
        "full_accepted_minutes": sum(row.status == "accepted" for row in full_quality),
        "too_few_samples": statuses["too_few_samples"],
        "missing_minute_edge": statuses["missing_minute_edge"],
        "internal_gap": statuses["internal_gap"],
    }


def format_index_trial_note(report):
    if report is None:
        return " Current trial: no new index updates were saved. "
    full_note = (f"complete recording-window minutes {report['full_accepted_minutes']}/"
                 f"{report['full_minutes']} accepted "
                 f"({report['full_observed_minutes']} observed); "
                 if all(key in report for key in
                        ("full_minutes", "full_observed_minutes", "full_accepted_minutes"))
                 else "")
    return (
        f" Current trial: {report['saved_updates']} unique updates; "
        f"exchange {report['first_exchange_ist']} to {report['last_exchange_ist']}; "
        f"receipt lag median {report['median_receipt_lag_seconds']}s, "
        f"maximum {report['max_receipt_lag_seconds']}s"
        f" ({report['exchange_time_ahead_updates']} exchange times ahead of receipt). "
        f"Observed minutes {report['observed_minutes']}, accepted {report['accepted_minutes']}; "
        + full_note +
        f"rejected: sparse {report['too_few_samples']}, "
        f"edge {report['missing_minute_edge']}, gap {report['internal_gap']}. "
        "Minute quality counts only exchange updates within this recording window. "
        "First and last trial minutes may be partial. "
    )

"""Excel-readable coverage map for locally observed Groww index updates."""

import csv
from datetime import timedelta
import os
from pathlib import Path
import tempfile

from .feed_quality import inspect_sampled_minutes
from .groww import IST
from .market_calendar import regular_session
from .tick_history import load_index_ticks


RULE = "at least 12 distinct exchange times; both edges within 10s; max internal gap 15s"


def summarize_recorded_feed(database):
    """Return per-date coverage against reviewed regular-session minute slots."""
    quality, _ = inspect_sampled_minutes(load_index_ticks(database))
    dates = sorted({row.start.astimezone(IST).date() for row in quality})
    result = []
    for day in dates:
        session = regular_session(day)
        expected = int((session.close - session.open).total_seconds() // 60)
        rows = [row for row in quality if row.start.astimezone(IST).date() == day]
        accepted = sum(row.status == "accepted" for row in rows)
        result.append({
            "date_ist": day.isoformat(),
            "expected_minutes": expected,
            "observed_minutes": len(rows),
            "accepted_minutes": accepted,
            "rejected_minutes": len(rows) - accepted,
            "missing_whole_minutes": expected - len(rows),
            "first_observed_start_ist": min(row.start for row in rows).astimezone(IST).isoformat(),
            "last_observed_end_ist": max(row.end for row in rows).astimezone(IST).isoformat(),
        })
    return result


def export_feed_quality_csv(database, destination):
    """Map every expected regular-session minute on observed dates.

    Missing rows are calendar expectations, not invented price observations.
    This report does not turn sampled updates into official OHLC candles.
    """
    source, target = Path(database), Path(destination)
    if source.resolve() == target.resolve() or (
        source.exists() and target.exists() and source.samefile(target)
    ):
        raise ValueError("Quality report destination must not overwrite the market database")
    ticks = load_index_ticks(source)
    quality, _ = inspect_sampled_minutes(ticks)
    if not quality:
        raise ValueError("No regular-session Groww index updates are saved")
    observed = {row.start.astimezone(IST): row for row in quality}
    dates = sorted({start.date() for start in observed})
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    count = 0
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", suffix=".csv",
                                         prefix=".banknifty-quality-", dir=target.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream)
            writer.writerow(("source", "session_date_ist", "expected_minute_start_ist",
                             "expected_minute_end_ist", "coverage_status", "sample_count",
                             "first_sample_offset_seconds", "last_sample_offset_seconds",
                             "largest_internal_gap_seconds", "quality_rule", "interpretation_note"))
            for day in dates:
                session = regular_session(day)
                minute = session.open
                while minute < session.close:
                    end = minute + timedelta(minutes=1)
                    row = observed.get(minute)
                    writer.writerow((
                        "Groww index feed", day.isoformat(), minute.isoformat(), end.isoformat(),
                        row.status if row else "missing_whole_minute",
                        row.sample_count if row else 0,
                        row.first_offset_seconds if row else "",
                        row.last_offset_seconds if row else "",
                        row.largest_gap_seconds if row else "",
                        RULE,
                        "Observed updates only; missing means no saved update, not proof the market did not trade",
                    ))
                    count += 1
                    minute = end
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"rows": count, "session_dates": len(dates), "observed_minutes": len(quality),
            "accepted_minutes": sum(row.status == "accepted" for row in quality)}

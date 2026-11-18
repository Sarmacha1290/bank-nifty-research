"""Offline Bank Nifty daily-range labels from complete sampled-feed sessions.

Observed sampled highs/lows are not official exchange OHLC. This module
creates historical evaluation labels only; it never predicts a future range.
"""
import csv
from datetime import datetime, timedelta
from decimal import Decimal
import os
from pathlib import Path
import tempfile

from .core import Session, validate_series
from .feed_quality import SAMPLED_SOURCE, inspect_sampled_minutes
from .groww import IST
from .market_calendar import regular_session
from .official_daily import load_official_days
from .tick_history import load_index_ticks


LABEL_VERSION = "sampled-daily-range-v0.1"
SYMBOL = "NSE-BANKNIFTY"


def label_complete_day(candles, session, *, as_of=None):
    """Label a fully recorded day; optional cutoff also labels its future portion.

    `as_of` is an evaluation origin, not a prediction time used by this
    function to hide later candles. Callers must keep these labels offline.
    """
    if not isinstance(session, Session):
        raise TypeError("A regular session is required")
    expected = int((session.close - session.open).total_seconds() // 60)
    rows = validate_series(list(candles), session.close)
    if (len(rows) != expected or not rows or rows[0].start != session.open
            or rows[-1].end != session.close
            or any(row.symbol != SYMBOL or row.kind != "index" or
                   row.source != SAMPLED_SOURCE for row in rows)):
        raise ValueError("A complete same-source sampled Bank Nifty session is required")
    opening = rows[0].open
    day_high, day_low = max(row.high for row in rows), min(row.low for row in rows)
    result = {
        "session_date_ist": session.open.astimezone(IST).date().isoformat(),
        "source": SAMPLED_SOURCE,
        "label_version": LABEL_VERSION,
        "sampled_open": opening,
        "sampled_close": rows[-1].close,
        "sampled_day_high": day_high,
        "sampled_day_low": day_low,
        "sampled_range_points": round(day_high - day_low, 4),
        "sampled_range_pct_of_open": round(100 * (day_high - day_low) / opening, 6),
        "cutoff_ist": None,
        "sampled_remaining_high": None,
        "sampled_remaining_low": None,
        "sampled_remaining_range_points": None,
        "interpretation": "historical_sampled_label_not_official_ohlc_or_prediction",
    }
    if as_of is not None:
        if (not isinstance(as_of, datetime) or as_of.tzinfo is None or
                as_of.utcoffset() is None or
                not session.open + timedelta(minutes=1) <= as_of < session.close or
                (as_of - session.open).total_seconds() % 60 != 0):
            raise ValueError("Cutoff must be a completed minute inside this session")
        future = [row for row in rows if row.start >= as_of]
        if not future or future[0].start != as_of:
            raise ValueError("No exact future interval at cutoff")
        high, low = max(row.high for row in future), min(row.low for row in future)
        result.update(cutoff_ist=as_of.astimezone(IST).isoformat(),
                      sampled_remaining_high=high,
                      sampled_remaining_low=low,
                      sampled_remaining_range_points=round(high - low, 4))
    return result


def build_daily_range_history(database):
    """One coverage row per observed session; partial days have no range label."""
    quality, accepted = inspect_sampled_minutes(load_index_ticks(database))
    dates = sorted({row.start.astimezone(IST).date() for row in quality})
    result = []
    for day in dates:
        session = regular_session(day)
        observed = [row for row in quality if session.open <= row.start < session.close]
        bars = [row for row in accepted if session.open <= row.start < session.close]
        expected = int((session.close - session.open).total_seconds() // 60)
        record = {
            "session_date_ist": day.isoformat(),
            "status": "insufficient_coverage",
            "expected_minutes": expected,
            "observed_minutes": len(observed),
            "accepted_minutes": len(bars),
            "missing_or_rejected_minutes": expected - len(bars),
            "source": SAMPLED_SOURCE,
            "label_version": LABEL_VERSION,
            "sampled_open": None,
            "sampled_close": None,
            "sampled_day_high": None,
            "sampled_day_low": None,
            "sampled_range_points": None,
            "sampled_range_pct_of_open": None,
            "interpretation": "no_daily_label_until_every_regular_session_minute_is_accepted",
        }
        if len(observed) == expected and len(bars) == expected:
            try:
                label = label_complete_day(bars, session)
            except ValueError:
                record["status"] = "invalid_continuity_or_provenance"
            else:
                for field in ("sampled_open", "sampled_close", "sampled_day_high",
                              "sampled_day_low", "sampled_range_points",
                              "sampled_range_pct_of_open", "interpretation"):
                    record[field] = label[field]
                record["status"] = "complete_sampled_session"
        result.append(record)
    return result


def export_daily_range_csv(database, destination):
    source, target = Path(database), Path(destination)
    if target.resolve() == source.resolve() or (
            source.exists() and target.exists() and source.samefile(target)):
        raise ValueError("Daily range export cannot overwrite the market database")
    rows = build_daily_range_history(database)
    if not rows:
        raise ValueError("No recorded regular-session index updates to summarize")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="",
                                         suffix=".csv", prefix=".banknifty-range-",
                                         dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(rows)


def compare_official_and_sampled(database):
    """Audit sampled extremes against saved official OHLC on matching dates."""
    sampled = {row["session_date_ist"]: row for row in build_daily_range_history(database)}
    result = []
    for bar in load_official_days(database):
        row = sampled.get(bar.day.isoformat())
        comparison = {
            "session_date_ist": bar.day.isoformat(),
            "status": "no_sampled_session" if row is None else
                      "sampled_incomplete" if row["status"] != "complete_sampled_session" else
                      "pending_comparison",
            "official_high": str(bar.high), "official_low": str(bar.low),
            "official_range_points": str(bar.range_points),
            "sampled_high": None, "sampled_low": None,
            "sampled_range_points": None,
            "official_minus_sampled_high_points": None,
            "sampled_minus_official_low_points": None,
            "official_minus_sampled_range_points": None,
        }
        if row is not None and row["status"] == "complete_sampled_session":
            sample_high = Decimal(str(row["sampled_day_high"]))
            sample_low = Decimal(str(row["sampled_day_low"]))
            high_gap = bar.high - sample_high
            low_gap = sample_low - bar.low
            range_gap = bar.range_points - (sample_high - sample_low)
            comparison.update(
                status=("sampled_outside_official_range"
                        if high_gap < Decimal("-0.01") or low_gap < Decimal("-0.01")
                        else "sampled_within_official_range"),
                sampled_high=str(sample_high), sampled_low=str(sample_low),
                sampled_range_points=str(sample_high - sample_low),
                official_minus_sampled_high_points=str(high_gap),
                sampled_minus_official_low_points=str(low_gap),
                official_minus_sampled_range_points=str(range_gap))
        result.append(comparison)
    return result


def export_range_comparison_csv(database, destination):
    """Export an audit row for every official day, including missing overlaps."""
    source, target = Path(database), Path(destination)
    if target.resolve() == source.resolve() or (
            source.exists() and target.exists() and source.samefile(target)):
        raise ValueError("Range comparison export cannot overwrite the market database")
    rows = compare_official_and_sampled(database)
    if not rows:
        raise ValueError("No official daily days to compare")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="",
                                         suffix=".csv", prefix=".banknifty-range-audit-",
                                         dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(rows)

"""Exploratory past-only daily-range reference, never a live forecast."""
import csv
from datetime import timedelta
from decimal import Decimal
import os
from pathlib import Path
import tempfile

from .market_calendar import regular_session, regular_session_date
from .official_daily import load_official_days


LOOKBACK = 40
LOWER_RANK = 0.10
UPPER_RANK = 0.90
REFERENCE_VERSION = "prior-close-rolling-40-q10-q90-v0.1"


def _previous_regular_day(day):
    cursor = day - timedelta(days=1)
    while cursor.year == 2026:
        if regular_session_date(cursor)[0]:
            return cursor
        cursor -= timedelta(days=1)
    return None


def _empirical_rank(values, fraction):
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * fraction)]


def evaluate_daily_reference(bars, lookback=LOOKBACK):
    """Evaluate frozen reference on consecutive days; no current-day input is used."""
    if lookback != LOOKBACK:
        raise ValueError("This reference is frozen at a 40-session lookback")
    rows = list(bars)
    if any(rows[i].day >= rows[i + 1].day for i in range(len(rows) - 1)):
        raise ValueError("Official daily bars must be unique and chronological")
    results = []
    for index in range(lookback + 1, len(rows)):
        # Each training observation needs its own known previous close. Gaps
        # invalidate the whole window instead of silently bridging missing days.
        if any(_previous_regular_day(rows[j].day) != rows[j - 1].day
               for j in range(index - lookback, index + 1)):
            continue
        training = rows[index - lookback:index]
        high_offsets = [bar.high - rows[j - 1].close
                        for j, bar in enumerate(rows[index - lookback:index],
                                                start=index - lookback)]
        low_offsets = [bar.low - rows[j - 1].close
                       for j, bar in enumerate(rows[index - lookback:index],
                                               start=index - lookback)]
        anchor = rows[index - 1].close
        lower = anchor + _empirical_rank(low_offsets, LOWER_RANK)
        upper = anchor + _empirical_rank(high_offsets, UPPER_RANK)
        actual = rows[index]
        target_open = regular_session(actual.day).open
        latest_input_retrieval = max(bar.retrieved_at_utc for bar in
                                     rows[index - lookback - 1:index])
        archived_before_target_open = latest_input_retrieval < target_open
        results.append({
            "session_date_ist": actual.day.isoformat(),
            "reference_version": REFERENCE_VERSION,
            "origin": "retrospective_before_session_cutoff_using_prior_official_close",
            "source_availability": ("archived_before_target_open" if archived_before_target_open
                                    else "reconstructed_from_later_retrieval"),
            "latest_input_retrieved_at_utc": latest_input_retrieval.isoformat(),
            "training_first_date": training[0].day.isoformat(),
            "training_last_date": training[-1].day.isoformat(),
            "training_days": lookback,
            "previous_close": str(anchor),
            "reference_lower": str(lower),
            "reference_upper": str(upper),
            "official_low": str(actual.low),
            "official_high": str(actual.high),
            "both_extremes_contained": lower <= actual.low and actual.high <= upper,
            "lower_absolute_error_points": str(abs(lower - actual.low)),
            "upper_absolute_error_points": str(abs(upper - actual.high)),
            "reference_width_points": str(upper - lower),
            "official_range_points": str(actual.range_points),
            "interpretation": "exploratory_historical_reference_not_live_or_calibrated_forecast",
        })
    return results


def summarize_daily_reference(rows):
    records = list(rows)
    if not records:
        return {"evaluated_days": 0, "both_extremes_contained_pct": None,
                "archived_before_target_open_days": 0,
                "reconstructed_from_later_retrieval_days": 0,
                "mean_lower_absolute_error_points": None,
                "mean_upper_absolute_error_points": None,
                "mean_reference_width_points": None,
                "mean_official_range_points": None}
    size = Decimal(len(records))
    mean = lambda key: sum(Decimal(row[key]) for row in records) / size
    return {
        "evaluated_days": len(records),
        "archived_before_target_open_days": sum(
            row["source_availability"] == "archived_before_target_open" for row in records),
        "reconstructed_from_later_retrieval_days": sum(
            row["source_availability"] == "reconstructed_from_later_retrieval"
            for row in records),
        "both_extremes_contained_pct":
            100 * Decimal(sum(row["both_extremes_contained"] for row in records)) / size,
        "mean_lower_absolute_error_points": mean("lower_absolute_error_points"),
        "mean_upper_absolute_error_points": mean("upper_absolute_error_points"),
        "mean_reference_width_points": mean("reference_width_points"),
        "mean_official_range_points": mean("official_range_points"),
    }


def export_daily_reference_csv(database, destination):
    source, target = Path(database), Path(destination)
    if target.resolve() == source.resolve() or (
            source.exists() and target.exists() and source.samefile(target)):
        raise ValueError("Reference export cannot overwrite the market database")
    rows = evaluate_daily_reference(load_official_days(database))
    if not rows:
        raise ValueError("Not enough contiguous official sessions for evaluation")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="",
                                         suffix=".csv", prefix=".banknifty-daily-reference-",
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

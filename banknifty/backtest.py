"""Conservative, offline rule comparison on recorded sampled index minutes.

This is an evaluation harness, not a fitted model or a live forecast.
Splits contain whole sessions only. Origins are 60 minutes apart, and all inputs at
an origin end there; later near-end sampled prices are used only for labels.
"""

from collections import defaultdict
import csv
from datetime import timedelta, timezone
from math import ceil
from math import sqrt
import os
from pathlib import Path
from statistics import NormalDist
import tempfile

from .agents.luna import HORIZONS, TECHNICAL_FORMULA_VERSION, assess
from .core import label_returns
from .feed import IndexTick
from .feed_quality import (MAX_RESEARCH_ENDPOINT_AGE_SECONDS, SAMPLED_SOURCE,
                           accepted_close_age_by_end, inspect_sampled_minutes)
from .groww import IST
from .market_calendar import regular_session
from .technical_features import build_technical_snapshot
from .tick_history import load_index_ticks


def _direction(value):
    return "up" if value > 0 else "down" if value < 0 else "flat"


def wilson_reference_interval(hits, calls):
    """Two-sided 95% Wilson interval, descriptive only for these market calls.

    Formula: NIST/SEMATECH e-Handbook, section 7.2.4.1. Its independent
    binomial-trial assumption is not established for time-series outcomes;
    these bounds are not a model-release criterion or a paired benchmark test.
    """
    if (isinstance(hits, bool) or isinstance(calls, bool) or
            not isinstance(hits, int) or not isinstance(calls, int) or
            calls < 0 or not 0 <= hits <= calls):
        raise ValueError("Hit and call counts must be nonnegative integers with hits <= calls")
    if calls == 0:
        return None
    z = NormalDist().inv_cdf(0.975)
    proportion = hits / calls
    denominator = 1 + z * z / calls
    centre = (proportion + z * z / (2 * calls)) / denominator
    radius = z * sqrt(proportion * (1 - proportion) / calls +
                      z * z / (4 * calls * calls)) / denominator
    return max(0.0, centre - radius), min(1.0, centre + radius)


def _split_dates(dates):
    """Chronological 60/20/20 by whole session date, requiring three groups."""
    count = len(dates)
    if count < 5:
        return {}
    first = max(1, min(count - 2, ceil(count * 0.6)))
    second = max(first + 1, min(count - 1, ceil(count * 0.8)))
    return {date: ("development" if i < first else "validation" if i < second else "holdout")
            for i, date in enumerate(dates)}


def evaluate_recorded_ticks(ticks):
    """Evaluate fixed rules and two simple baselines without crossing sessions.

    Five dates with a 61-minute input window are required before splits appear. Outcomes
    from every date stay in that date's partition. This harness does not train,
    tune, or calibrate the Technical rule score.
    """
    timely = []
    excluded_late_or_clock_invalid = 0
    for tick in ticks:
        if (not isinstance(tick, IndexTick) or tick.exchange_at.tzinfo is None or
                tick.received_at.tzinfo is None):
            raise ValueError("Comparison requires timezone-aware index updates")
        local = tick.exchange_at.astimezone(IST)
        minute_end = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
        if (tick.received_at < tick.exchange_at or
                tick.received_at > minute_end + timedelta(seconds=5)):
            excluded_late_or_clock_invalid += 1
            continue
        timely.append(tick)
    quality, accepted = inspect_sampled_minutes(timely)
    close_ages = accepted_close_age_by_end(quality)
    if any(c.symbol != "NSE-BANKNIFTY" or c.source != SAMPLED_SOURCE for c in accepted):
        raise ValueError("Evaluation requires recorded Bank Nifty sampled minutes")
    by_date = defaultdict(list)
    for candle in accepted:
        by_date[candle.end.date()].append(candle)
    observed_dates = sorted(by_date)
    origins_by_date = {}
    for day in observed_dates:
        candles = sorted(by_date[day], key=lambda item: item.end)
        session = regular_session(day)
        by_end = {item.end.astimezone(timezone.utc): item for item in candles}
        selected = []
        last_origin = None
        for origin_candle in candles:
            origin = origin_candle.end
            if last_origin is not None and origin - last_origin < timedelta(minutes=60):
                continue
            history = [by_end.get((origin - timedelta(minutes=i)).astimezone(timezone.utc))
                       for i in range(60, -1, -1)]
            if any(item is None for item in history):
                continue
            if close_ages.get(origin.astimezone(timezone.utc), float("inf")) > MAX_RESEARCH_ENDPOINT_AGE_SECONDS:
                continue
            labels = label_returns(candles, origin, session, HORIZONS)
            # Select without looking at future label availability or direction.
            selected.append((origin, history, labels))
            last_origin = origin
        if selected:
            origins_by_date[day] = selected
    dates = sorted(origins_by_date)
    assignments = _split_dates(dates)
    report = {
        "source": SAMPLED_SOURCE,
        "data_status": "recorded_sampled_index_updates; receipts by minute-end plus 5 seconds; near-end prices within 5 seconds; not official exchange OHLC",
        "sampled_minutes_accepted": len(accepted),
        "sampled_minutes_rejected": sum(row.status != "accepted" for row in quality),
        "excluded_late_or_clock_invalid_ticks": excluded_late_or_clock_invalid,
        "observed_session_dates": len(observed_dates),
        "analysis_ready_session_dates": len(dates),
        "minimum_sessions_for_split": 5,
        "split_status": "ready" if assignments else "insufficient_sessions",
        "partitions": {},
    }
    if not assignments:
        return report
    new_stats = lambda: {"eligible_origins": 0, "near_end_labels": 0,
                                 "technical_calls": 0, "technical_hits": 0,
                                 "momentum_calls": 0, "momentum_hits": 0,
                                 "always_up_calls": 0, "always_up_hits": 0,
                                 "technical_vs_always_up_matched_calls": 0,
                                 "technical_vs_always_up_technical_hits": 0,
                                 "technical_vs_always_up_baseline_hits": 0,
                                 "technical_vs_momentum_matched_calls": 0,
                                 "technical_vs_momentum_technical_hits": 0,
                                 "technical_vs_momentum_baseline_hits": 0}
    stats = defaultdict(new_stats)
    daily_stats = defaultdict(new_stats)
    for day in dates:
        for origin, history, labels in origins_by_date[day]:
            decision_at = origin + timedelta(seconds=5)
            snapshot = build_technical_snapshot(history, origin, generated_at=decision_at)
            for label in labels:
                horizon = label["horizon_minutes"]
                if label["status"] not in ("ready", "missing_data"):
                    continue
                bucket = stats[(assignments[day], horizon)]
                day_bucket = daily_stats[(assignments[day], horizon, day)]
                for counted in (bucket, day_bucket):
                    counted["eligible_origins"] += 1
                if label["status"] != "ready":
                    continue
                target = origin + timedelta(minutes=horizon)
                if close_ages.get(target.astimezone(timezone.utc), float("inf")) > MAX_RESEARCH_ENDPOINT_AGE_SECONDS:
                    continue
                for counted in (bucket, day_bucket):
                    counted["near_end_labels"] += 1
                actual = _direction(label["return_pct"])
                candidate = assess(snapshot, horizon, generated_at=decision_at)
                technical_hit = None
                if candidate.bias in ("bullish", "bearish"):
                    technical_hit = int((candidate.bias == "bullish" and actual == "up") or
                                        (candidate.bias == "bearish" and actual == "down"))
                    for counted in (bucket, day_bucket):
                        counted["technical_calls"] += 1
                        counted["technical_hits"] += technical_hit
                        counted["technical_vs_always_up_matched_calls"] += 1
                        counted["technical_vs_always_up_technical_hits"] += technical_hit
                        counted["technical_vs_always_up_baseline_hits"] += int(actual == "up")
                prior_return = getattr(snapshot, f"return_{horizon}m_pct")
                if prior_return != 0:
                    hit = int(_direction(prior_return) == actual)
                    for counted in (bucket, day_bucket):
                        counted["momentum_calls"] += 1
                        counted["momentum_hits"] += hit
                        if technical_hit is not None:
                            counted["technical_vs_momentum_matched_calls"] += 1
                            counted["technical_vs_momentum_technical_hits"] += technical_hit
                            counted["technical_vs_momentum_baseline_hits"] += hit
                for counted in (bucket, day_bucket):
                    counted["always_up_calls"] += 1
                    counted["always_up_hits"] += int(actual == "up")
    for partition in ("development", "validation", "holdout"):
        partition_dates = [d.isoformat() for d in dates if assignments[d] == partition]
        horizons = {}
        for horizon in HORIZONS:
            row = dict(stats[(partition, horizon)])
            row["label_coverage"] = (row["near_end_labels"] / row["eligible_origins"]
                                     if row["eligible_origins"] else None)
            for name in ("technical", "momentum", "always_up"):
                calls = row[f"{name}_calls"]
                row[f"{name}_hit_rate"] = row[f"{name}_hits"] / calls if calls else None
                interval = wilson_reference_interval(row[f"{name}_hits"], calls)
                row[f"{name}_wilson95_lower"] = interval[0] if interval else None
                row[f"{name}_wilson95_upper"] = interval[1] if interval else None
                # Treat the market day as the independent reporting unit.
                # This is descriptive, not a confidence interval or skill test.
                day_rates = [daily_stats[(partition, horizon, day)][f"{name}_hits"] /
                             daily_stats[(partition, horizon, day)][f"{name}_calls"]
                             for day in dates if assignments[day] == partition and
                             daily_stats[(partition, horizon, day)][f"{name}_calls"]]
                row[f"{name}_days_with_calls"] = len(day_rates)
                row[f"{name}_mean_day_hit_rate"] = (
                    sum(day_rates) / len(day_rates) if day_rates else None)
                row[f"{name}_min_day_hit_rate"] = min(day_rates) if day_rates else None
                row[f"{name}_max_day_hit_rate"] = max(day_rates) if day_rates else None
            for baseline in ("always_up", "momentum"):
                prefix = f"technical_vs_{baseline}"
                calls = row[f"{prefix}_matched_calls"]
                row[f"{prefix}_matched_technical_hit_rate"] = (
                    row[f"{prefix}_technical_hits"] / calls if calls else None)
                row[f"{prefix}_matched_baseline_hit_rate"] = (
                    row[f"{prefix}_baseline_hits"] / calls if calls else None)
                row[f"{prefix}_matched_difference"] = (
                    (row[f"{prefix}_technical_hits"] - row[f"{prefix}_baseline_hits"]) / calls
                    if calls else None)
                row[f"{prefix}_days_with_matched_calls"] = sum(
                    daily_stats[(partition, horizon, day)][f"{prefix}_matched_calls"] > 0
                    for day in dates if assignments[day] == partition)
            horizons[str(horizon)] = row
        report["partitions"][partition] = {"dates": partition_dates, "horizons": horizons}
    return report


def evaluate_recorded_database(database):
    return evaluate_recorded_ticks(load_index_ticks(database))


def export_recorded_comparison_csv(database, destination, *, report=None):
    """Write a provenance-labelled, Excel-readable split summary atomically.

    A report is unavailable until five analysis-ready dates exist. Exporting
    never mutates the source archive, including if a user selects its path.
    """
    source, target = Path(database), Path(destination)
    if source.resolve() == target.resolve() or (
        source.exists() and target.exists() and source.samefile(target)
    ):
        raise ValueError("Report destination must not overwrite the market database")
    report = evaluate_recorded_database(source) if report is None else report
    if report["split_status"] != "ready":
        raise ValueError("At least five analysis-ready session dates are required")
    header = (
        "source", "data_status", "formula_version", "split_method", "partition",
        "first_date_ist", "last_date_ist", "session_dates", "horizon_minutes",
        "sampled_minutes_accepted", "sampled_minutes_rejected",
        "excluded_late_or_clock_invalid_ticks", "observed_session_dates",
        "analysis_ready_session_dates", "eligible_origins", "near_end_labels", "label_coverage",
        "technical_calls", "technical_hits", "technical_hit_rate",
        "technical_wilson95_lower", "technical_wilson95_upper",
        "technical_days_with_calls", "technical_mean_day_hit_rate",
        "technical_min_day_hit_rate", "technical_max_day_hit_rate",
        "prior_momentum_calls", "prior_momentum_hits", "prior_momentum_hit_rate",
        "prior_momentum_wilson95_lower", "prior_momentum_wilson95_upper",
        "prior_momentum_days_with_calls", "prior_momentum_mean_day_hit_rate",
        "prior_momentum_min_day_hit_rate", "prior_momentum_max_day_hit_rate",
        "always_up_calls", "always_up_hits", "always_up_hit_rate",
        "always_up_wilson95_lower", "always_up_wilson95_upper",
        "always_up_days_with_calls", "always_up_mean_day_hit_rate",
        "always_up_min_day_hit_rate", "always_up_max_day_hit_rate",
        "technical_vs_always_up_matched_calls", "technical_vs_always_up_days_with_matched_calls",
        "technical_vs_always_up_matched_technical_hit_rate",
        "technical_vs_always_up_matched_baseline_hit_rate",
        "technical_vs_always_up_matched_difference",
        "technical_vs_momentum_matched_calls", "technical_vs_momentum_days_with_matched_calls",
        "technical_vs_momentum_matched_technical_hit_rate",
        "technical_vs_momentum_matched_baseline_hit_rate",
        "technical_vs_momentum_matched_difference",
        "interpretation_note",
    )
    rows = []
    for partition, group in report["partitions"].items():
        dates = group["dates"]
        for horizon in HORIZONS:
            stat = group["horizons"][str(horizon)]
            rows.append((
                report["source"], report["data_status"], TECHNICAL_FORMULA_VERSION,
                "whole session dates; chronological 60/20/20; origins >=60 minutes apart",
                partition, dates[0], dates[-1], len(dates), horizon,
                report["sampled_minutes_accepted"], report["sampled_minutes_rejected"],
                report["excluded_late_or_clock_invalid_ticks"],
                report["observed_session_dates"], report["analysis_ready_session_dates"],
                stat["eligible_origins"], stat["near_end_labels"], stat["label_coverage"],
                stat["technical_calls"], stat["technical_hits"], stat["technical_hit_rate"],
                stat["technical_wilson95_lower"], stat["technical_wilson95_upper"],
                stat["technical_days_with_calls"], stat["technical_mean_day_hit_rate"],
                stat["technical_min_day_hit_rate"], stat["technical_max_day_hit_rate"],
                stat["momentum_calls"], stat["momentum_hits"], stat["momentum_hit_rate"],
                stat["momentum_wilson95_lower"], stat["momentum_wilson95_upper"],
                stat["momentum_days_with_calls"], stat["momentum_mean_day_hit_rate"],
                stat["momentum_min_day_hit_rate"], stat["momentum_max_day_hit_rate"],
                stat["always_up_calls"], stat["always_up_hits"], stat["always_up_hit_rate"],
                stat["always_up_wilson95_lower"], stat["always_up_wilson95_upper"],
                stat["always_up_days_with_calls"], stat["always_up_mean_day_hit_rate"],
                stat["always_up_min_day_hit_rate"], stat["always_up_max_day_hit_rate"],
                stat["technical_vs_always_up_matched_calls"],
                stat["technical_vs_always_up_days_with_matched_calls"],
                stat["technical_vs_always_up_matched_technical_hit_rate"],
                stat["technical_vs_always_up_matched_baseline_hit_rate"],
                stat["technical_vs_always_up_matched_difference"],
                stat["technical_vs_momentum_matched_calls"],
                stat["technical_vs_momentum_days_with_matched_calls"],
                stat["technical_vs_momentum_matched_technical_hit_rate"],
                stat["technical_vs_momentum_matched_baseline_hit_rate"],
                stat["technical_vs_momentum_matched_difference"],
                "Experimental rule comparison. Wilson interval assumes independent calls, "
                "which is unverified for market time series; day hit rates are descriptive "
                "and neither is a release criterion",
            ))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", suffix=".csv",
                                         prefix=".banknifty-report-", dir=target.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(rows)

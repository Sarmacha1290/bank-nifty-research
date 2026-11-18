"""Point-in-time research rows from recorded Groww sampled index minutes.

This prepares labelled observations; it does not fit or release a model.
Only timely received ticks form features or labels. Every selected origin has
61 continuous accepted prior minutes and is chosen without inspecting labels.
"""
from collections import defaultdict
import csv
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile

from .agents.luna import HORIZONS
from .backtest import _split_dates
from .core import label_returns
from .candle_patterns import PATTERN_NAMES, VERSION as CANDLE_PATTERN_VERSION, analyze_candle_patterns
from .feed import IndexTick
from .feed_quality import (MAX_RESEARCH_ENDPOINT_AGE_SECONDS, SAMPLED_SOURCE,
                           accepted_close_age_by_end, inspect_sampled_minutes)
from .groww import IST
from .expiry_context import expiry_features, load_expiry_references
from .event_context import describe_event_window, load_known_events
from .indicator_library import VERSION as INDICATOR_VERSION, calculate_price_indicators
from .market_calendar import regular_session
from .research_row_audit import audit_research_rows
from .technical_features import build_technical_snapshot
from .tick_history import load_index_ticks


MAX_RECEIPT_DELAY = timedelta(seconds=5)
FEATURES = (
    "current_price", "return_5m_pct", "return_15m_pct",
    "return_30m_pct", "return_60m_pct", "ema_fast_distance_pct",
    "ema_slow_distance_pct", "rsi_14", "atr_pct", "trend_strength",
)
ADDITIONAL_INDICATORS = (
    "return_1m_pct", "return_3m_pct", "return_10m_pct",
    *(f"log_return_{period}m_pct" for period in (1, 3, 5, 10, 15, 30, 60)),
    *(f"sma_{period}" for period in (5, 12, 20, 26, 60)),
    *(f"ema_{period}" for period in (5, 12, 20, 26, 60)),
    *(f"sma_{period}_distance_pct" for period in (5, 12, 20, 26, 60)),
    "ema_5_distance_pct", "ema_20_distance_pct", "ema_60_distance_pct",
    "macd", "macd_signal", "macd_histogram", "atr_14", "atr_28",
    *(f"rolling_{period}_{field}" for period in (20, 60)
      for field in ("high", "low", "range", "midpoint", "location_pct")),
    "realized_vol_20_per_minute_pct", "realized_vol_60_per_minute_pct",
    "realized_vol_20_within_slice_percentile", "within_slice_volatility_regime",
    "bollinger_middle_20", "bollinger_upper_20", "bollinger_lower_20",
    "bollinger_width_20_pct", "stochastic_k_14_pct", "candle_body",
    "upper_wick", "lower_wick", "true_range", "breakout_20", "breakdown_20",
    "two_close_breakout_20", "two_close_breakdown_20",
    "confirmed_swing_high", "confirmed_swing_low", "momentum_alignment",
    "body_to_range_pct", "upper_wick_to_range_pct", "lower_wick_to_range_pct",
    *(f"pattern_{name}" for name in PATTERN_NAMES),
    *(f"price_change_{period}m_points" for period in (5, 15, 30, 60)),
    *(f"movement_efficiency_{period}m_pct" for period in (5, 15, 30, 60)),
    "price_acceleration_5m_points",
    "expiry_date", "calendar_days_to_expiry", "is_expiry_day",
    "is_pre_expiry_session", "expiry_reference_observed_at_utc",
    "expiry_reference_source", "expiry_catalog_sha256",
    "known_recent_bank_events", "known_recent_macro_events",
    "known_upcoming_events", "event_context_status", "event_context_forecast_eligible",
)
CSV_FIELDS = (
    "source", "indicator_formula_version", "candle_pattern_formula_version", "session_date_ist", "partition", "origin_at_utc",
    "origin_close_age_seconds", "decision_at_utc", "horizon_minutes",
    "target_at_utc", "target_close_age_seconds", "label_status",
    "target_return_pct", *FEATURES, *ADDITIONAL_INDICATORS, "interpretation",
)


def build_training_research_rows(ticks, *, expiry_references=(), events=()):
    """Create past-only features with near-end sampled labels or explicit gaps."""
    timely = []
    excluded = 0
    for tick in ticks:
        if (not isinstance(tick, IndexTick) or tick.symbol != "NSE-BANKNIFTY" or
                tick.source != "Groww index feed" or tick.exchange_at.tzinfo is None or
                tick.received_at.tzinfo is None):
            raise ValueError("Training research needs validated Bank Nifty feed updates")
        local = tick.exchange_at.astimezone(IST)
        minute_end = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
        if (tick.received_at < tick.exchange_at or
                tick.received_at > minute_end + MAX_RECEIPT_DELAY):
            excluded += 1
            continue
        timely.append(tick)
    quality, accepted = inspect_sampled_minutes(timely)
    close_age_by_end = accepted_close_age_by_end(quality)
    if any(row.symbol != "NSE-BANKNIFTY" or row.source != SAMPLED_SOURCE for row in accepted):
        raise ValueError("Accepted bars did not match the sampled index source")
    by_date = defaultdict(list)
    for candle in accepted:
        by_date[candle.end.astimezone(IST).date()].append(candle)
    origins = {}
    excluded_weak_origins = 0
    for day, candles in sorted(by_date.items()):
        session = regular_session(day)
        candles.sort(key=lambda candle: candle.end)
        by_end = {candle.end.astimezone(timezone.utc): candle for candle in candles}
        if len(by_end) != len(candles):
            raise ValueError("Duplicate sampled minute endpoint")
        chosen = []
        last_origin = None
        for candle in candles:
            origin = candle.end
            if last_origin is not None and origin - last_origin < timedelta(minutes=60):
                continue
            history = [by_end.get((origin - timedelta(minutes=offset)).astimezone(timezone.utc))
                       for offset in range(60, -1, -1)]
            if any(item is None for item in history):
                continue
            if close_age_by_end[origin.astimezone(timezone.utc)] > MAX_RESEARCH_ENDPOINT_AGE_SECONDS:
                excluded_weak_origins += 1
                continue
            # Selection uses past coverage only; later outcomes never choose origins.
            chosen.append((origin, history, candles))
            last_origin = origin
        if chosen:
            origins[day] = (session, chosen)
    partitions = _split_dates(sorted(origins))
    rows = []
    weak_target_labels = 0
    for day in sorted(origins):
        session, chosen = origins[day]
        for origin, history, candles in chosen:
            decision_at = origin + MAX_RECEIPT_DELAY
            snapshot = build_technical_snapshot(history, origin, generated_at=decision_at)
            extra = calculate_price_indicators(history, origin)["features"]
            shapes = analyze_candle_patterns(history, origin)
            extra.update({name: shapes[name] for name in (
                "body_to_range_pct", "upper_wick_to_range_pct", "lower_wick_to_range_pct")})
            extra.update({f"pattern_{name}": found for name, found in shapes["patterns"].items()})
            extra.update(expiry_features(decision_at, expiry_references))
            event_window = describe_event_window(decision_at, events)
            extra.update({"known_recent_bank_events": event_window["known_recent_bank_events"],
                          "known_recent_macro_events": event_window["known_recent_macro_events"],
                          "known_upcoming_events": event_window["known_upcoming_events"],
                          "event_context_status": event_window["source_status"],
                          "event_context_forecast_eligible": event_window["forecast_eligible"]})
            for label in label_returns(candles, origin, session, HORIZONS):
                target_at = datetime.fromisoformat(label["target_at"]).astimezone(timezone.utc)
                target_age = close_age_by_end.get(target_at) if label["status"] == "ready" else None
                status = label["status"]
                target_return = label["return_pct"]
                if status == "ready" and (target_age is None or target_age > MAX_RESEARCH_ENDPOINT_AGE_SECONDS):
                    status = "weak_target_edge"
                    target_return = None
                    weak_target_labels += 1
                rows.append({
                    "source": SAMPLED_SOURCE,
                    "indicator_formula_version": INDICATOR_VERSION,
                    "candle_pattern_formula_version": CANDLE_PATTERN_VERSION,
                    "session_date_ist": day.isoformat(),
                    "partition": partitions.get(day, "unassigned_too_few_dates"),
                    "origin_at_utc": origin.astimezone(timezone.utc).isoformat(),
                    "origin_close_age_seconds": close_age_by_end[origin.astimezone(timezone.utc)],
                    "decision_at_utc": decision_at.astimezone(timezone.utc).isoformat(),
                    "horizon_minutes": label["horizon_minutes"],
                    "target_at_utc": target_at.isoformat(),
                    "target_close_age_seconds": target_age,
                    "label_status": status,
                    "target_return_pct": target_return,
                    **{name: getattr(snapshot, name) for name in FEATURES},
                    **{name: extra[name] for name in ADDITIONAL_INDICATORS},
                    "interpretation": "sampled_near_boundary_prices_research_only_not_a_trained_or_validated_forecast",
                })
    audit = audit_research_rows(rows, FEATURES + ADDITIONAL_INDICATORS)
    return {
        "raw_ticks": len(timely) + excluded,
        "excluded_late_or_clock_invalid_ticks": excluded,
        "accepted_minutes": len(accepted),
        "rejected_observed_minutes": sum(item.status != "accepted" for item in quality),
        "analysis_ready_session_dates": len(origins),
        "selected_origins": sum(len(chosen) for _, chosen in origins.values()),
        "excluded_weak_origins": excluded_weak_origins,
        "weak_target_labels": weak_target_labels,
        "split_status": "ready" if partitions else "insufficient_sessions",
        "rows": rows,
        "structural_audit": audit,
    }


def export_training_research_csv(database, destination):
    """Atomically export labelled research rows; do not overwrite the archive."""
    source, target = Path(database), Path(destination)
    if source.resolve() == target.resolve() or (
            source.exists() and target.exists() and source.samefile(target)):
        raise ValueError("Research export must not overwrite the market database")
    report = build_training_research_rows(load_index_ticks(source),
        expiry_references=load_expiry_references(source),
        events=load_known_events(source, datetime.now(timezone.utc)))
    if not report["rows"]:
        raise ValueError("No continuous point-in-time intraday research rows are available")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="",
                                         suffix=".csv", prefix=".banknifty-research-",
                                         dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(report["rows"])
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return report

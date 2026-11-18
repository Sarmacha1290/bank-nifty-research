"""Ex-post aligned index and constituent LTP movement, without weight claims.

Both endpoint index minutes must pass the sampled-minute quality gate. Each
bank quote must have an exchange timestamp no later than its index anchor,
be received by the endpoint's five-second decision cutoff, and be at most
five seconds old. Missing quotes are never filled or treated as zero moves.
"""

from bisect import bisect_right
from collections import defaultdict
import csv
from datetime import timedelta, timezone
from math import isfinite
from numbers import Real
import os
from pathlib import Path
import tempfile

from .equity_feed import EquityTick
from .equity_history import load_equity_ticks
from .feed import IndexTick
from .feed_quality import accepted_close_age_by_end, inspect_sampled_minutes
from .groww import IST
from .market_calendar import regular_session
from .nse_weights import load_reference_weights
from .tick_history import latest_index_session_date, load_index_ticks


HORIZONS = (15, 30, 60)
MAX_QUOTE_AGE_SECONDS = 5.0
MAX_ENDPOINT_RECEIPT_SECONDS = 5.0


def _bank_at(ticks, times, anchor, boundary):
    position = bisect_right(times, anchor.exchange_at) - 1
    cutoff = boundary + timedelta(seconds=MAX_ENDPOINT_RECEIPT_SECONDS)
    while position >= 0:
        item = ticks[position]
        age = (anchor.exchange_at - item.exchange_at).total_seconds()
        if age > MAX_QUOTE_AGE_SECONDS:
            break
        if item.received_at <= cutoff and item.received_at >= item.exchange_at:
            return item, age
        position -= 1
    return None, None


def compare_aligned_bank_moves(base_index, target_index, base_end, target_end,
                               equity_ticks, expected_symbols):
    """Describe one already-completed index interval and same-cutoff bank pairs."""
    symbols = tuple(expected_symbols)
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError("A unique expected bank list is required")
    if (not isinstance(base_index, IndexTick) or not isinstance(target_index, IndexTick) or
            base_index.symbol != target_index.symbol or
            base_index.source != "Groww index feed" or target_index.source != "Groww index feed" or
            any(value.tzinfo is None or value.utcoffset() is None
                for value in (base_end, target_end, base_index.exchange_at,
                              target_index.exchange_at, base_index.received_at,
                              target_index.received_at)) or
            not base_end < target_end or
            (target_end - base_end) != timedelta(minutes=int((target_end - base_end).total_seconds() / 60)) or
            int((target_end - base_end).total_seconds() / 60) not in HORIZONS or
            target_index.exchange_at - base_index.exchange_at < timedelta(seconds=1)):
        raise ValueError("A valid completed Groww index interval is required")
    for anchor, boundary in ((base_index, base_end), (target_index, target_end)):
        if not (boundary - timedelta(minutes=1) <= anchor.exchange_at < boundary and
                anchor.received_at <= boundary + timedelta(seconds=MAX_ENDPOINT_RECEIPT_SECONDS) and
                anchor.received_at >= anchor.exchange_at and
                (boundary - anchor.exchange_at).total_seconds() <= MAX_QUOTE_AGE_SECONDS):
            raise ValueError("Index endpoint is not near a received minute close")
    if base_end.astimezone(IST).date() != target_end.astimezone(IST).date():
        raise ValueError("Bank comparison cannot cross an NSE session date")
    if any(isinstance(value, bool) or not isinstance(value, Real) or
           not isfinite(value) or value <= 0 for value in
           (base_index.value, target_index.value)):
        raise ValueError("Index prices must be positive")
    by_symbol = defaultdict(list)
    for tick in equity_ticks:
        if (not isinstance(tick, EquityTick) or tick.source != "Groww equity LTP feed" or
                isinstance(tick.ltp, bool) or not isinstance(tick.ltp, Real) or
                not isfinite(tick.ltp) or tick.ltp <= 0 or
                any(value.tzinfo is None or value.utcoffset() is None
                    for value in (tick.exchange_at, tick.received_at))):
            raise ValueError("Expected validated Groww bank LTP records")
        if tick.symbol in symbols:
            by_symbol[tick.symbol].append(tick)
    rows = []
    missing = []
    for symbol in symbols:
        items = sorted(by_symbol[symbol], key=lambda item: item.exchange_at)
        times = [item.exchange_at for item in items]
        base, base_age = _bank_at(items, times, base_index, base_end)
        target, target_age = _bank_at(items, times, target_index, target_end)
        if base is None or target is None:
            missing.append({"symbol": symbol, "missing_base": base is None,
                            "missing_target": target is None})
            continue
        change = 100 * (target.ltp / base.ltp - 1)
        rows.append({"symbol": symbol, "base_ltp": base.ltp,
                     "target_ltp": target.ltp, "change_pct": change,
                     "base_quote_age_seconds": base_age,
                     "target_quote_age_seconds": target_age})
    complete = not missing
    return {
        "base_end_utc": base_end.astimezone(timezone.utc).isoformat(),
        "target_end_utc": target_end.astimezone(timezone.utc).isoformat(),
        "horizon_minutes": int((target_end - base_end).total_seconds() / 60),
        "index_move_points": target_index.value - base_index.value,
        "index_return_pct": 100 * (target_index.value / base_index.value - 1),
        "bank_rows": rows, "missing_banks": missing,
        "banks_with_aligned_pairs": len(rows), "expected_banks": len(symbols),
        "advancing_banks": sum(item["change_pct"] > 0 for item in rows) if complete else None,
        "declining_banks": sum(item["change_pct"] < 0 for item in rows) if complete else None,
        "unweighted_mean_bank_return_pct": (
            sum(item["change_pct"] for item in rows) / len(rows) if complete else None),
        "status": "complete_unweighted_ex_post" if complete else "incomplete_bank_endpoints",
        "interpretation": "aligned_saved_ltp_moves_not_weighted_index_contributions_or_forecasts",
    }


def review_recorded_bank_moves(index_ticks, equity_ticks, expected_symbols):
    """Pick the latest fixed eligible origin with all three index targets.

    A 61-minute accepted history precedes the origin, matching the research
    row feature window. This retrospective choice is not a live prediction.
    """
    timely = []
    for tick in index_ticks:
        if not isinstance(tick, IndexTick):
            raise ValueError("Expected recorded index updates")
        end = tick.exchange_at.astimezone(IST).replace(second=0, microsecond=0) + timedelta(minutes=1)
        if tick.exchange_at <= tick.received_at <= end + timedelta(seconds=MAX_ENDPOINT_RECEIPT_SECONDS):
            timely.append(tick)
    quality, accepted = inspect_sampled_minutes(timely)
    close_age = accepted_close_age_by_end(quality)
    by_end = {row.end.astimezone(timezone.utc): row for row in accepted}
    by_minute = defaultdict(list)
    for tick in timely:
        minute = tick.exchange_at.astimezone(timezone.utc).replace(second=0, microsecond=0)
        by_minute[minute].append(tick)
    for origin in sorted(by_end, reverse=True):
        history = [by_end.get(origin - timedelta(minutes=offset)) for offset in range(60, -1, -1)]
        targets = [origin + timedelta(minutes=minutes) for minutes in HORIZONS]
        if (any(item is None for item in history) or any(target not in by_end for target in targets) or
                any(close_age.get(end, 60) > MAX_QUOTE_AGE_SECONDS for end in [origin, *targets])):
            continue
        endpoints = [origin, *targets]
        anchors = []
        for end in endpoints:
            group = by_minute[end - timedelta(minutes=1)]
            if not group:
                break
            anchors.append(max(group, key=lambda tick: tick.exchange_at))
        if len(anchors) != 4:
            continue
        if any(anchor.received_at > end + timedelta(seconds=MAX_ENDPOINT_RECEIPT_SECONDS)
               for anchor, end in zip(anchors, endpoints)):
            continue
        results = [compare_aligned_bank_moves(anchors[0], anchors[index+1],
                   origin, targets[index], equity_ticks, expected_symbols)
                   for index in range(3)]
        return {"status": "reviewed_index_endpoints", "origin_end_utc": origin.isoformat(),
                "accepted_minutes": len(accepted),
                "excluded_late_index_updates": len(index_ticks) - len(timely),
                "results": results,
                "interpretation": "retrospective_observed_moves_only_not_a_forecast"}
    return {"status": "insufficient_index_history", "origin_end_utc": None,
            "accepted_minutes": len(accepted),
            "excluded_late_index_updates": len(index_ticks) - len(timely),
            "results": [],
            "interpretation": "no_61_minute_history_with_three_exact_targets"}


def review_latest_recorded_bank_moves(database, reference_path):
    reference = load_reference_weights(reference_path)
    symbols = tuple("NSE-" + item["symbol"] for item in reference["constituents"])
    day = latest_index_session_date(database)
    if day is None:
        return {"status": "no_index_data", "session_date_ist": None,
                "bank_list_as_of_date": reference["as_of_date"], "results": []}
    session = regular_session(day)
    result = review_recorded_bank_moves(
        load_index_ticks(database, start=session.open, end=session.close),
        load_equity_ticks(database, start=session.open, end=session.close, symbols=symbols),
        symbols)
    return {**result, "session_date_ist": day.isoformat(),
            "bank_list_as_of_date": reference["as_of_date"],
            "weight_status": "not_used_dated_list_only"}


def export_latest_bank_moves_csv(database, reference_path, destination):
    source, target = Path(database), Path(destination)
    if target.resolve() == source.resolve() or (
            source.exists() and target.exists() and source.samefile(target)):
        raise ValueError("Bank movement CSV cannot overwrite the archive")
    review = review_latest_recorded_bank_moves(source, reference_path)
    if not review.get("results"):
        raise ValueError("No completed aligned index intervals to export")
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = ("session_date_ist", "bank_list_as_of_date", "base_end_utc",
              "target_end_utc", "horizon_minutes", "interval_status", "symbol",
              "availability", "base_ltp", "target_ltp", "bank_change_pct",
              "base_quote_age_seconds", "target_quote_age_seconds",
              "index_move_points", "index_return_pct", "interpretation")
    temporary = None
    count = 0
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="",
                suffix=".csv", prefix=".bank-moves-", dir=target.parent,
                delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for item in review["results"]:
                common = {"session_date_ist": review["session_date_ist"],
                          "bank_list_as_of_date": review["bank_list_as_of_date"],
                          "base_end_utc": item["base_end_utc"],
                          "target_end_utc": item["target_end_utc"],
                          "horizon_minutes": item["horizon_minutes"],
                          "interval_status": item["status"],
                          "index_move_points": item["index_move_points"],
                          "index_return_pct": item["index_return_pct"],
                          "interpretation": item["interpretation"]}
                for bank in item["bank_rows"]:
                    writer.writerow({**common, "symbol": bank["symbol"],
                                     "availability": "aligned_pair", "base_ltp": bank["base_ltp"],
                                     "target_ltp": bank["target_ltp"],
                                     "bank_change_pct": bank["change_pct"],
                                     "base_quote_age_seconds": bank["base_quote_age_seconds"],
                                     "target_quote_age_seconds": bank["target_quote_age_seconds"]})
                    count += 1
                for bank in item["missing_banks"]:
                    missing = ("missing_both" if bank["missing_base"] and bank["missing_target"]
                               else "missing_base" if bank["missing_base"] else "missing_target")
                    writer.writerow({**common, "symbol": bank["symbol"],
                                     "availability": missing})
                    count += 1
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return count

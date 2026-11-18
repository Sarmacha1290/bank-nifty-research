"""Diagnostic index/bank feed timing audit; never makes a contribution claim.

For each observed index minute, use its last exchange-stamped update as an
anchor. A bank update qualifies only when its exchange time is no later than
the anchor, its age is within the displayed trial threshold, and the laptop
had received it by the anchor's receipt time. No missing quote is filled.
"""
from bisect import bisect_right
from collections import defaultdict
from datetime import timedelta, timezone

from .equity_feed import EquityTick
from .equity_history import load_equity_ticks
from .feed import IndexTick
from .market_calendar import regular_session
from .nse_weights import load_reference_weights
from .tick_history import latest_index_session_date, load_index_ticks


def audit_alignment(index_ticks, equity_ticks, expected_symbols, *, max_lag_seconds=5):
    symbols = tuple(expected_symbols)
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError("Expected unique bank symbols")
    if not isinstance(max_lag_seconds, (int, float)) or not 0 < max_lag_seconds <= 120:
        raise ValueError("Trial maximum lag must be between 0 and 120 seconds")
    anchors = {}
    for tick in index_ticks:
        if not isinstance(tick, IndexTick) or tick.source != "Groww index feed":
            raise ValueError("Expected recorded Groww index ticks")
        minute = tick.exchange_at.astimezone(timezone.utc).replace(second=0, microsecond=0)
        if minute not in anchors or tick.exchange_at > anchors[minute].exchange_at:
            anchors[minute] = tick
    by_symbol = defaultdict(list)
    for tick in equity_ticks:
        if not isinstance(tick, EquityTick) or tick.source != "Groww equity LTP feed":
            raise ValueError("Expected recorded Groww bank-equity ticks")
        if tick.symbol in symbols:
            by_symbol[tick.symbol].append(tick)
    times = {}
    for symbol in symbols:
        by_symbol[symbol].sort(key=lambda tick: tick.exchange_at)
        times[symbol] = [tick.exchange_at for tick in by_symbol[symbol]]
    counts = {symbol: {"fresh": 0, "stale": 0, "not_received_yet": 0, "no_prior_quote": 0}
              for symbol in symbols}
    complete = 0
    for anchor in (anchors[key] for key in sorted(anchors)):
        all_fresh = True
        for symbol in symbols:
            stock_times = times[symbol]
            prior = bisect_right(stock_times, anchor.exchange_at) - 1
            if prior < 0:
                reason = "no_prior_quote"
            else:
                reason = "stale"
                while prior >= 0:
                    age = (anchor.exchange_at - stock_times[prior]).total_seconds()
                    if age > max_lag_seconds:
                        break
                    if by_symbol[symbol][prior].received_at <= anchor.received_at:
                        reason = "fresh"
                        break
                    reason = "not_received_yet"
                    prior -= 1
            counts[symbol][reason] += 1
            if reason != "fresh":
                all_fresh = False
        complete += all_fresh
    return {"observed_index_minutes": len(anchors),
            "all_banks_fresh_minutes": complete,
            "trial_max_lag_seconds": max_lag_seconds,
            "per_bank": counts,
            "latest_index_exchange_at_utc": (max(anchors.values(), key=lambda tick: tick.exchange_at)
                                              .exchange_at.astimezone(timezone.utc).isoformat()
                                              if anchors else None)}


def audit_latest_recorded_session(database, reference_path, *, max_lag_seconds=5):
    reference = load_reference_weights(reference_path)
    symbols = ["NSE-" + row["symbol"] for row in reference["constituents"]]
    day = latest_index_session_date(database)
    if day is None:
        result = audit_alignment([], [], symbols, max_lag_seconds=max_lag_seconds)
        return {**result, "status": "no_index_data", "session_date": None,
                "expected_regular_minutes": 0, "reference_as_of_date": reference["as_of_date"]}
    session = regular_session(day)
    index_ticks = load_index_ticks(database, start=session.open, end=session.close)
    equity_ticks = load_equity_ticks(database, start=session.open, end=session.close,
                                     symbols=symbols)
    result = audit_alignment(index_ticks, equity_ticks, symbols,
                             max_lag_seconds=max_lag_seconds)
    return {**result, "status": "diagnostic_only", "session_date": day.isoformat(),
            "expected_regular_minutes": int((session.close - session.open) / timedelta(minutes=1)),
            "reference_as_of_date": reference["as_of_date"]}

"""Retrospective Bank Nifty futures quote comparison with strict trade ages."""

from datetime import datetime, timedelta
from math import isfinite

from .futures_history import load_future_snapshots
from .groww import IST


MAX_RECEIPT_GAP = timedelta(minutes=30)
MAX_TRADE_AGE_SECONDS = 5.0


def compare_saved_future_quotes(previous, current):
    """Compare two saved quotes only when their last trades are recent/distinct."""
    if (previous.get("source") != "Groww" or current.get("source") != "Groww" or
            previous.get("trading_symbol") != current.get("trading_symbol") or
            previous.get("expiry_date") != current.get("expiry_date")):
        raise ValueError("Same-contract Groww futures quotes are required")
    try:
        earlier = datetime.fromisoformat(previous["received_at_utc"])
        later = datetime.fromisoformat(current["received_at_utc"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Futures receipt times are invalid") from exc
    if (earlier.tzinfo is None or earlier.utcoffset() is None or
            later.tzinfo is None or later.utcoffset() is None or
            not earlier < later or later - earlier > MAX_RECEIPT_GAP or
            earlier.astimezone(IST).date() != later.astimezone(IST).date()):
        raise ValueError("Futures quotes need ordered same-day receipts within 30 minutes")
    trades = []
    ages = []
    for row, received in ((previous, earlier), (current, later)):
        value = row.get("last_trade_at_utc")
        if value is None:
            return {"status": "missing_last_trade_time", "comparison": None}
        try:
            trade = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Futures last-trade time is invalid") from exc
        if trade.tzinfo is None or trade.utcoffset() is None:
            raise ValueError("Futures last-trade time lacks a timezone")
        age = (received - trade).total_seconds()
        trades.append(trade)
        ages.append(age)
    if (not 0 <= ages[0] <= MAX_TRADE_AGE_SECONDS or
            not 0 <= ages[1] <= MAX_TRADE_AGE_SECONDS or
            not trades[0] < trades[1]):
        return {"status": "stale_or_repeated_last_trade", "comparison": None,
                "previous_trade_age_seconds": ages[0],
                "current_trade_age_seconds": ages[1]}
    prices = [row.get("last_price") for row in (previous, current)]
    interest = [row.get("open_interest") for row in (previous, current)]
    if (any(isinstance(value, bool) or not isinstance(value, (int, float)) or
            not isfinite(value) or value <= 0 for value in prices) or
            any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in interest)):
        return {"status": "missing_price_or_open_interest", "comparison": None}
    point_change = prices[1] - prices[0]
    oi_change = interest[1] - interest[0]
    direction = ("price_up" if point_change > 0 else "price_down" if point_change < 0
                 else "price_flat")
    direction += ("_oi_up" if oi_change > 0 else "_oi_down" if oi_change < 0
                  else "_oi_flat")
    return {"status": "recent_distinct_last_trades",
            "comparison": {"trading_symbol": current["trading_symbol"],
                "expiry_date": current["expiry_date"],
                "previous_received_at_utc": earlier.isoformat(),
                "current_received_at_utc": later.isoformat(),
                "receipt_gap_seconds": (later - earlier).total_seconds(),
                "previous_trade_age_seconds": ages[0],
                "current_trade_age_seconds": ages[1],
                "price_change_points": point_change,
                "open_interest_change_reported": oi_change,
                "descriptive_quadrant": direction,
                "interpretation": "recent_last_trades_but_oi_exchange_time_and_trade_side_unverified_not_forecast"}}


def review_latest_saved_future_pair(database):
    snapshots = load_future_snapshots(database)
    if len(snapshots) < 2:
        return {"status": "need_two_saved_quotes", "snapshots": len(snapshots),
                "comparison": None}
    latest = snapshots[-1]
    for prior in reversed(snapshots[:-1]):
        if (prior.get("trading_symbol") != latest.get("trading_symbol") or
                prior.get("expiry_date") != latest.get("expiry_date")):
            continue
        try:
            result = compare_saved_future_quotes(prior, latest)
        except ValueError:
            continue
        return {**result, "snapshots": len(snapshots)}
    return {"status": "no_recent_same_contract_pair", "snapshots": len(snapshots),
            "comparison": None}

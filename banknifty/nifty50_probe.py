"""Short, read-only Nifty 50 index-feed check with no archive writes."""

from datetime import datetime, timezone
import time

from .feed import parse_index_tick
from .groww import IST
from .market_calendar import regular_market_status


def probe_nifty50_feed(feed, instrument, *, duration_seconds=30, now=None,
                       monotonic=None, sleep=None, stop_requested=None):
    """Count distinct fresh feed messages; never infer continuity or save prices."""
    if (instrument.get("exchange"), instrument.get("segment"),
            instrument.get("trading_symbol"), instrument.get("groww_symbol")) != (
            "NSE", "CASH", "NIFTY", "NSE-NIFTY") or not instrument.get("exchange_token"):
        raise ValueError("Expected the reviewed Nifty 50 cash-index instrument")
    if not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool) or not 0 < duration_seconds <= 60:
        raise ValueError("Probe duration must be at most 60 seconds")
    now = now or (lambda: datetime.now(timezone.utc))
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep
    stop_requested = stop_requested or (lambda: False)
    initial_status = regular_market_status(now())
    if initial_status != "regular_session":
        return {"status": initial_status, "unique": 0, "repeats": 0,
                "rejected": 0, "first_exchange_ist": None, "last_exchange_ist": None}
    subscription = [{"exchange": "NSE", "segment": "CASH",
                     "exchange_token": instrument["exchange_token"]}]
    result = {"status": "completed", "unique": 0, "repeats": 0,
              "rejected": 0, "first_exchange_ist": None, "last_exchange_ist": None}
    try:
        feed.subscribe_index_value(subscription)
    except Exception:
        result["status"] = "subscribe_failed"
        return result
    seen = set()
    try:
        deadline = monotonic() + duration_seconds
        while monotonic() < deadline and not stop_requested():
            if regular_market_status(now()) != "regular_session":
                result["status"] = "session_ended"
                break
            try:
                payload = feed.get_index_value()
            except Exception:
                result["status"] = "feed_interrupted"
                break
            try:
                tick = parse_index_tick(payload, instrument, now())
            except (TypeError, ValueError):
                result["rejected"] += 1
                tick = None
            if tick is not None:
                if tick.exchange_at in seen:
                    result["repeats"] += 1
                else:
                    seen.add(tick.exchange_at)
                    result["unique"] += 1
                    local = tick.exchange_at.astimezone(IST).isoformat()
                    if result["first_exchange_ist"] is None:
                        result["first_exchange_ist"] = local
                    result["last_exchange_ist"] = local
            sleep(min(0.5, max(0.0, deadline - monotonic())))
        if stop_requested() and result["status"] == "completed":
            result["status"] = "stopped"
    finally:
        try:
            feed.unsubscribe_index_value(subscription)
        except Exception:
            if result["status"] == "completed":
                result["status"] = "unsubscribe_failed"
    if result["status"] == "completed" and result["unique"] == 0:
        result["status"] = "no_fresh_updates"
    return result

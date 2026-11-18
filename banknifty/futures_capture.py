"""Bounded read-only archive of one catalogue-resolved Bank Nifty future."""

from datetime import datetime, timezone
import time

from .futures import FutureInstrument, parse_future_quote
from .futures_history import save_future_snapshot
from .groww import DataAccessError


def capture_future_quotes(client, instrument, database, *, duration_seconds=1800,
                          interval_seconds=300, stop_requested=lambda: False,
                          on_progress=None, clock=time.monotonic, pause=time.sleep,
                          now_utc=lambda: datetime.now(timezone.utc)):
    """Persist validated quotes; never infer quote freshness from receipt time."""
    if (not isinstance(instrument, FutureInstrument) or
            isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int) or
            not 300 <= duration_seconds <= 5400 or
            isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or
            not 60 <= interval_seconds <= 900 or interval_seconds > duration_seconds):
        raise ValueError("Use one catalogue future, 5-90 minute duration and 1-15 minute interval")
    started = clock()
    deadline = started + duration_seconds
    next_read = started
    attempts = saved = 0
    status = "completed"
    last_error = None
    while clock() < deadline:
        if stop_requested():
            status = "stopped"
            break
        if clock() < next_read:
            pause(min(0.5, next_read - clock(), deadline - clock()))
            continue
        attempts += 1
        try:
            response = client.banknifty_future_quote(instrument.trading_symbol)
            snapshot = parse_future_quote(response, instrument, now_utc())
            save_future_snapshot(database, snapshot)
        except DataAccessError as exc:
            status = "access_stopped"
            last_error = str(exc)
            break
        except (OSError, ValueError, TypeError):
            status = "invalid_or_unsaved_snapshot"
            last_error = "Futures response or archive did not pass validation"
            break
        saved += 1
        if on_progress is not None:
            on_progress({"attempts": attempts, "saved": saved,
                         "last_received_at_utc": snapshot.received_at_utc.isoformat(),
                         "last_trade_at_utc": (snapshot.last_trade_at_utc.isoformat()
                                               if snapshot.last_trade_at_utc else None),
                         "last_trade_age_seconds": (
                             (snapshot.received_at_utc - snapshot.last_trade_at_utc).total_seconds()
                             if snapshot.last_trade_at_utc else None),
                         "freshness": snapshot.freshness})
        next_read = started + attempts * interval_seconds
        if clock() >= next_read:
            next_read = clock() + interval_seconds
    return {"status": status, "attempts": attempts, "saved": saved,
            "trading_symbol": instrument.trading_symbol, "last_error": last_error,
            "interpretation": "receipt_time_and_last_trade_time_kept_separate_not_a_forecast"}

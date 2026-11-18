"""Bounded read-only collection of repeated Groww Bank Nifty option chains.

Each response retains local receipt time only. This does not establish the
exchange's snapshot time, trade-side flow or any forecast signal.
"""

from datetime import date, datetime, timezone
import time

from .agents.options_futures import analyze_option_chain
from .groww import DataAccessError
from .option_history import save_option_chain_analysis


def capture_option_chains(client, expiry, database, *, duration_seconds=1800,
                          interval_seconds=300, stop_requested=lambda: False,
                          on_progress=None, clock=time.monotonic, pause=time.sleep,
                          now_utc=lambda: datetime.now(timezone.utc)):
    """Save at most one chain per interval; stop on provider or archive failure."""
    if (not isinstance(expiry, date) or isinstance(expiry, datetime) or
            isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int) or
            not 300 <= duration_seconds <= 5400 or
            isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or
            not 60 <= interval_seconds <= 900 or
            interval_seconds > duration_seconds):
        raise ValueError("Use a dated expiry, 5-90 minute duration and 1-15 minute interval")
    started = clock()
    deadline = started + duration_seconds
    next_read = started
    attempts = saved = 0
    last_error = None
    status = "completed"
    while clock() < deadline:
        if stop_requested():
            status = "stopped"
            break
        if clock() < next_read:
            pause(min(0.5, next_read - clock(), deadline - clock()))
            continue
        attempts += 1
        try:
            response = client.banknifty_option_chain(expiry)
            received_at = now_utc()
            analysis = analyze_option_chain(response, expiry, received_at)
            save_option_chain_analysis(database, analysis)
        except DataAccessError as exc:
            status = "access_stopped"
            last_error = str(exc)
            break
        except (OSError, ValueError, TypeError):
            status = "invalid_or_unsaved_snapshot"
            last_error = "Option response or archive did not pass validation"
            break
        saved += 1
        if on_progress is not None:
            on_progress({"attempts": attempts, "saved": saved,
                         "last_received_at_utc": analysis.received_at_utc.isoformat(),
                         "complete_pairs": analysis.complete_pairs,
                         "strikes": analysis.strikes})
        next_read = started + attempts * interval_seconds
        if clock() >= next_read:
            # A slow request never triggers a rapid sequence of catch-up calls.
            next_read = clock() + interval_seconds
    return {"status": status, "attempts": attempts, "saved": saved,
            "expiry_date": expiry.isoformat(), "last_error": last_error,
            "interpretation": "receipt_time_only_exchange_freshness_and_trade_side_unverified"}

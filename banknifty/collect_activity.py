"""Conservative, read-only 14-bank Groww quote sweeps.

Fourteen serial REST reads every 15 seconds use at most 56 quote requests per
minute, below Groww's documented shared live-data limit of 300/minute. They
are not simultaneous exchange snapshots or real-time trade classification.
"""
from datetime import datetime, timezone
import sqlite3
import time
import uuid

from .groww import DataAccessError
from .market_activity import (detect_activity_spikes, detect_aggregate_spikes,
                              parse_bank_quote, summarize_bank_quotes)
from .market_activity_history import (load_bank_quote_sweeps, save_activity_alerts,
                                      save_bank_quotes)


def collect_bank_activity(client, instruments, database, *, duration_seconds,
                          stop_requested=lambda: False, interval_seconds=15,
                          on_progress=None, clock=time.monotonic, pause=time.sleep):
    instruments = tuple(instruments)
    if (len(instruments) != 14 or len({row["trading_symbol"] for row in instruments}) != 14
            or duration_seconds <= 0 or interval_seconds < 15):
        raise ValueError("Use 14 distinct bank symbols and at least a 15-second interval")
    deadline = clock() + duration_seconds
    sweep_count = saved = failures = 0
    last_summary = None
    last_alerts = []
    last_error = None
    access_stopped = False
    archive_stopped = False
    while clock() < deadline and not stop_requested():
        start = clock()
        sweep = []
        for instrument in instruments:
            if stop_requested():
                break
            try:
                response = client.equity_quote(instrument["trading_symbol"])
                sweep.append(parse_bank_quote(response, instrument["trading_symbol"],
                                              datetime.now(timezone.utc)))
            except DataAccessError as exc:
                last_error = str(exc)
                failures += 1
                if "request limit" in last_error.lower() or "rejected access" in last_error.lower():
                    access_stopped = True
                    break
            except ValueError:
                last_error = "Groww quote fields were invalid for one or more banks."
                failures += 1
            # Cap consecutive requests even if responses arrive instantly.
            if clock() < deadline and not stop_requested():
                pause(0.2)
        if sweep:
            try:
                save_bank_quotes(database, uuid.uuid4().hex, sweep)
            except (OSError, sqlite3.Error, ValueError):
                # Do not report unsaved quotes as captured or keep requesting
                # data after the local archive has failed.
                archive_stopped = True
                last_error = "Bank quote archive failed; inspect saved rows before another capture."
                break
            saved += len(sweep)
            sweep_count += 1
            last_summary = summarize_bank_quotes(sweep, ["NSE-" + row["trading_symbol"] for row in instruments])
            recent_sweeps = load_bank_quote_sweeps(database, limit=20)
            recent_rows = [row for group in recent_sweeps for row in group]
            expected = ["NSE-" + row["trading_symbol"] for row in instruments]
            current_times = {row.received_at.isoformat() for row in sweep}
            last_alerts = [alert for alert in
                           (detect_activity_spikes(recent_rows) +
                            detect_aggregate_spikes(recent_sweeps, expected))
                           if alert["at"] in current_times]
            save_activity_alerts(database, last_alerts)
            if on_progress is not None:
                on_progress({"sweeps": sweep_count, "saved": saved, "failures": failures,
                             "summary": last_summary, "alerts": last_alerts})
        else:
            break
        if access_stopped:
            break
        while clock() < min(deadline, start + interval_seconds) and not stop_requested():
            pause(min(0.5, max(0, min(deadline, start + interval_seconds) - clock())))
    return {"status": ("archive_error" if archive_stopped else
                       "access_stopped" if access_stopped else
                       "stopped" if stop_requested() else "no_data" if not saved else "completed"),
            "sweeps": sweep_count, "saved": saved, "failures": failures,
            "summary": last_summary, "alerts": last_alerts, "last_error": last_error,
            "warning": "Receipt-time quotes; no executed buy/sell split or simultaneous index-wide volume"}

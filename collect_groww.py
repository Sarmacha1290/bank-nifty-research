"""One-session, market-data-only Groww index-feed recorder.

The access token is typed at a masked prompt and held only in process memory.
No order API or candle inference is used.
"""

import argparse
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
import sqlite3
import time as clock

from banknifty.feed import parse_index_tick
from banknifty.archive_retry import save_with_busy_retry
from banknifty.equity_feed import parse_equity_ticks
from banknifty.equity_history import save_equity_ticks
from banknifty.depth_feed import parse_depth_ticks
from banknifty.depth_history import load_recent_depth_ticks, save_depth_ticks
from banknifty.depth_spikes import detect_depth_spikes
from banknifty.market_activity_history import save_activity_alerts
from banknifty.groww import IST, fetch_banknifty_instrument
from banknifty.market_calendar import regular_market_status
from banknifty.paper_pipeline import attempt_boundary_assessment
from banknifty.tick_history import save_index_tick
from banknifty.groww_feed_security import GrowwFeedSecurityError, isolated_groww_feed


def market_window(now):
    return regular_market_status(now) == "regular_session"


def collect(feed, instrument, database, *, duration_seconds, now=None, sleep=None,
            monotonic=None, max_silence_seconds=90, stop_requested=None,
            on_completed_minute=None, equity_instruments=(), depth_instruments=(),
            on_progress=None):
    """Poll Groww's documented index feed; save only fresh unique updates."""
    now = now or (lambda: datetime.now(timezone.utc))
    sleep = sleep or clock.sleep
    monotonic = monotonic or clock.monotonic
    stop_requested = stop_requested or (lambda: False)
    if duration_seconds <= 0 or duration_seconds > 390 * 60:
        raise ValueError("Capture duration must be between 1 second and 390 minutes")
    if max_silence_seconds <= 0:
        raise ValueError("max_silence_seconds must be positive")
    if not market_window(now()):
        return {"status": regular_market_status(now()), "inserted": 0, "duplicates": 0, "rejected": 0,
                "archive_busy_retries": 0, "equity_archive_busy_retries": 0,
                "depth_archive_busy_retries": 0,
                "paper_logged": 0, "paper_errors": 0, "equity_status": "not_started",
                "equity_inserted": 0, "equity_duplicates": 0, "equity_rejected": 0,
                "equity_symbols_with_updates": 0, "equity_expected_symbols": len(equity_instruments),
                "depth_status": "not_started", "depth_inserted": 0, "depth_duplicates": 0,
                "depth_rejected": 0, "depth_alerts": 0, "depth_alert_errors": 0,
                "depth_symbols_with_updates": 0,
                "depth_expected_symbols": len(depth_instruments)}
    subscriptions = [{"exchange": instrument["exchange"], "segment": instrument["segment"],
                      "exchange_token": instrument["exchange_token"]}]
    counts = {"status": "completed", "inserted": 0, "duplicates": 0, "rejected": 0,
              "archive_busy_retries": 0, "equity_archive_busy_retries": 0,
              "depth_archive_busy_retries": 0,
              "paper_logged": 0, "paper_errors": 0,
              "equity_status": "not_requested" if not equity_instruments else "pending",
              "equity_inserted": 0, "equity_duplicates": 0, "equity_rejected": 0,
              "equity_symbols_with_updates": 0, "equity_expected_symbols": len(equity_instruments),
              "depth_status": "not_requested" if not depth_instruments else "pending",
              "depth_inserted": 0, "depth_duplicates": 0, "depth_rejected": 0,
              "depth_alerts": 0, "depth_alert_errors": 0,
              "depth_symbols_with_updates": 0, "depth_expected_symbols": len(depth_instruments)}
    try:
        feed.subscribe_index_value(subscriptions)
    except GrowwFeedSecurityError:
        counts["status"] = "security_blocked"
        return counts
    except Exception:
        counts["status"] = "subscribe_failed"
        return counts
    try:
        equity_subscriptions = [{"exchange": row["exchange"], "segment": row["segment"],
                                 "exchange_token": row["exchange_token"]}
                                for row in equity_instruments]
        equity_active = False
        equity_seen = set()
        if equity_subscriptions:
            try:
                feed.subscribe_ltp(equity_subscriptions)
                equity_active = True
                counts["equity_status"] = "recording"
            except Exception:
                counts["equity_status"] = "subscribe_failed"
        depth_subscriptions = [{"exchange": row["exchange"], "segment": row["segment"],
                                "exchange_token": row["exchange_token"]}
                               for row in depth_instruments]
        depth_active = False
        depth_seen = set()
        if depth_subscriptions:
            try:
                feed.subscribe_market_depth(depth_subscriptions)
                depth_active = True
                counts["depth_status"] = "recording"
            except Exception:
                counts["depth_status"] = "subscribe_failed"
        end = monotonic() + duration_seconds
        last_new = monotonic()
        last_progress = monotonic()
        last_boundary = None
        while monotonic() < end and market_window(now()) and not stop_requested():
            try:
                payload = feed.get_index_value()
            except Exception:
                counts["status"] = "feed_interrupted"
                break
            try:
                tick = parse_index_tick(payload, instrument, now())
            except ValueError:
                counts["rejected"] += 1
                tick = None
            if tick is not None:
                try:
                    outcome, _ = save_with_busy_retry(
                        lambda: save_index_tick(database, tick), pause=sleep,
                        on_retry=lambda: counts.__setitem__("archive_busy_retries",
                            counts["archive_busy_retries"] + 1))
                except (OSError, ValueError, sqlite3.Error):
                    counts["status"] = "archive_error"
                    break
                counts["duplicates" if outcome == "duplicate" else "inserted"] += 1
                if outcome == "inserted":
                    last_new = monotonic()
                    boundary = tick.exchange_at.astimezone(IST).replace(second=0, microsecond=0)
                    if on_completed_minute is not None and (last_boundary is None or boundary > last_boundary):
                        last_boundary = boundary
                        try:
                            counts["paper_logged"] += int(on_completed_minute(boundary, now()) or 0)
                        except Exception:
                            counts["paper_errors"] += 1
            if equity_active:
                try:
                    equity_payload = feed.get_ltp()
                except Exception:
                    equity_active = False
                    counts["equity_status"] = "feed_interrupted"
                else:
                    equity_ticks, rejected = parse_equity_ticks(
                        equity_payload, equity_instruments, now())
                    counts["equity_rejected"] += rejected
                    try:
                        saved, _ = save_with_busy_retry(
                            lambda: save_equity_ticks(database, equity_ticks), pause=sleep,
                            on_retry=lambda: counts.__setitem__("equity_archive_busy_retries",
                                counts["equity_archive_busy_retries"] + 1))
                    except (OSError, sqlite3.Error, ValueError):
                        equity_active = False
                        counts["equity_status"] = "archive_error"
                    else:
                        counts["equity_inserted"] += saved["inserted"]
                        counts["equity_duplicates"] += saved["duplicates"]
                        equity_seen.update(saved["inserted_symbols"])
            if depth_active:
                try:
                    depth_payload = feed.get_market_depth()
                except Exception:
                    depth_active = False
                    counts["depth_status"] = "feed_interrupted"
                else:
                    depth_ticks, rejected = parse_depth_ticks(depth_payload, depth_instruments, now())
                    counts["depth_rejected"] += rejected
                    try:
                        saved_depth, _ = save_with_busy_retry(
                            lambda: save_depth_ticks(database, depth_ticks), pause=sleep,
                            on_retry=lambda: counts.__setitem__("depth_archive_busy_retries",
                                counts["depth_archive_busy_retries"] + 1))
                    except (OSError, sqlite3.Error, ValueError):
                        depth_active = False
                        counts["depth_status"] = "archive_error"
                    else:
                        counts["depth_inserted"] += saved_depth["inserted"]
                        counts["depth_duplicates"] += saved_depth["duplicates"]
                        depth_seen.update(saved_depth["symbols"])
                        if saved_depth["inserted"]:
                            try:
                                alerts = detect_depth_spikes(load_recent_depth_ticks(database, limit=500))
                                newest = {(tick.symbol, tick.exchange_at.isoformat()) for tick in depth_ticks}
                                counts["depth_alerts"] += save_activity_alerts(database, [
                                    alert for alert in alerts if (alert["symbol"], alert["at"]) in newest])
                            except (OSError, sqlite3.Error, ValueError):
                                counts["depth_alert_errors"] += 1
            if monotonic() - last_new >= max_silence_seconds:
                counts["status"] = "no_fresh_updates"
                break
            if on_progress is not None and monotonic() - last_progress >= 10:
                last_progress = monotonic()
                try:
                    on_progress(dict(counts))
                except Exception:
                    pass
            sleep(min(1.0, max(0.0, end - monotonic())))
    finally:
        if depth_subscriptions and counts["depth_status"] != "subscribe_failed":
            try:
                feed.unsubscribe_market_depth(depth_subscriptions)
            except Exception:
                if counts["depth_status"] == "recording":
                    counts["depth_status"] = "unsubscribe_failed"
        if equity_subscriptions and counts["equity_status"] != "subscribe_failed":
            try:
                feed.unsubscribe_ltp(equity_subscriptions)
            except Exception:
                if counts["equity_status"] == "recording":
                    counts["equity_status"] = "unsubscribe_failed"
        try:
            feed.unsubscribe_index_value(subscriptions)
        except Exception:
            if counts["status"] == "completed":
                counts["status"] = "unsubscribe_failed"
    if counts["status"] == "completed" and stop_requested():
        counts["status"] = "stopped_by_user"
    if counts["status"] == "completed" and not market_window(now()):
        counts["status"] = "session_ended"
    if counts["status"] in ("completed", "session_ended") and counts["inserted"] == 0:
        counts["status"] = "no_fresh_updates"
    counts["equity_symbols_with_updates"] = len(equity_seen)
    counts["depth_symbols_with_updates"] = len(depth_seen)
    if counts["equity_status"] == "recording":
        counts["equity_status"] = ("no_fresh_updates" if not equity_seen else
                                   "partial_coverage" if len(equity_seen) < len(equity_instruments)
                                   else "completed")
    if counts["depth_status"] == "recording":
        counts["depth_status"] = ("no_fresh_updates" if not depth_seen else
                                  "partial_coverage" if len(depth_seen) < len(depth_instruments)
                                  else "completed")
    return counts


def main():
    parser = argparse.ArgumentParser(description="Record timestamped Bank Nifty index feed updates")
    parser.add_argument("--minutes", type=int, default=5)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "data")
    args = parser.parse_args()
    if not 1 <= args.minutes <= 390:
        parser.error("minutes must be between 1 and 390")
    current_status = regular_market_status(datetime.now(timezone.utc))
    if current_status != "regular_session":
        print(f"NSE regular capture unavailable ({current_status}). No feed connection or token is needed now.")
        return
    try:
        from growwapi import GrowwAPI
    except ImportError:
        print("Groww's Python SDK is needed. Install it with: python -m pip install growwapi")
        return
    token = getpass("Today's Groww Access Token (hidden): ").strip()
    if not token:
        print("No token entered. Nothing was recorded.")
        return
    try:
        instrument = fetch_banknifty_instrument()
        with isolated_groww_feed(GrowwAPI(token)) as feed:
            token = ""
            database = args.data_dir / "market-data.sqlite3"
            result = collect(feed, instrument, database, duration_seconds=args.minutes * 60,
                             on_completed_minute=lambda boundary, generated_at:
                             attempt_boundary_assessment(database, boundary, generated_at))
        print(f"Feed capture: {result['status']}; {result['inserted']} new updates, "
              f"{result['duplicates']} repeats, {result['rejected']} rejected updates; "
              f"{result['paper_logged']} experimental paper scores fixed, {result['paper_errors']} paper errors.")
    except Exception:
        print("Feed capture stopped. Check Groww access and the connection; no orders were placed.")
    finally:
        token = ""


if __name__ == "__main__":
    main()

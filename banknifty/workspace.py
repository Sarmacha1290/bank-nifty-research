"""User-facing readiness and an explicitly synthetic offline dataset."""
from datetime import datetime, timedelta, timezone
import json
from math import sin
from pathlib import Path

from .core import Candle, Session
from .groww import IST


AGENTS = (
    ("Data Quality", "Prototype", "Admit only timestamped, continuous, source-labelled observations.",
     "Existing feed-quality and archive checks reject weak or missing sampled minutes. They have not authenticated a real open-market Groww session."),
    ("Technical", "Prototype", "Price, momentum, trend and volatility.",
     "Fixed rules run on invented history. A quality-gated recorded-feed path and exact-endpoint offline outcome check are built, but real feed capture and historical validation remain pending."),
    ("Fundamentals", "Planned", "Bank results, balance sheets and corporate events.",
     "Needs permitted filings and point-in-time financial data. No analysis engine is connected yet."),
    ("Index Contribution", "Prototype", "Which constituent banks are moving the index.",
     "A separate point-in-time weighted-return calculator runs on invented bank data. An official 30 Sep 2026 full-weight report is saved as dated reference only. Verified interval-valid weights and aligned real bank prices are still required for real contributions."),
    ("Options & Futures", "Prototype", "Open interest, positioning and derivatives prices.",
     "An offline Bank Nifty option-chain parser calculates OI and volume put-call ratios only for complete strike pairs. Groww's documented chain has no exchange timestamp, so freshness and real market interpretation remain unverified. Futures and live chain capture are not yet connected."),
    ("Institutional Flows", "Prototype", "Published institutional and market-flow context.",
     "The app can inspect user-supplied NSE-style FII/FPI and DII cash reports as separate NSE-only and combined market scopes. These delayed, market-wide rows are unverified manual imports, not bank-level orders, a live signal or forecast input. Original publication time remains unknown."),
    ("News", "Planned", "Bank and market news with publication times.",
     "Needs permitted news feeds, source links, deduplication and timestamp checks."),
    ("Macro", "Planned", "Rates, currency and scheduled economic events.",
     "Needs timestamped releases and permitted market-context feeds."),
    ("Risk", "Prototype", "Freshness, session and publication limits.",
     "A deterministic gate now limits experimental paper logging to fresh, verified Groww sampled-feed evidence and same-session horizons. Public forecasts and trading remain blocked; trade sizing is not built."),
    ("Bull", "Prototype", "Observed evidence for an upward move and its contradictions.",
     "A past-only rules view examines the same validated feature snapshot as Bear for 15/30/60 minutes. It lists supporting and opposing observations, with no probability or forecast. Real feed validation is pending."),
    ("Bear", "Prototype", "Observed evidence for a downward move and its contradictions.",
     "A separate past-only rules view uses Bull's exact information cutoff and discloses opposing observations. It abstains on stale input. Real feed validation is pending."),
    ("Verification", "Prototype", "Check timestamps, continuity and evidence identity.",
     "An independent structural check now compares candles, the feature snapshot and all three assessments. It cannot certify the provider's data or predictive skill."),
    ("Evaluation", "Prototype", "Match later exact-endpoint outcomes and compare against simple baselines.",
     "Existing chronological and paper-outcome checkers separate missing labels from misses. No verified intraday validation sample or calibrated forecast model is available."),
    ("Master", "Prototype", "Check one shared evidence bundle and final research gates.",
     "A deterministic research gate checks matching 15/30/60-minute Technical, Bull, Bear, Verification and Risk records. It blocks invented or mismatched inputs. It does not publish a forecast, probability or trading decision."),
)

DEMO_DATASET = "demo-banknifty-2026-10-01-v1"


def synthetic_session():
    """Invented prices for testing storage and charts; never feed a live model."""
    session = Session(
        datetime(2026, 10, 1, 9, 15, tzinfo=IST),
        datetime(2026, 10, 1, 15, 30, tzinfo=IST),
        "synthetic example; not an exchange calendar",
    )
    candles = []
    previous = 50000.0
    for minute in range(180):
        start = session.open + timedelta(minutes=minute)
        close = round(50000 + minute * 0.45 + 48 * sin(minute / 15) + 8 * sin(minute / 3), 2)
        candles.append(Candle(
            symbol="DEMO-BANKNIFTY", kind="index", start=start,
            end=start + timedelta(minutes=1), open=previous,
            high=round(max(previous, close) + 4, 2),
            low=round(min(previous, close) - 4, 2), close=close,
            source="synthetic", volume=None, open_interest=None,
        ))
        previous = close
    return session, candles


def read_connection_status(data_dir):
    """Return an allowlisted summary, never equating an old check with a stream."""
    path = Path(data_dir) / "connection-status.json"
    result = {"state": "not_checked", "title": "Not checked", "checked_at": None,
              "detail": "Check Groww access from Setup. Live streaming is not connected."}
    if not path.exists():
        return result
    try:
        if path.stat().st_size > 16384:
            raise ValueError("Unexpected status size")
        saved = json.loads(path.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(saved["checked_at"])
        if checked.tzinfo is None or checked.utcoffset() is None or checked > datetime.now(timezone.utc):
            raise ValueError("Invalid check timestamp")
        if not isinstance(saved.get("connected"), bool):
            raise ValueError("Invalid status")
        succeeded = saved["connected"]
        result.update(state="passed" if succeeded else "failed",
                      title="Past access check only" if succeeded else "Last check failed",
                      checked_at=checked.astimezone(IST).strftime("%d %b %Y, %H:%M IST"),
                      detail=("Past Groww latest-price access check only. Token cleared; price freshness and live streaming remain unverified."
                              if succeeded else "Repeat the data-access check. Live streaming is not connected."))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        result.update(state="unreadable", title="Check required",
                      detail="Previous status could not be verified. Repeat the Groww data-access check.")
    return result


def read_history_audit(data_dir):
    """Show only validated, credential-free fields from the latest audit."""
    path = Path(data_dir) / "history-audit-latest.json"
    if not path.exists():
        return "No Groww history audit has been run. Use the connection window to inspect a trading day."
    try:
        if path.stat().st_size > 16384:
            raise ValueError("Unexpected audit size")
        saved = json.loads(path.read_text(encoding="utf-8"))
        day = datetime.fromisoformat(saved["session_date"]).date().isoformat()
        count = saved["returned_rows"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("Invalid count")
        allowed = {"no_data", "incomplete_or_irregular", "boundary_ambiguous",
                   "coverage_observed_convention_unverified"}
        status = saved["status"]
        if status not in allowed or saved.get("ready_for_forecast_archive") is not False:
            raise ValueError("Invalid audit")
        return f"Groww history audit {day}: {count} rows, {status.replace('_', ' ')}. Not cleared for forecasts."
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "History audit could not be verified. Run it again before using the data."

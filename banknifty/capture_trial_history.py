"""Durable, credential-free summaries of completed local capture attempts."""
from contextlib import closing
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .groww import IST
from .feed_quality import MIN_SAMPLES_PER_ACCEPTED_MINUTE
from .storage import record_event


EVENT_TYPE = "capture_trial_summary"
_COUNTERS = ("inserted", "duplicates", "rejected", "equity_inserted",
             "equity_rejected", "depth_inserted", "depth_rejected")
_RETRY_COUNTERS = ("archive_busy_retries", "equity_archive_busy_retries",
                   "depth_archive_busy_retries")
_TRIAL_FIELDS = ("saved_updates", "observed_minutes", "accepted_minutes",
                 "too_few_samples", "missing_minute_edge", "internal_gap",
                 "exchange_time_ahead_updates", "median_receipt_lag_seconds",
                 "max_receipt_lag_seconds", "first_exchange_ist", "last_exchange_ist")
_FULL_MINUTE_FIELDS = ("full_minutes", "full_observed_minutes", "full_accepted_minutes")


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def build_capture_trial_summary(started_at, finished_at, result, trial_report,
                                quote_result, *, include_banks=False, include_depth=False,
                                include_activity=False, accepted=False,
                                index_finished_at=None):
    """Keep only reviewed diagnostics; no token, URL, raw exception or market price."""
    if not _aware(started_at) or not _aware(finished_at) or finished_at < started_at:
        raise ValueError("Capture bounds must be ordered and timezone-aware")
    if index_finished_at is not None and (not _aware(index_finished_at) or
            not started_at <= index_finished_at <= finished_at):
        raise ValueError("Index recording end must be within the capture attempt")
    if not isinstance(result, dict) or result.get("status") not in (
            "completed", "session_ended", "stopped_by_user", "no_fresh_updates",
            "unsubscribe_failed", "security_blocked", "subscribe_failed",
            "feed_interrupted", "archive_error",
            "outside_regular_hours", "weekend", "nse_holiday", "calendar_year_unverified",
            "preparation_failed", "preparation_cancelled", "capture_exception"):
        raise ValueError("Capture status is not recognized")
    if not all(isinstance(value, bool) for value in
               (include_banks, include_depth, include_activity, accepted)):
        raise ValueError("Capture flags must be Boolean")
    if trial_report is not None and not isinstance(trial_report, dict):
        raise ValueError("Trial report must be a mapping or absent")
    if quote_result is not None and not isinstance(quote_result, dict):
        raise ValueError("Bank quote result must be a mapping or absent")
    counters = {}
    for key in _COUNTERS:
        value = result.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError("Invalid capture counter")
        counters[key] = value
    # Preserve older event shapes exactly: absence means this measurement did
    # not exist when that review was saved, rather than a measured zero.
    for key in _RETRY_COUNTERS:
        if key in result:
            value = result[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Invalid archive retry counter")
            counters[key] = value
    trial = None
    if trial_report is not None:
        trial = {key: trial_report.get(key) for key in _TRIAL_FIELDS}
        # Older saved reviews have no full-minute fields; preserve their exact
        # schema on read instead of inventing historical measurements.
        trial.update({key: trial_report[key] for key in _FULL_MINUTE_FIELDS
                      if key in trial_report})
        for key in _TRIAL_FIELDS[:7]:
            value = trial[key]
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError("Invalid trial count")
        for key in _TRIAL_FIELDS[7:9]:
            value = trial[key]
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not -86400 <= value <= 86400):
                raise ValueError("Invalid trial lag")
        for key in _TRIAL_FIELDS[9:]:
            value = trial[key]
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("Invalid trial exchange time")
                try:
                    if not _aware(datetime.fromisoformat(value)):
                        raise ValueError("Invalid trial exchange time")
                except ValueError as error:
                    raise ValueError("Invalid trial exchange time") from error
        if any(key in trial for key in _FULL_MINUTE_FIELDS):
            if not all(key in trial for key in _FULL_MINUTE_FIELDS):
                raise ValueError("Incomplete full-minute review")
            full, observed, accepted_full = (trial[key] for key in _FULL_MINUTE_FIELDS)
            if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                   for value in (full, observed, accepted_full)):
                raise ValueError("Invalid full-minute count")
            if (accepted_full > observed or observed > full or
                    (trial["accepted_minutes"] is not None and
                     accepted_full > trial["accepted_minutes"])):
                raise ValueError("Inconsistent full-minute review")
    bank_sweeps = None
    if quote_result is not None:
        if quote_result.get("status") not in ("completed", "stopped", "access_stopped",
                                              "no_data", "activity_error", "archive_error"):
            raise ValueError("Unknown bank quote status")
        bank_sweeps = {"status": quote_result["status"]}
        for key in ("sweeps", "saved", "failures"):
            value = quote_result.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError("Invalid bank quote counter")
            bank_sweeps[key] = value
        summary = quote_result.get("summary")
        bank_sweeps["complete_14_bank_sweep"] = (
            summary.get("complete") if isinstance(summary, dict) and
            isinstance(summary.get("complete"), bool) else None)
    if accepted and (trial is None or not trial.get("accepted_minutes")):
        raise ValueError("Accepted capture requires an accepted current-trial minute")
    if trial is not None and trial["observed_minutes"] is not None and trial["accepted_minutes"] is not None:
        if trial["accepted_minutes"] > trial["observed_minutes"]:
            raise ValueError("Accepted minute count exceeds observed minutes")
    if accepted:
        minimum = trial["accepted_minutes"] * MIN_SAMPLES_PER_ACCEPTED_MINUTE
        if (result["status"] not in ("completed", "session_ended") or
                counters["inserted"] is None or counters["inserted"] < minimum or
                trial["saved_updates"] is None or trial["saved_updates"] < minimum or
                trial["observed_minutes"] is None):
            raise ValueError("Accepted capture has inconsistent index evidence")
        if include_activity and (bank_sweeps is None or
                                 bank_sweeps["status"] not in ("completed", "stopped") or
                                 not bank_sweeps["saved"] or
                                 bank_sweeps["failures"] != 0 or
                                 bank_sweeps["complete_14_bank_sweep"] is not True):
            raise ValueError("Accepted capture has inconsistent bank quote evidence")
        if index_finished_at is not None and (
                trial.get("full_accepted_minutes", 0) < 1 or
                trial["saved_updates"] != counters["inserted"]):
            raise ValueError("Accepted capture has inconsistent complete-minute or archive evidence")
    summary = {
        "kind": "local_capture_diagnostics", "started_at_utc": started_at.astimezone(timezone.utc).isoformat(),
        "finished_at_utc": finished_at.astimezone(timezone.utc).isoformat(),
        "capture_status": result["status"], "accepted_minimal_gate": accepted,
        "requested_bank_prices": include_banks, "requested_bank_depth": include_depth,
        "requested_bank_quotes": include_activity, "index_counters": counters,
        "index_trial": trial, "bank_quote_trial": bank_sweeps,
        "interpretation": "local_capture_only_no_market_freshness_or_forecast_claim",
    }
    if index_finished_at is not None:
        summary["index_finished_at_utc"] = index_finished_at.astimezone(timezone.utc).isoformat()
    return summary


def save_capture_trial_summary(database, summary):
    if not isinstance(summary, dict) or summary.get("kind") != "local_capture_diagnostics":
        raise ValueError("Invalid capture summary")
    # Rebuild from the narrow schema so direct writes cannot add secret fields or
    # change derived fields while retaining superficially plausible metadata.
    started = datetime.fromisoformat(summary["started_at_utc"])
    finished = datetime.fromisoformat(summary["finished_at_utc"])
    raw = {"status": summary["capture_status"], **summary["index_counters"]}
    bank = summary["bank_quote_trial"]
    if bank is not None:
        bank = {**bank, "summary": {"complete": bank["complete_14_bank_sweep"]}}
    rebuilt = build_capture_trial_summary(
        started, finished, raw, summary["index_trial"], bank,
        include_banks=summary["requested_bank_prices"],
        include_depth=summary["requested_bank_depth"],
        include_activity=summary["requested_bank_quotes"],
        accepted=summary["accepted_minimal_gate"],
        index_finished_at=(datetime.fromisoformat(summary["index_finished_at_utc"])
                           if "index_finished_at_utc" in summary else None))
    if rebuilt != summary:
        raise ValueError("Capture summary is inconsistent")
    return record_event(database, EVENT_TYPE, summary)


def load_capture_trial_summaries(database):
    path = Path(database)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='events' AND type='table'").fetchone() is None:
            return []
        rows = connection.execute(
            "SELECT payload, sha256 FROM events WHERE event_type=? ORDER BY recorded_at, id",
            (EVENT_TYPE,)).fetchall()
    result = []
    for payload, digest in rows:
        if sha256(payload.encode()).hexdigest() != digest:
            raise ValueError("Capture record failed its integrity check")
        item = json.loads(payload)
        # Validate on read as well as write; event hashes alone do not prove provenance.
        try:
            bank = item["bank_quote_trial"]
            if bank is not None:
                bank = {**bank, "summary": {"complete": bank["complete_14_bank_sweep"]}}
            build = build_capture_trial_summary(
                datetime.fromisoformat(item["started_at_utc"]),
                datetime.fromisoformat(item["finished_at_utc"]),
                {"status": item["capture_status"], **item["index_counters"]},
                item["index_trial"], bank,
                include_banks=item["requested_bank_prices"],
                include_depth=item["requested_bank_depth"],
                include_activity=item["requested_bank_quotes"],
                accepted=item["accepted_minimal_gate"],
                index_finished_at=(datetime.fromisoformat(item["index_finished_at_utc"])
                                   if "index_finished_at_utc" in item else None))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Capture record has invalid fields") from error
        if build != item:
            raise ValueError("Capture record has inconsistent fields")
        result.append(item)
    return result


def format_capture_trial_summary(item):
    """Plain-language latest-trial report with an explicit evidence limit."""
    if not isinstance(item, dict) or item.get("kind") != "local_capture_diagnostics":
        raise ValueError("Invalid capture trial")
    started = datetime.fromisoformat(item["started_at_utc"])
    trial = item["index_trial"]
    counters = item["index_counters"]
    inserted = counters["inserted"]
    updates_note = f"{inserted} new updates" if inserted is not None else "new-update count unavailable"
    minute_note = (f"{trial['accepted_minutes']}/{trial['observed_minutes']} observed minutes accepted"
                   if trial and trial["accepted_minutes"] is not None and
                   trial["observed_minutes"] is not None else "minute-quality review unavailable")
    full_note = (f"; {trial['full_accepted_minutes']}/{trial['full_minutes']} complete "
                 f"recording-window minutes accepted"
                 if trial and "full_minutes" in trial else "")
    retry_note = (f"; {counters['archive_busy_retries']} index archive-lock retries"
                  if "archive_busy_retries" in counters else "")
    return (
        f"Latest capture {started:%Y-%m-%d %H:%M UTC}: "
        f"{'minimal software gate passed' if item['accepted_minimal_gate'] else 'needs review'}; "
        f"index {item['capture_status']}, {updates_note}, {minute_note}{full_note}{retry_note}. "
        f"Bank prices {'requested' if item['requested_bank_prices'] else 'off'}; "
        f"depth {'requested' if item['requested_bank_depth'] else 'off'}; "
        f"bank quotes {'requested' if item['requested_bank_quotes'] else 'off'}. "
        "This is local capture quality, not market-data authenticity or forecast accuracy."
    )


def export_capture_trial_csv(database, destination):
    """Export validated trial outcomes as one Excel-readable row per attempt."""
    source, target = Path(database), Path(destination)
    if target.resolve() == source.resolve() or (
            source.exists() and target.exists() and source.samefile(target)):
        raise ValueError("Capture review export must not overwrite the archive database")
    saved = load_capture_trial_summaries(source)
    if not saved:
        raise ValueError("No capture reviews have been saved")
    fields = (
        "session_date_ist", "started_at_utc", "index_finished_at_utc",
        "finished_at_utc", "capture_status",
        "minimal_software_gate_passed", "index_updates_inserted", "index_updates_duplicate",
        "index_updates_rejected", "index_archive_busy_retries", "index_trial_saved_updates", "observed_index_minutes",
        "accepted_index_minutes", "too_few_samples_minutes", "missing_edge_minutes",
        "complete_window_minutes", "observed_complete_window_minutes",
        "accepted_complete_window_minutes",
        "internal_gap_minutes", "exchange_time_ahead_updates",
        "median_receipt_lag_seconds", "max_receipt_lag_seconds",
        "first_exchange_time_ist", "last_exchange_time_ist",
        "bank_prices_requested", "bank_price_updates_inserted", "bank_price_updates_rejected",
        "bank_price_archive_busy_retries", "depth_requested", "depth_updates_inserted", "depth_updates_rejected",
        "depth_archive_busy_retries",
        "bank_quotes_requested", "bank_quote_status", "bank_quote_sweeps",
        "bank_quote_rows_saved", "bank_quote_failures", "complete_14_bank_sweep",
        "interpretation",
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", suffix=".csv",
                                         prefix=".banknifty-capture-", dir=target.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for item in saved:
                trial = item["index_trial"] or {}
                counters = item["index_counters"]
                bank = item["bank_quote_trial"] or {}
                yes_no = lambda value: "" if value is None else "yes" if value else "no"
                row = {
                    "session_date_ist": datetime.fromisoformat(item["started_at_utc"]).astimezone(IST).date().isoformat(),
                    "started_at_utc": item["started_at_utc"],
                    "index_finished_at_utc": item.get("index_finished_at_utc"),
                    "finished_at_utc": item["finished_at_utc"],
                    "capture_status": item["capture_status"],
                    "minimal_software_gate_passed": yes_no(item["accepted_minimal_gate"]),
                    "index_updates_inserted": counters["inserted"],
                    "index_updates_duplicate": counters["duplicates"],
                    "index_updates_rejected": counters["rejected"],
                    "index_archive_busy_retries": counters.get("archive_busy_retries"),
                    "index_trial_saved_updates": trial.get("saved_updates"),
                    "observed_index_minutes": trial.get("observed_minutes"),
                    "accepted_index_minutes": trial.get("accepted_minutes"),
                    "complete_window_minutes": trial.get("full_minutes"),
                    "observed_complete_window_minutes": trial.get("full_observed_minutes"),
                    "accepted_complete_window_minutes": trial.get("full_accepted_minutes"),
                    "too_few_samples_minutes": trial.get("too_few_samples"),
                    "missing_edge_minutes": trial.get("missing_minute_edge"),
                    "internal_gap_minutes": trial.get("internal_gap"),
                    "exchange_time_ahead_updates": trial.get("exchange_time_ahead_updates"),
                    "median_receipt_lag_seconds": trial.get("median_receipt_lag_seconds"),
                    "max_receipt_lag_seconds": trial.get("max_receipt_lag_seconds"),
                    "first_exchange_time_ist": trial.get("first_exchange_ist"),
                    "last_exchange_time_ist": trial.get("last_exchange_ist"),
                    "bank_prices_requested": yes_no(item["requested_bank_prices"]),
                    "bank_price_updates_inserted": counters["equity_inserted"],
                    "bank_price_updates_rejected": counters["equity_rejected"],
                    "bank_price_archive_busy_retries": counters.get("equity_archive_busy_retries"),
                    "depth_requested": yes_no(item["requested_bank_depth"]),
                    "depth_updates_inserted": counters["depth_inserted"],
                    "depth_updates_rejected": counters["depth_rejected"],
                    "depth_archive_busy_retries": counters.get("depth_archive_busy_retries"),
                    "bank_quotes_requested": yes_no(item["requested_bank_quotes"]),
                    "bank_quote_status": bank.get("status"),
                    "bank_quote_sweeps": bank.get("sweeps"),
                    "bank_quote_rows_saved": bank.get("saved"),
                    "bank_quote_failures": bank.get("failures"),
                    "complete_14_bank_sweep": yes_no(bank.get("complete_14_bank_sweep")),
                    "interpretation": item["interpretation"],
                }
                writer.writerow(row)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(saved)

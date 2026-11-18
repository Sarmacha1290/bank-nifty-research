"""Independent structural audit for offline sampled-index research rows.

This checks information cutoffs and labels encoded in rows. It cannot prove
that upstream market observations or indicator formulas are authentic.
"""

from datetime import datetime, time, timedelta
from math import isfinite

from .feed_quality import MAX_RESEARCH_ENDPOINT_AGE_SECONDS, SAMPLED_SOURCE
from .groww import IST
from .expiry_context import expiry_features


HORIZONS = frozenset((15, 30, 60))
PARTITIONS = ("development", "validation", "holdout")


def _aware(value, name):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid {name} timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} timestamp lacks timezone")
    return parsed


def audit_research_rows(rows, feature_names):
    """Reject inconsistent origins, cutoffs, targets, labels and date splits."""
    rows = list(rows)
    if not rows:
        return {"checked_rows": 0, "checked_origins": 0,
                "checked_dates": 0, "status": "no_rows"}
    if not feature_names or len(set(feature_names)) != len(feature_names):
        raise ValueError("A unique nonempty feature list is required")
    seen = set()
    by_origin = {}
    by_date = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("source") != SAMPLED_SOURCE:
            raise ValueError("Research row has an unapproved source")
        origin = _aware(row.get("origin_at_utc"), "origin")
        decision = _aware(row.get("decision_at_utc"), "decision")
        target = _aware(row.get("target_at_utc"), "target")
        local = origin.astimezone(IST)
        day = local.date().isoformat()
        if row.get("session_date_ist") != day or not time(10, 16) <= local.time() <= time(15, 30):
            raise ValueError("Research row origin is outside its NSE session date")
        if decision != origin + timedelta(seconds=5):
            raise ValueError("Research decision cutoff is not five seconds after origin")
        event_status = row.get("event_context_status")
        event_counts = ("known_recent_bank_events", "known_recent_macro_events",
                        "known_upcoming_events")
        if row.get("event_context_forecast_eligible") is not False or event_status not in (
                "no_event_source", "unverified_manual_partial_coverage"):
            raise ValueError("Research event context was incorrectly approved")
        if event_status == "no_event_source":
            if any(row.get(name) is not None for name in event_counts):
                raise ValueError("Missing event source was represented as zero activity")
        elif any(type(row.get(name)) is not int or row[name] < 0 for name in event_counts):
            raise ValueError("Invalid staged event counts")
        expiry_fields = (
            "expiry_date", "calendar_days_to_expiry", "is_expiry_day",
            "is_pre_expiry_session", "expiry_reference_observed_at_utc",
            "expiry_reference_source", "expiry_catalog_sha256",
        )
        if row.get("expiry_date") is None:
            if any(row.get(name) is not None for name in expiry_fields):
                raise ValueError("Expiry fields lack an observed contract reference")
        else:
            reference = {
                "expiry_date": row["expiry_date"],
                "observed_at_utc": row.get("expiry_reference_observed_at_utc"),
                "source": row.get("expiry_reference_source"),
                "catalog_sha256": row.get("expiry_catalog_sha256"),
            }
            expected_expiry = expiry_features(decision, (reference,))
            if any(row.get(name) != expected_expiry[name] for name in expiry_fields):
                raise ValueError("Expiry context is inconsistent or learned after cutoff")
        horizon = row.get("horizon_minutes")
        if (type(horizon) is not int or horizon not in HORIZONS or
                target != origin + timedelta(minutes=horizon)):
            raise ValueError("Research target does not match a reviewed horizon")
        if not decision < target:
            raise ValueError("Research target is not later than the decision")
        origin_age = row.get("origin_close_age_seconds")
        if (isinstance(origin_age, bool) or not isinstance(origin_age, (int, float)) or
                not isfinite(origin_age) or not 0 <= origin_age <= MAX_RESEARCH_ENDPOINT_AGE_SECONDS):
            raise ValueError("Research origin has an invalid sampled-price age")
        status = row.get("label_status")
        target_inside_session = target.astimezone(IST).time() <= time(15, 30)
        target_age = row.get("target_close_age_seconds")
        value = row.get("target_return_pct")
        if status == "ready":
            if (not target_inside_session or
                    isinstance(target_age, bool) or not isinstance(target_age, (int, float)) or
                    not isfinite(target_age) or not 0 <= target_age <= MAX_RESEARCH_ENDPOINT_AGE_SECONDS or
                    isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not isfinite(value)):
                raise ValueError("Ready label has an invalid near-end target")
        elif status == "weak_target_edge":
            if (not target_inside_session or value is not None or isinstance(target_age, bool) or
                    not isinstance(target_age, (int, float)) or not isfinite(target_age) or
                    target_age <= MAX_RESEARCH_ENDPOINT_AGE_SECONDS):
                raise ValueError("Weak target edge was mislabeled")
        elif status in ("missing_data", "crosses_session_close"):
            if (value is not None or target_age is not None or
                    (status == "missing_data") != target_inside_session):
                raise ValueError("Unavailable target has a fabricated value")
        else:
            raise ValueError("Unknown research label status")
        partition = row.get("partition")
        if partition not in (*PARTITIONS, "unassigned_too_few_dates"):
            raise ValueError("Unknown research partition")
        if day in by_date and by_date[day] != partition:
            raise ValueError("One session date spans research partitions")
        by_date[day] = partition
        identity = (origin, horizon)
        if identity in seen:
            raise ValueError("Duplicate research origin and horizon")
        seen.add(identity)
        try:
            features = tuple(row[name] for name in feature_names)
        except KeyError as error:
            raise ValueError("Research row is missing a declared feature") from error
        origin_key = (day, origin)
        prior = by_origin.setdefault(origin_key, {"horizons": set(), "features": features,
                                                  "origin_age": origin_age, "partition": partition})
        if (prior["features"] != features or prior["origin_age"] != origin_age or
                prior["partition"] != partition):
            raise ValueError("One origin has horizon-dependent or inconsistent features")
        prior["horizons"].add(horizon)
    if any(item["horizons"] != HORIZONS for item in by_origin.values()):
        raise ValueError("A research origin is missing a reviewed horizon")
    ordered_partitions = [by_date[day] for day in sorted(by_date)]
    if "unassigned_too_few_dates" in ordered_partitions:
        if (len(by_date) >= 5 or
                any(value != "unassigned_too_few_dates" for value in ordered_partitions)):
            raise ValueError("Assigned and unassigned dates are mixed")
    elif (len(by_date) < 5 or set(ordered_partitions) != set(PARTITIONS) or
          ordered_partitions != sorted(ordered_partitions, key=PARTITIONS.index)):
        raise ValueError("Research date partitions are not chronological")
    return {"checked_rows": len(rows), "checked_origins": len(by_origin),
            "checked_dates": len(by_date), "status": "structural_checks_passed_not_source_authentication"}

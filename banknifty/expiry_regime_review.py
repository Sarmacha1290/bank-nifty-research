"""Descriptive, date-separated Bank Nifty expiry movement review.

This measures later observed index movement; it is not a forecast strategy.
Rows must already have passed the independent point-in-time research audit.
Unknown expiry status and unavailable outcome labels are counted but excluded.
"""

from collections import defaultdict
from math import isfinite
from statistics import mean


REGIMES = ("expiry", "pre_expiry", "ordinary")
MIN_DAYS_PER_REGIME = 10


def review_expiry_regimes(report):
    if not isinstance(report, dict) or report.get("structural_audit", {}).get("status") not in (
            "structural_checks_passed_not_source_authentication", "no_rows"):
        raise ValueError("Expiry review requires independently audited research rows")
    rows = report.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Expiry review needs research rows")
    audit = report["structural_audit"]
    if (audit["status"] == "no_rows" and rows) or (
            audit["status"] != "no_rows" and audit.get("checked_rows") != len(rows)):
        raise ValueError("Expiry review row count disagrees with structural audit")
    grouped = defaultdict(list)
    counts = {"unknown_expiry_rows": 0, "unavailable_label_rows": 0}
    for row in rows:
        if (type(row.get("is_expiry_day")) not in (bool, type(None)) or
                type(row.get("is_pre_expiry_session")) not in (bool, type(None)) or
                row.get("horizon_minutes") not in (15, 30, 60)):
            raise ValueError("Expiry review has malformed research flags or horizon")
        if row.get("is_expiry_day") is None:
            counts["unknown_expiry_rows"] += 1
            continue
        if row.get("is_expiry_day") is True:
            regime = "expiry"
        elif row.get("is_pre_expiry_session") is True:
            regime = "pre_expiry"
        else:
            regime = "ordinary"
        if row.get("label_status") != "ready":
            counts["unavailable_label_rows"] += 1
            continue
        value = row.get("target_return_pct")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError("Expiry review has a malformed ready outcome")
        grouped[(row["partition"], row["horizon_minutes"], regime)].append(row)
    summaries = []
    for (partition, horizon, regime), samples in sorted(grouped.items()):
        days = {row["session_date_ist"] for row in samples}
        returns = [row["target_return_pct"] for row in samples]
        summaries.append({
            "partition": partition, "horizon_minutes": horizon,
            "regime": regime, "session_days": len(days), "ready_observations": len(samples),
            "mean_return_pct": mean(returns),
            "mean_absolute_move_pct": mean(abs(value) for value in returns),
            "up_fraction": sum(value > 0 for value in returns) / len(returns),
            "evidence_status": ("descriptive_only_sufficient_dates_for_review"
                                if len(days) >= MIN_DAYS_PER_REGIME
                                else "insufficient_dates_no_comparison_claim"),
        })
    return {**counts, "summary": summaries,
            "interpretation": "observed_sampled_index_moves_only_not_expiry_effect_or_prediction"}

"""Fail-closed inventory for honest Bank Nifty model development."""
from .official_daily import load_official_days
from .feed_quality_report import summarize_recorded_feed
from .tick_history import count_index_ticks


def assess_training_readiness(database):
    daily = load_official_days(database)
    sessions = summarize_recorded_feed(database)
    complete = sum(row["accepted_minutes"] == 375 and
                   row["missing_whole_minutes"] == 0 and
                   row["rejected_minutes"] == 0 for row in sessions)
    ticks = count_index_ticks(database)
    return {
        "nse_daily_rows": len(daily),
        "groww_index_ticks": ticks,
        "complete_sampled_sessions": complete,
        "intraday_model_ready": False,
        "daily_range_model_ready": False,
        "trained_forecast_models": 0,
        "reason": ("Daily NSE OHLC has no point-in-time 15/30/60-minute features or exact "
                   "intraday labels. A recorded session needs quality review and a "
                   "chronological out-of-sample evaluation before model release."),
    }

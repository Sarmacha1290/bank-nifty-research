"""Exploratory changes in visible five-level order-book quantities."""
from datetime import timedelta, timezone
from statistics import median

IST = timezone(timedelta(hours=5, minutes=30))


def detect_depth_spikes(ticks, *, min_baseline=5, multiple=2.0, imbalance_min=0.6):
    """Use only prior same-day nearby books; never infer completed trade side."""
    if min_baseline < 3 or multiple <= 1:
        raise ValueError("Conservative trial thresholds required")
    rows = sorted(tuple(ticks), key=lambda tick: (tick.symbol, tick.exchange_at))
    alerts = []
    for symbol in sorted({row.symbol for row in rows}):
        series = [row for row in rows if row.symbol == symbol]
        if len(series) < min_baseline + 1:
            continue
        latest = series[-1]
        day = latest.exchange_at.astimezone(IST).date()
        series = [row for row in series if row.exchange_at.astimezone(IST).date() == day]
        if len(series) < min_baseline + 1:
            continue
        history = series[-min_baseline-1:-1]
        if any(not 0 < (right.exchange_at - left.exchange_at).total_seconds() <= 5
               for left, right in zip(history, history[1:] + [latest])):
            continue
        for side, other in (("buy", "sell"), ("sell", "buy")):
            field = f"{side}_quantity"
            current = getattr(latest, field)
            opposite = getattr(latest, f"{other}_quantity")
            previous = [getattr(row, field) for row in history]
            if current is None or opposite is None or any(value is None for value in previous):
                continue
            baseline = median(previous)
            dominance = ((current - opposite) / (current + opposite)
                         if current + opposite > 0 else 0)
            if baseline > 0 and current >= baseline * multiple and dominance >= imbalance_min:
                alerts.append({"symbol": symbol, "kind": f"top5_{side}_depth_surge",
                               "at": latest.exchange_at.isoformat(), "current": current,
                               "baseline": baseline, "multiple": current / baseline,
                               "meaning": f"Visible top-five {side} orders rose; executed {side} trades unknown"})
    return alerts

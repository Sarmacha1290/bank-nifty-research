"""Display-only OHLC aggregation; never fill missing intraday minutes."""

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

from .core import Candle


IST = timezone(timedelta(hours=5, minutes=30))
INTERVALS = (1, 5, 15, 30, 60)


def completed_chart_bars(candles, minutes):
    """Build session-anchored bars only when every source minute is present.

    These are aggregates of saved candles, not independently sourced exchange
    bars. They must not be treated as official OHLC or model input by default.
    """
    if type(minutes) is not int or minutes not in INTERVALS:
        raise ValueError("Unsupported chart interval")
    rows = list(candles)
    if not rows:
        return []
    if any(not isinstance(row, Candle) for row in rows):
        raise ValueError("Chart rows must be candles")
    identity = {(row.symbol, row.kind, row.source) for row in rows}
    if len(identity) != 1:
        raise ValueError("Chart rows cannot mix instruments or sources")
    groups = defaultdict(list)
    for row in rows:
        if row.end - row.start != timedelta(minutes=1):
            raise ValueError("Chart aggregation requires one-minute inputs")
        local = row.start.astimezone(IST)
        anchor = datetime.combine(local.date(), time(9, 15), IST)
        offset = int((local - anchor).total_seconds() / 60)
        if not 0 <= offset < 375:
            continue
        slot = offset // minutes
        groups[(local.date(), slot)].append(row)
    result = []
    for (_day, _slot), group in sorted(groups.items()):
        group.sort(key=lambda row: row.start)
        if len(group) != minutes or any(left.end != right.start for left, right in zip(group, group[1:])):
            continue
        first, last = group[0], group[-1]
        local = first.start.astimezone(IST)
        offset = int((local - datetime.combine(local.date(), time(9, 15), IST)).total_seconds() / 60)
        if offset % minutes or last.end - first.start != timedelta(minutes=minutes):
            continue
        volume = sum(row.volume for row in group) if all(row.volume is not None for row in group) else None
        result.append(Candle(first.symbol, first.kind, first.start, last.end,
                             first.open, max(row.high for row in group),
                             min(row.low for row in group), last.close, first.source,
                             volume=volume))
    return result

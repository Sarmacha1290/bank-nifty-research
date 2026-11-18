"""Offline alpha arithmetic on verified, matched, net paper returns.

This module cannot turn a spot-direction hit rate into trading alpha. Inputs
must already represent an executable instrument and include transaction costs.
It never generates or backfills a strategy's trade decisions.
"""

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from statistics import mean

from .market_calendar import regular_session


MIN_EVALUATION_DAYS = 20


@dataclass(frozen=True)
class MatchedPaperReturn:
    session_date: date
    decided_at: datetime
    realized_at: datetime
    strategy_net_return_pct: float
    benchmark_net_return_pct: float
    source_status: str


def evaluate_paper_alpha(rows):
    """Return period alpha and beta only for 20+ verified forward dates.

    The regression intercept is per matched trading-day observation; it is not
    annualized and does not imply statistical significance or future skill.
    """
    observations = list(rows)
    if len(observations) < MIN_EVALUATION_DAYS:
        raise ValueError("Alpha requires at least 20 matched forward paper days")
    if any(not isinstance(row, MatchedPaperReturn) or
           row.source_status != "verified_forward_paper" for row in observations):
        raise ValueError("Alpha requires verified forward paper returns")
    if len({row.session_date for row in observations}) != len(observations):
        raise ValueError("Alpha requires one matched observation per market date")
    for row in observations:
        if (not isinstance(row.session_date, date) or isinstance(row.session_date, datetime) or
                not isinstance(row.decided_at, datetime) or
                not isinstance(row.realized_at, datetime) or
                row.decided_at.tzinfo is None or row.decided_at.utcoffset() is None or
                row.realized_at.tzinfo is None or row.realized_at.utcoffset() is None or
                row.decided_at >= row.realized_at):
            raise ValueError("Alpha timestamps or session date are invalid")
        session = regular_session(row.session_date)
        if not session.open <= row.decided_at < row.realized_at <= session.close:
            raise ValueError("Alpha returns must cover a reviewed intraday session window")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
               not isfinite(value) or value <= -100
               for value in (row.strategy_net_return_pct, row.benchmark_net_return_pct)):
            raise ValueError("Alpha returns must be finite net percentages")
    strategy = [row.strategy_net_return_pct for row in observations]
    benchmark = [row.benchmark_net_return_pct for row in observations]
    benchmark_average = mean(benchmark)
    variance = mean((value - benchmark_average) ** 2 for value in benchmark)
    if variance == 0:
        raise ValueError("Benchmark has no variation; regression alpha is undefined")
    covariance = mean((x - benchmark_average) * (y - mean(strategy))
                      for x, y in zip(benchmark, strategy))
    beta = covariance / variance
    intercept = mean(strategy) - beta * benchmark_average
    excess = mean(y - x for y, x in zip(strategy, benchmark))
    if not all(isfinite(value) for value in (beta, intercept, excess)):
        raise ValueError("Alpha calculation overflowed")
    return {"days": len(observations), "beta": beta,
            "regression_alpha_pct_per_day": intercept,
            "mean_excess_return_pct_per_day": excess,
            "first_day": min(row.session_date for row in observations).isoformat(),
            "last_day": max(row.session_date for row in observations).isoformat(),
            "interpretation": "descriptive_forward_paper_net_returns_not_significance_or_live_alpha"}

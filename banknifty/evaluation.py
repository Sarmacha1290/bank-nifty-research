"""Offline outcome matching for research and paper evaluation only.

Future candles enter this module after an assessment is fixed. The module does
not fit a model, trade, or treat a rules score as a return prediction.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from .agents.luna import HORIZONS, LunaAssessment
from .core import Session, label_returns


@dataclass(frozen=True)
class Outcome:
    horizon_minutes: int
    as_of: datetime
    target_at: datetime
    assessment_bias: str
    assessment_score: float | None
    status: str
    realized_return_pct: float | None
    realized_direction: str | None
    direction_hit: bool | None


def match_outcomes(assessments, future_candles, session):
    """Match fixed 15/30/60-minute assessments to exact later candle endpoints."""
    records = list(assessments)
    if not records:
        return []
    if not isinstance(session, Session):
        raise ValueError("An explicit verified session is required")
    if any(not isinstance(item, LunaAssessment) for item in records):
        raise ValueError("Expected fixed Technical Agent assessments")
    origins = {item.as_of.astimezone(timezone.utc) for item in records}
    if len(origins) != 1:
        raise ValueError("Assessments must have one shared origin")
    if len({item.horizon_minutes for item in records}) != len(records):
        raise ValueError("Duplicate assessment horizon")
    if len({(item.symbol, item.source) for item in records}) != 1:
        raise ValueError("Assessments must share one instrument and source")
    origin = records[0].as_of
    if any(item.generated_at != item.as_of for item in records):
        raise ValueError("Offline assessments must be fixed at their origin")
    if any(item.horizon_minutes not in HORIZONS for item in records):
        raise ValueError("Unsupported assessment horizon")
    candles = list(future_candles)
    if any(candle.symbol != records[0].symbol or candle.source != records[0].source
           for candle in candles):
        raise ValueError("Outcome candles must match assessment instrument and source")
    labels = {row["horizon_minutes"]: row for row in
              label_returns(candles, origin, session,
                            (item.horizon_minutes for item in records))}
    results = []
    for assessment in records:
        label = labels[assessment.horizon_minutes]
        value = label["return_pct"]
        direction = None if value is None else "up" if value > 0 else "down" if value < 0 else "flat"
        hit = None
        if value is not None and assessment.bias in ("bullish", "bearish"):
            hit = (direction == "up") if assessment.bias == "bullish" else (direction == "down")
        results.append(Outcome(assessment.horizon_minutes, origin,
                               datetime.fromisoformat(label["target_at"]), assessment.bias,
                               assessment.score, label["status"], value, direction, hit))
    return results


def summarize_outcomes(outcomes):
    """Report coverage and directional hit rate; neutral calls are abstentions."""
    rows = list(outcomes)
    if any(not isinstance(row, Outcome) for row in rows):
        raise ValueError("Expected outcome records")
    by_horizon = {}
    for horizon in sorted({row.horizon_minutes for row in rows}):
        subset = [row for row in rows if row.horizon_minutes == horizon]
        labelled = [row for row in subset if row.status == "ready"]
        calls = [row for row in labelled if row.direction_hit is not None]
        by_horizon[horizon] = {
            "total": len(subset),
            "labelled": len(labelled),
            "directional_calls": len(calls),
            "directional_hits": sum(row.direction_hit for row in calls),
            "label_coverage": len(labelled) / len(subset),
            "directional_hit_rate": (sum(row.direction_hit for row in calls) / len(calls)) if calls else None,
        }
    return by_horizon

"""Technical Agent: a transparent, deterministic intraday interpretation role.

This role reads validated features only. It does not fetch data, call an AI model,
estimate a calibrated probability, place an order, or manufacture missing
values. The rules are deliberately explicit so every score can be audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from numbers import Real
from typing import Any, Literal


HORIZONS = (15, 30, 60)
TECHNICAL_FORMULA_VERSION = "technical-rules-v0.2"
Bias = Literal["bullish", "bearish", "neutral", "unavailable"]


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


def _finite(value: Any, name: str, *, minimum: float | None = None,
            maximum: float | None = None, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(float(value)):
        raise ValueError(f"{name} must be finite and numeric")
    number = float(value)
    if positive and number <= 0:
        raise ValueError(f"{name} must be positive")
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return number


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class FeatureSnapshot:
    """Features known at one information cutoff.

    Returns and distances are percentage points (for example ``0.25`` means
    +0.25%). ``trend_strength`` follows a 0–100 ADX-like scale. The provider
    timestamp and source are supplied by the data pipeline, not this role.
    """

    as_of: datetime
    symbol: str
    current_price: float
    return_5m_pct: float
    return_15m_pct: float
    return_30m_pct: float
    return_60m_pct: float
    vwap_distance_pct: float | None
    ema_fast_distance_pct: float
    ema_slow_distance_pct: float
    rsi_14: float
    atr_pct: float
    trend_strength: float
    data_freshness_seconds: float
    source: str
    volume_zscore: float | None = None

    def __post_init__(self) -> None:
        _aware(self.as_of, "as_of")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a nonempty string")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a nonempty string")
        _finite(self.current_price, "current_price", positive=True)
        for field in (
            "return_5m_pct", "return_15m_pct", "return_30m_pct",
            "return_60m_pct", "ema_fast_distance_pct",
            "ema_slow_distance_pct",
        ):
            _finite(getattr(self, field), field)
        if self.vwap_distance_pct is not None:
            _finite(self.vwap_distance_pct, "vwap_distance_pct")
        _finite(self.rsi_14, "rsi_14", minimum=0, maximum=100)
        _finite(self.atr_pct, "atr_pct", minimum=0)
        _finite(self.trend_strength, "trend_strength", minimum=0, maximum=100)
        _finite(self.data_freshness_seconds, "data_freshness_seconds", minimum=0)
        if self.volume_zscore is not None:
            _finite(self.volume_zscore, "volume_zscore")

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "symbol": self.symbol,
            "current_price": self.current_price,
            "return_5m_pct": self.return_5m_pct,
            "return_15m_pct": self.return_15m_pct,
            "return_30m_pct": self.return_30m_pct,
            "return_60m_pct": self.return_60m_pct,
            "vwap_distance_pct": self.vwap_distance_pct,
            "ema_fast_distance_pct": self.ema_fast_distance_pct,
            "ema_slow_distance_pct": self.ema_slow_distance_pct,
            "rsi_14": self.rsi_14,
            "atr_pct": self.atr_pct,
            "trend_strength": self.trend_strength,
            "data_freshness_seconds": self.data_freshness_seconds,
            "source": self.source,
            "volume_zscore": self.volume_zscore,
        }


@dataclass(frozen=True)
class LunaAssessment:
    horizon_minutes: int
    bias: Bias
    score: float | None
    confidence: None
    as_of: datetime
    generated_at: datetime
    symbol: str
    source: str
    reasons: tuple[str, ...]
    risks: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    formula_version: str = TECHNICAL_FORMULA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": "Technical",
            "formula_version": self.formula_version,
            "horizon_minutes": self.horizon_minutes,
            "bias": self.bias,
            "score": self.score,
            "confidence": self.confidence,
            "as_of": self.as_of.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "symbol": self.symbol,
            "source": self.source,
            "reasons": list(self.reasons),
            "risks": list(self.risks),
            "invalidation_conditions": list(self.invalidation_conditions),
        }


def _unavailable(snapshot: FeatureSnapshot, horizon: int, reason: str,
                 generated_at: datetime) -> LunaAssessment:
    return LunaAssessment(horizon, "unavailable", None, None, snapshot.as_of,
                          generated_at, snapshot.symbol, snapshot.source, (), (reason,),
                          ("Obtain a complete, fresh validated snapshot before interpreting.",))


def assess(snapshot: FeatureSnapshot, horizon_minutes: int,
           *, generated_at: datetime | None = None) -> LunaAssessment:
    """Apply auditable technical rules to one snapshot and one horizon."""
    if not isinstance(snapshot, FeatureSnapshot):
        raise TypeError("snapshot must be a FeatureSnapshot")
    if isinstance(horizon_minutes, bool) or horizon_minutes not in HORIZONS:
        raise ValueError("horizon_minutes must be 15, 30, or 60")
    generated = snapshot.as_of if generated_at is None else _aware(generated_at, "generated_at")
    if generated < snapshot.as_of:
        raise ValueError("generated_at cannot precede as_of")
    if snapshot.data_freshness_seconds > 120:
        return _unavailable(snapshot, horizon_minutes, "Input data is older than 120 seconds.", generated)

    horizon_return = getattr(snapshot, f"return_{horizon_minutes}m_pct")
    contributions: list[tuple[str, float]] = []

    # All multipliers are fixed and visible. They are an interpretation score,
    # not a probability and must be validated against historical outcomes later.
    contributions.append((f"{horizon_minutes}m return", _clamp(horizon_return * 20.0, -20.0, 20.0)))
    contributions.append(("5m momentum", _clamp(snapshot.return_5m_pct * 20.0, -10.0, 10.0)))
    if snapshot.vwap_distance_pct is not None:
        contributions.append(("VWAP distance", _clamp(snapshot.vwap_distance_pct * 15.0, -15.0, 15.0)))
    contributions.append(("fast EMA distance", _clamp(snapshot.ema_fast_distance_pct * 10.0, -10.0, 10.0)))
    contributions.append(("slow EMA distance", _clamp(snapshot.ema_slow_distance_pct * 10.0, -10.0, 10.0)))

    directional = horizon_return + snapshot.return_5m_pct + (snapshot.vwap_distance_pct or 0.0)
    direction = 1 if directional > 0 else -1 if directional < 0 else 0
    if direction:
        contributions.append(("trend strength", direction * min(10.0, snapshot.trend_strength / 10.0)))
    else:
        contributions.append(("trend strength", 0.0))

    rsi_adjustment = -8.0 if snapshot.rsi_14 > 70 else 8.0 if snapshot.rsi_14 < 30 else 0.0
    contributions.append(("RSI exhaustion", rsi_adjustment))
    if snapshot.volume_zscore is not None and direction:
        contributions.append(("volume confirmation", direction * min(5.0, abs(snapshot.volume_zscore) * 2.0)))

    score = round(_clamp(sum(value for _, value in contributions), -100.0, 100.0), 4)
    bias: Bias = "bullish" if score >= 20 else "bearish" if score <= -20 else "neutral"
    reasons = tuple(f"{name}: {value:+.2f}" for name, value in contributions if abs(value) >= 0.01)
    risks: list[str] = []
    if snapshot.vwap_distance_pct is None:
        risks.append("VWAP unavailable: no valid traded volume for this instrument")
    if snapshot.rsi_14 > 70:
        risks.append(f"RSI is overbought ({snapshot.rsi_14:.1f})")
    elif snapshot.rsi_14 < 30:
        risks.append(f"RSI is oversold ({snapshot.rsi_14:.1f})")
    if snapshot.atr_pct >= 2.0:
        risks.append(f"ATR is elevated ({snapshot.atr_pct:.2f}%)")
    if snapshot.trend_strength < 20:
        risks.append(f"Trend strength is weak ({snapshot.trend_strength:.1f}/100)")
    if snapshot.return_5m_pct * horizon_return < 0:
        risks.append("Short-term and horizon momentum disagree")
    reference = "VWAP and fast EMA" if snapshot.vwap_distance_pct is not None else "fast EMA"
    invalidation = (
        (f"Bullish bias: close below {reference} with negative horizon momentum.",)
        if bias == "bullish" else
        (f"Bearish bias: close above {reference} with positive horizon momentum.",)
        if bias == "bearish" else
        ("Neutral bias: a sustained break with confirming momentum would change the assessment.",)
    )
    return LunaAssessment(horizon_minutes, bias, score, None, snapshot.as_of,
                          generated, snapshot.symbol, snapshot.source, reasons, tuple(risks), invalidation)

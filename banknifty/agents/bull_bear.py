"""Two opposing, past-only evidence views on one technical feature snapshot.

These cases describe observed conditions. Neither role predicts a return,
assigns a probability, or has authority to place an order.
"""
from dataclasses import dataclass
from datetime import datetime

from .luna import FeatureSnapshot, HORIZONS


FORMULA_VERSION = "bull-bear-evidence-v0.1"


@dataclass(frozen=True)
class DirectionCase:
    agent: str
    horizon_minutes: int
    as_of: datetime
    symbol: str
    source: str
    status: str
    supporting_observations: tuple[str, ...]
    opposing_observations: tuple[str, ...]
    cautions: tuple[str, ...]
    formula_version: str = FORMULA_VERSION


def _case(snapshot, horizon_minutes, agent):
    if not isinstance(snapshot, FeatureSnapshot):
        raise TypeError("A validated feature snapshot is required")
    if isinstance(horizon_minutes, bool) or horizon_minutes not in HORIZONS:
        raise ValueError("Horizon must be 15, 30 or 60 minutes")
    if agent not in ("Bull", "Bear"):
        raise ValueError("Agent must be Bull or Bear")
    if snapshot.data_freshness_seconds > 120:
        return DirectionCase(agent, horizon_minutes, snapshot.as_of, snapshot.symbol,
                             snapshot.source, "unavailable", (), (),
                             ("Input is older than 120 seconds",))

    observations = []
    directional = (
        (f"Past {horizon_minutes}m return {getattr(snapshot, f'return_{horizon_minutes}m_pct'):+.3f}%",
         getattr(snapshot, f"return_{horizon_minutes}m_pct"), 0.05),
        (f"Past 5m return {snapshot.return_5m_pct:+.3f}%", snapshot.return_5m_pct, 0.03),
        (f"Fast EMA distance {snapshot.ema_fast_distance_pct:+.3f}%",
         snapshot.ema_fast_distance_pct, 0.03),
        (f"Slow EMA distance {snapshot.ema_slow_distance_pct:+.3f}%",
         snapshot.ema_slow_distance_pct, 0.03),
    )
    for label, value, threshold in directional:
        if value >= threshold:
            observations.append(("Bull", label))
        elif value <= -threshold:
            observations.append(("Bear", label))
    support = tuple(label for side, label in observations if side == agent)
    opposing = tuple(label for side, label in observations if side != agent)
    cautions = ["Past-only observations; trial thresholds are not calibrated"]
    if snapshot.trend_strength < 20:
        cautions.append(f"Weak measured trend ({snapshot.trend_strength:.1f}/100)")
    if snapshot.rsi_14 >= 70:
        cautions.append(f"RSI {snapshot.rsi_14:.1f}: upside may be stretched")
    elif snapshot.rsi_14 <= 30:
        cautions.append(f"RSI {snapshot.rsi_14:.1f}: downside may be stretched")
    if snapshot.source == "synthetic":
        cautions.append("Input prices are invented")
    return DirectionCase(agent, horizon_minutes, snapshot.as_of, snapshot.symbol,
                         snapshot.source, "descriptive", support, opposing,
                         tuple(cautions))


def build_bull_case(snapshot, horizon_minutes):
    return _case(snapshot, horizon_minutes, "Bull")


def build_bear_case(snapshot, horizon_minutes):
    return _case(snapshot, horizon_minutes, "Bear")

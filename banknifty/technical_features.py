"""Point-in-time technical features from validated Bank Nifty index candles.

Only candles completed by ``as_of`` enter a snapshot. These calculations are
deterministic; no fitted forecast or probability is implied. Spot-index volume
is unavailable, so VWAP and volume confirmation remain absent.
"""

from datetime import datetime, timezone
from math import isfinite

from .agents.luna import FeatureSnapshot
from .core import validate_series


def _ema(values, period):
    result = sum(values[:period]) / period
    multiplier = 2.0 / (period + 1)
    for value in values[period:]:
        result += multiplier * (value - result)
    return result


def _rsi(closes, period=14):
    moves = [current - previous for previous, current in zip(closes, closes[1:])]
    gain = sum(max(move, 0.0) for move in moves[:period]) / period
    loss = sum(max(-move, 0.0) for move in moves[:period]) / period
    for move in moves[period:]:
        gain = (gain * (period - 1) + max(move, 0.0)) / period
        loss = (loss * (period - 1) + max(-move, 0.0)) / period
    if gain == 0 and loss == 0:
        return 50.0
    if loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def _range_and_adx(candles, period=14):
    tr, positive, negative = [], [], []
    for previous, current in zip(candles, candles[1:]):
        tr.append(max(current.high - current.low,
                      abs(current.high - previous.close),
                      abs(current.low - previous.close)))
        up = current.high - previous.high
        down = previous.low - current.low
        positive.append(up if up > down and up > 0 else 0.0)
        negative.append(down if down > up and down > 0 else 0.0)
    average_tr = sum(tr[:period]) / period
    smooth_tr = sum(tr[:period])
    smooth_positive = sum(positive[:period])
    smooth_negative = sum(negative[:period])
    dx_values = []
    for index in range(period - 1, len(tr)):
        if index >= period:
            average_tr = (average_tr * (period - 1) + tr[index]) / period
            smooth_tr = smooth_tr - smooth_tr / period + tr[index]
            smooth_positive = smooth_positive - smooth_positive / period + positive[index]
            smooth_negative = smooth_negative - smooth_negative / period + negative[index]
        plus_di = 100.0 * smooth_positive / smooth_tr if smooth_tr else 0.0
        minus_di = 100.0 * smooth_negative / smooth_tr if smooth_tr else 0.0
        denominator = plus_di + minus_di
        dx_values.append(100.0 * abs(plus_di - minus_di) / denominator if denominator else 0.0)
    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period
    return average_tr, adx


def build_technical_snapshot(candles, as_of, *, generated_at=None):
    """Build one 15/30/60-minute evidence snapshot without future leakage.

    Requires 61 continuous completed one-minute index candles within one
    supplied session slice. For offline replay pass ``generated_at=as_of``;
    for live use, pass the actual generation time so stale data is blocked.
    """
    checked = validate_series(list(candles), as_of, expected_minutes=1)
    if len(checked) < 61:
        raise ValueError("At least 61 continuous completed candles are required")
    if checked[0].kind != "index":
        raise ValueError("Technical snapshot currently supports only the spot index")
    if checked[-1].end != as_of:
        raise ValueError("Latest candle must end exactly at as_of")
    if generated_at is None:
        generated_at = datetime.now(timezone.utc)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None or generated_at < as_of:
        raise ValueError("generated_at must be aware and cannot precede as_of")

    closes = [float(c.close) for c in checked]
    current = closes[-1]
    ema_fast = _ema(closes, 12)
    ema_slow = _ema(closes, 26)
    atr, adx = _range_and_adx(checked)
    values = (ema_fast, ema_slow, atr, adx)
    if not all(isfinite(value) for value in values):
        raise ValueError("Technical calculation overflowed")
    return FeatureSnapshot(
        as_of=as_of, symbol=checked[0].symbol, current_price=current,
        return_5m_pct=100.0 * (current / closes[-6] - 1.0),
        return_15m_pct=100.0 * (current / closes[-16] - 1.0),
        return_30m_pct=100.0 * (current / closes[-31] - 1.0),
        return_60m_pct=100.0 * (current / closes[-61] - 1.0),
        vwap_distance_pct=None,
        ema_fast_distance_pct=100.0 * (current / ema_fast - 1.0),
        ema_slow_distance_pct=100.0 * (current / ema_slow - 1.0),
        rsi_14=_rsi(closes), atr_pct=100.0 * atr / current,
        trend_strength=adx,
        data_freshness_seconds=(generated_at - as_of).total_seconds(),
        source=checked[0].source, volume_zscore=None,
    )

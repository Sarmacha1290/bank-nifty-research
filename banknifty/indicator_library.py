"""Point-in-time descriptive indicators; no fitted signal or forecast.

Only completed, continuous candles at or before ``as_of`` are admissible.
Missing volume/previous-session inputs produce None, never substitute index
volume or a later daily high/low. Percent values are percentage points.
"""

from datetime import datetime, timedelta
from math import isfinite, log, sqrt
from statistics import mean, pstdev

from .core import Candle, validate_series


VERSION = "descriptive-indicators-v0.1"
RETURN_WINDOWS = (1, 3, 5, 10, 15, 30, 60)


def _ema_series(values, period):
    if len(values) < period:
        return []
    ema = mean(values[:period])
    result = [ema]
    weight = 2 / (period + 1)
    for value in values[period:]:
        ema += weight * (value - ema)
        result.append(ema)
    return result


def _wilder_atr(candles, period):
    ranges = [max(current.high - current.low,
                  abs(current.high - previous.close),
                  abs(current.low - previous.close))
              for previous, current in zip(candles, candles[1:])]
    if len(ranges) < period:
        return None
    value = mean(ranges[:period])
    for item in ranges[period:]:
        value = (value * (period - 1) + item) / period
    return value


def _confirmed_swings(candles, *, radius=2):
    """A pivot is known only after the two following candles have closed."""
    highs, lows = [], []
    for index in range(radius, len(candles) - radius):
        window = candles[index - radius:index + radius + 1]
        item = candles[index]
        if item.high > max(row.high for row in window if row is not item):
            highs.append(item.high)
        if item.low < min(row.low for row in window if row is not item):
            lows.append(item.low)
    return (highs[-1] if highs else None, lows[-1] if lows else None)


def calculate_price_indicators(candles, as_of, *, session_open=None,
                               previous_session=None, opening_range_minutes=15):
    """Calculate complete-candle features with explicit availability.

    ``previous_session`` is a completed Candle supplied by the caller and must
    end before the current session opens. It is never looked up from the future.
    Session-to-date and opening-range values require uninterrupted data from
    the supplied session opening; a 61-minute rolling slice is insufficient.
    """
    rows = validate_series(list(candles), as_of, expected_minutes=1)
    if len(rows) < 61 or rows[-1].end != as_of:
        raise ValueError("At least 61 continuous completed minutes ending at as_of are required")
    if isinstance(opening_range_minutes, bool) or not isinstance(opening_range_minutes, int) or opening_range_minutes < 1:
        raise ValueError("Invalid opening range")
    if session_open is not None:
        if (not isinstance(session_open, datetime) or session_open.tzinfo is None or
                session_open.utcoffset() is None or session_open > rows[0].start):
            raise ValueError("Full session candles must begin at session_open")
        if rows[0].start != session_open:
            raise ValueError("Session-to-date indicators require full opening coverage")
    if previous_session is not None:
        if (not isinstance(previous_session, Candle) or
                previous_session.symbol != rows[0].symbol or
                previous_session.kind != rows[0].kind or
                session_open is None or previous_session.end > session_open):
            raise ValueError("Previous session must be completed before the covered session")
    close = [float(row.close) for row in rows]
    current = close[-1]
    last = rows[-1]
    values = {}
    for period in RETURN_WINDOWS:
        values[f"return_{period}m_pct"] = 100 * (current / close[-period - 1] - 1)
        values[f"log_return_{period}m_pct"] = 100 * log(current / close[-period - 1])
    for period in (5, 15, 30, 60):
        movement = close[-period - 1:]
        path = sum(abs(right - left) for left, right in zip(movement, movement[1:]))
        values[f"price_change_{period}m_points"] = current - movement[0]
        values[f"movement_efficiency_{period}m_pct"] = (
            100 * abs(current - movement[0]) / path if path else None)
    values["price_acceleration_5m_points"] = (
        current - close[-6]) - (close[-6] - close[-11])
    for period in (5, 12, 20, 26, 60):
        sma = mean(close[-period:])
        ema = _ema_series(close, period)[-1]
        values[f"sma_{period}"] = sma
        values[f"ema_{period}"] = ema
        values[f"sma_{period}_distance_pct"] = 100 * (current / sma - 1)
        values[f"ema_{period}_distance_pct"] = 100 * (current / ema - 1)
    fast, slow = _ema_series(close, 12), _ema_series(close, 26)
    # Align the two EMA streams on their actual starting indices.
    macd_series = [fast[index + 14] - slow[index] for index in range(len(slow))]
    signal = _ema_series(macd_series, 9)[-1]
    values.update(macd=macd_series[-1], macd_signal=signal,
                  macd_histogram=macd_series[-1] - signal)
    for period in (14, 28):
        values[f"atr_{period}"] = _wilder_atr(rows, period)
    for period in (20, 60):
        recent = rows[-period:]
        high, low = max(row.high for row in recent), min(row.low for row in recent)
        values[f"rolling_{period}_high"] = high
        values[f"rolling_{period}_low"] = low
        values[f"rolling_{period}_range"] = high - low
        values[f"rolling_{period}_midpoint"] = (high + low) / 2
        values[f"rolling_{period}_location_pct"] = 100 * (current - low) / (high - low) if high > low else 50.0
        returns = [log(right / left) for left, right in zip(close[-period - 1:-1], close[-period:])]
        values[f"realized_vol_{period}_per_minute_pct"] = 100 * pstdev(returns)
    historical_vol = []
    for end in range(21, len(close) + 1):
        segment = close[end - 21:end]
        historical_vol.append(100 * pstdev(
            [log(right / left) for left, right in zip(segment, segment[1:])]))
    prior_vol = historical_vol[:-1]
    current_vol = historical_vol[-1]
    vol_rank = (sum(value <= current_vol for value in prior_vol) / len(prior_vol)
                if len(prior_vol) >= 20 and max(prior_vol) > min(prior_vol) else None)
    values["realized_vol_20_within_slice_percentile"] = vol_rank
    values["within_slice_volatility_regime"] = (
        "unclassified" if vol_rank is None else
        "elevated" if vol_rank >= 0.8 else
        "quiet" if vol_rank <= 0.2 else "middle")
    bands_middle = values["sma_20"]
    bands_std = pstdev(close[-20:])
    values["bollinger_middle_20"] = bands_middle
    values["bollinger_upper_20"] = bands_middle + 2 * bands_std
    values["bollinger_lower_20"] = bands_middle - 2 * bands_std
    values["bollinger_width_20_pct"] = 400 * bands_std / bands_middle
    stochastic_high = max(row.high for row in rows[-14:])
    stochastic_low = min(row.low for row in rows[-14:])
    values["stochastic_k_14_pct"] = (100 * (current - stochastic_low) /
        (stochastic_high - stochastic_low) if stochastic_high > stochastic_low else 50.0)
    values["candle_body"] = last.close - last.open
    values["upper_wick"] = last.high - max(last.open, last.close)
    values["lower_wick"] = min(last.open, last.close) - last.low
    values["true_range"] = max(last.high - last.low,
                                abs(last.high - rows[-2].close),
                                abs(last.low - rows[-2].close))
    values["breakout_20"] = current > max(row.high for row in rows[-21:-1])
    values["breakdown_20"] = current < min(row.low for row in rows[-21:-1])
    values["two_close_breakout_20"] = (
        values["breakout_20"] and rows[-2].close >
        max(row.high for row in rows[-22:-2]))
    values["two_close_breakdown_20"] = (
        values["breakdown_20"] and rows[-2].close <
        min(row.low for row in rows[-22:-2]))
    values["confirmed_swing_high"], values["confirmed_swing_low"] = _confirmed_swings(rows)
    values["momentum_alignment"] = ("up" if all(values[f"return_{n}m_pct"] > 0 for n in (5, 15, 30, 60))
                                     else "down" if all(values[f"return_{n}m_pct"] < 0 for n in (5, 15, 30, 60))
                                     else "mixed")
    values["session_return_pct"] = 100 * (current / rows[0].open - 1) if session_open is not None else None
    values["session_high"] = max(row.high for row in rows) if session_open is not None else None
    values["session_low"] = min(row.low for row in rows) if session_open is not None else None
    values["session_midpoint"] = ((values["session_high"] + values["session_low"]) / 2
                                  if session_open is not None else None)
    values["session_prior_high_breakout"] = (
        current > max(row.high for row in rows[:-1])
        if session_open is not None and len(rows) > 1 else None)
    values["session_prior_low_breakdown"] = (
        current < min(row.low for row in rows[:-1])
        if session_open is not None and len(rows) > 1 else None)
    opening = (rows[:opening_range_minutes] if session_open is not None and
               len(rows) >= opening_range_minutes else None)
    values["opening_range_high"] = max(row.high for row in opening) if opening else None
    values["opening_range_low"] = min(row.low for row in opening) if opening else None
    values["opening_range_breakout"] = (current > values["opening_range_high"] if opening else None)
    values["opening_range_breakdown"] = (current < values["opening_range_low"] if opening else None)
    if previous_session is None:
        values.update(previous_close_return_pct=None, opening_gap_pct=None,
                      gap_direction=None, previous_high=None, previous_low=None,
                      previous_close=None, previous_midpoint=None,
                      classic_pivot=None, pivot_r1=None, pivot_s1=None,
                      distance_to_previous_high_pct=None,
                      distance_to_previous_low_pct=None,
                      distance_to_pivot_pct=None)
    else:
        previous = previous_session
        pivot = (previous.high + previous.low + previous.close) / 3
        gap = 100 * (rows[0].open / previous.close - 1)
        values.update(previous_close_return_pct=100 * (current / previous.close - 1),
                      opening_gap_pct=gap,
                      gap_direction="up" if gap > 0 else "down" if gap < 0 else "flat",
                      previous_high=previous.high, previous_low=previous.low,
                      previous_close=previous.close,
                      previous_midpoint=(previous.high + previous.low) / 2,
                      classic_pivot=pivot, pivot_r1=2 * pivot - previous.low,
                      pivot_s1=2 * pivot - previous.high,
                      distance_to_previous_high_pct=100 * (current / previous.high - 1),
                      distance_to_previous_low_pct=100 * (current / previous.low - 1),
                      distance_to_pivot_pct=100 * (current / pivot - 1))
    if rows[0].kind == "index" or any(row.volume is None for row in rows):
        values.update(vwap=None, volume_zscore_20=None, relative_volume_20=None)
    else:
        volumes = [float(row.volume) for row in rows]
        total = sum(volumes)
        typical = [(row.high + row.low + row.close) / 3 for row in rows]
        values["vwap"] = sum(price * volume for price, volume in zip(typical, volumes)) / total if total > 0 else None
        prior = volumes[-21:-1]
        average = mean(prior)
        deviation = pstdev(prior)
        values["relative_volume_20"] = volumes[-1] / average if average > 0 else None
        values["volume_zscore_20"] = (volumes[-1] - average) / deviation if deviation > 0 else None
    if any(isinstance(value, float) and not isfinite(value) for value in values.values()):
        raise ValueError("Indicator calculation overflowed")
    return {"formula_version": VERSION, "as_of": as_of.isoformat(),
            "symbol": rows[0].symbol, "source": rows[0].source,
            "input_minutes": len(rows), "features": values,
            "interpretation": "descriptive_point_in_time_not_a_trained_forecast"}


def calculate_aligned_beta(index_candles, bank_candles, as_of, *, periods=20):
    """Rolling return beta/correlation for exactly aligned completed minutes."""
    if isinstance(periods, bool) or not isinstance(periods, int) or periods < 20:
        raise ValueError("At least 20 aligned return intervals are required")
    index = validate_series(list(index_candles), as_of, expected_minutes=1)
    bank = validate_series(list(bank_candles), as_of, expected_minutes=1)
    if (len(index) < periods + 1 or len(bank) < periods + 1 or
            index[-1].end != as_of or bank[-1].end != as_of or
            index[-1].kind != "index" or bank[-1].kind != "equity"):
        raise ValueError("Aligned index and bank series are incomplete")
    index, bank = index[-periods - 1:], bank[-periods - 1:]
    if any(left.end != right.end for left, right in zip(index, bank)):
        raise ValueError("Index and bank candle endpoints are misaligned")
    x = [log(right.close / left.close) for left, right in zip(index, index[1:])]
    y = [log(right.close / left.close) for left, right in zip(bank, bank[1:])]
    x_average, y_average = mean(x), mean(y)
    covariance = mean((a - x_average) * (b - y_average) for a, b in zip(x, y))
    x_variance = mean((item - x_average) ** 2 for item in x)
    y_variance = mean((item - y_average) ** 2 for item in y)
    beta = covariance / x_variance if x_variance > 0 else None
    correlation = covariance / sqrt(x_variance * y_variance) if x_variance > 0 and y_variance > 0 else None
    return {"as_of": as_of.isoformat(), "index_symbol": index[-1].symbol,
            "bank_symbol": bank[-1].symbol, "intervals": periods,
            "beta": beta, "correlation": correlation,
            "interpretation": "past_aligned_returns_only_not_a_future_bank_contribution"}

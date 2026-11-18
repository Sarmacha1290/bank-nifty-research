"""Deterministic, past-only candle shapes and formations for research.

These are explicit local definitions, not universal textbook standards or
validated trading strategies. They use only completed, continuous 1m OHLC.
The caller must separately establish market-data quality and source identity.
"""

from .core import validate_series


VERSION = "candle-patterns-v0.1"
PATTERN_NAMES = (
    "doji", "long_legged_doji", "dragonfly_doji", "gravestone_doji",
    "bullish_marubozu", "bearish_marubozu", "hammer_shape",
    "inverted_hammer_shape", "bullish_engulfing", "bearish_engulfing",
    "bullish_harami", "bearish_harami", "inside_bar", "outside_bar",
    "piercing_line", "dark_cloud_cover", "morning_star", "evening_star",
    "three_white_soldiers", "three_black_crows", "tweezer_top", "tweezer_bottom",
)


def _parts(row):
    span = row.high - row.low
    body = abs(row.close - row.open)
    upper = row.high - max(row.open, row.close)
    lower = min(row.open, row.close) - row.low
    return span, body, upper, lower


def analyze_candle_patterns(candles, as_of, *, expected_minutes=1):
    """Return fixed-schema flags for formations ending exactly at ``as_of``.

    Zero-range candles cannot establish a shape. Tweezers use a 10%-of-range
    tolerance; doji bodies use 10%, marubozu wicks 5%, and star bodies 30%.
    Hammer shapes do not assert a preceding trend or future reversal.
    """
    if type(expected_minutes) is not int or expected_minutes not in (1, 5, 15, 30, 60):
        raise ValueError("Unsupported candle interval")
    rows = validate_series(list(candles), as_of, expected_minutes=expected_minutes)
    if len(rows) < 3 or rows[-1].end != as_of:
        raise ValueError("Three continuous completed candles ending at as_of are required")
    a, b, c = rows[-3:]
    ar, ab, au, al = _parts(a)
    br, bb, bu, bl = _parts(b)
    cr, cb, cu, cl = _parts(c)
    bull = c.close > c.open
    bear = c.close < c.open
    prev_bull = b.close > b.open
    prev_bear = b.close < b.open
    body_top, body_bottom = max(c.open, c.close), min(c.open, c.close)
    prev_top, prev_bottom = max(b.open, b.close), min(b.open, b.close)
    doji = cr > 0 and cb <= .1 * cr
    tolerance = .1 * max(br, cr)
    flags = {
        "doji": doji,
        "long_legged_doji": doji and cu >= .35 * cr and cl >= .35 * cr,
        "dragonfly_doji": doji and cl >= .6 * cr and cu <= .1 * cr,
        "gravestone_doji": doji and cu >= .6 * cr and cl <= .1 * cr,
        "bullish_marubozu": bull and cr > 0 and cb >= .8 * cr and cu <= .05 * cr and cl <= .05 * cr,
        "bearish_marubozu": bear and cr > 0 and cb >= .8 * cr and cu <= .05 * cr and cl <= .05 * cr,
        "hammer_shape": cr > 0 and cb > 0 and cl >= 2 * cb and cu <= .25 * cb and body_top >= c.low + .65 * cr,
        "inverted_hammer_shape": cr > 0 and cb > 0 and cu >= 2 * cb and cl <= .25 * cb and body_bottom <= c.low + .35 * cr,
        "bullish_engulfing": prev_bear and bull and body_bottom <= prev_bottom and body_top >= prev_top and cb > bb,
        "bearish_engulfing": prev_bull and bear and body_bottom <= prev_bottom and body_top >= prev_top and cb > bb,
        "bullish_harami": prev_bear and bull and bb > cb > 0 and body_bottom >= prev_bottom and body_top <= prev_top,
        "bearish_harami": prev_bull and bear and bb > cb > 0 and body_bottom >= prev_bottom and body_top <= prev_top,
        "inside_bar": c.high < b.high and c.low > b.low,
        "outside_bar": c.high > b.high and c.low < b.low,
        "piercing_line": prev_bear and bull and c.open < b.close and (b.open + b.close) / 2 < c.close < b.open,
        "dark_cloud_cover": prev_bull and bear and c.open > b.close and b.open < c.close < (b.open + b.close) / 2,
        "morning_star": a.close < a.open and ar > 0 and ab >= .5 * ar and br > 0 and bb <= .3 * br and bull and cr > 0 and cb >= .5 * cr and c.close > (a.open + a.close) / 2 and max(b.open, b.close) < a.close,
        "evening_star": a.close > a.open and ar > 0 and ab >= .5 * ar and br > 0 and bb <= .3 * br and bear and cr > 0 and cb >= .5 * cr and c.close < (a.open + a.close) / 2 and min(b.open, b.close) > a.close,
        "three_white_soldiers": all(row.close > row.open for row in (a, b, c)) and a.close < b.close < c.close and a.open < b.open < a.close and b.open < c.open < b.close and all(_parts(row)[1] >= .5 * _parts(row)[0] for row in (a, b, c)),
        "three_black_crows": all(row.close < row.open for row in (a, b, c)) and a.close > b.close > c.close and a.close < b.open < a.open and b.close < c.open < b.open and all(_parts(row)[1] >= .5 * _parts(row)[0] for row in (a, b, c)),
        "tweezer_top": br > 0 and cr > 0 and prev_bull and bear and abs(b.high - c.high) <= tolerance,
        "tweezer_bottom": br > 0 and cr > 0 and prev_bear and bull and abs(b.low - c.low) <= tolerance,
    }
    assert tuple(flags) == PATTERN_NAMES
    return {
        "formula_version": VERSION,
        "as_of": as_of.isoformat(),
        "symbol": c.symbol,
        "source": c.source,
        "input_minutes": len(rows) * expected_minutes,
        "candle_interval_minutes": expected_minutes,
        "body_to_range_pct": 100 * cb / cr if cr else None,
        "upper_wick_to_range_pct": 100 * cu / cr if cr else None,
        "lower_wick_to_range_pct": 100 * cl / cr if cr else None,
        "patterns": flags,
        "interpretation": "unvalidated_descriptive_shapes_not_predictions_or_trading_signals",
    }

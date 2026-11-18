"""Point-in-time constituent contribution estimates; no prediction or order.

NSE Indices describes Nifty Bank as a capped free-float-weighted index. Given
a *complete* weight snapshot at the first price timestamp, weighted stock
returns describe an approximate index move over a later aligned interval.
Official divisor/corporate-action adjustments can create a residual, which is
reported rather than hidden. A public top-10 factsheet is not a full snapshot.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite, sqrt
from statistics import mean
from urllib.parse import urlparse

from ..core import validate_series


METHODOLOGY_URL = "https://www.niftyindices.com/Methodology/Method_NIFTY_Equity_Indices.pdf"
MAX_CONSTITUENTS = 14


def _aware(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _price(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")


@dataclass(frozen=True)
class Weight:
    symbol: str
    percent: float

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("Weight symbol is required")
        _price(self.percent, "weight percent")
        if self.percent > 100:
            raise ValueError("Weight cannot exceed 100 percent")


@dataclass(frozen=True)
class WeightSnapshot:
    index_symbol: str
    valid_from: datetime
    valid_until: datetime
    known_at: datetime
    source_kind: str
    source_uri: str
    weights: tuple[Weight, ...]

    def __post_init__(self):
        if not isinstance(self.index_symbol, str) or not self.index_symbol.strip():
            raise ValueError("Index symbol is required")
        if _aware(self.valid_from, "valid_from") >= _aware(self.valid_until, "valid_until"):
            raise ValueError("Weight validity interval is invalid")
        _aware(self.known_at, "known_at")
        if self.source_kind not in ("synthetic", "official_nse_indices"):
            raise ValueError("Weight source kind must be synthetic or official_nse_indices")
        if self.source_kind == "official_nse_indices":
            parsed = urlparse(self.source_uri)
            if parsed.scheme != "https" or parsed.hostname not in ("niftyindices.com", "www.niftyindices.com"):
                raise ValueError("Official weights require an NSE Indices HTTPS source URI")
        elif not self.source_uri.startswith("synthetic:"):
            raise ValueError("Synthetic weights must be labelled synthetic")
        if not isinstance(self.weights, tuple) or not 1 <= len(self.weights) <= MAX_CONSTITUENTS:
            raise ValueError("A complete snapshot must contain 1 to 14 constituent weights")
        if any(not isinstance(item, Weight) for item in self.weights):
            raise ValueError("Weights must be Weight records")
        if len({item.symbol for item in self.weights}) != len(self.weights):
            raise ValueError("Duplicate constituent weight")
        total = sum(item.percent for item in self.weights)
        if abs(total - 100.0) > 0.1:
            raise ValueError("Full constituent weights must sum to 100 percent within rounding tolerance")


@dataclass(frozen=True)
class PricePair:
    symbol: str
    base_at: datetime
    current_at: datetime
    base_price: float
    current_price: float
    source: str

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("Price symbol is required")
        if _aware(self.base_at, "base_at") >= _aware(self.current_at, "current_at"):
            raise ValueError("Price timestamps are not increasing")
        _price(self.base_price, "base price")
        _price(self.current_price, "current price")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("Price source is required")


@dataclass(frozen=True)
class ConstituentContribution:
    symbol: str
    weight_percent: float
    stock_return_percent: float
    weighted_return_percentage_points: float
    estimated_index_points: float


@dataclass(frozen=True)
class ContributionResult:
    base_at: datetime
    current_at: datetime
    source_kind: str
    source_uri: str
    rows: tuple[ConstituentContribution, ...]
    weighted_return_percent: float
    observed_index_return_percent: float
    residual_percentage_points: float
    estimated_index_point_change: float
    observed_index_point_change: float
    status: str


def calculate_contributions(weights, index_prices, constituent_prices):
    """Compare a complete weighted basket with the observed index interval.

    Input timestamps must match exactly; no nearest-row fill or historical
    application of current weights is permitted. Source claims are structural
    metadata, not independent authentication of downloaded documents.
    """
    if not isinstance(weights, WeightSnapshot) or not isinstance(index_prices, PricePair):
        raise TypeError("Expected a weight snapshot and index price pair")
    prices = list(constituent_prices)
    if any(not isinstance(item, PricePair) for item in prices):
        raise TypeError("Expected constituent PricePair records")
    base = _aware(index_prices.base_at, "index base_at")
    current = _aware(index_prices.current_at, "index current_at")
    if weights.index_symbol != index_prices.symbol:
        raise ValueError("Index symbol does not match weight snapshot")
    if not (_aware(weights.valid_from, "valid_from") <= base < current <
            _aware(weights.valid_until, "valid_until")):
        raise ValueError("Weight snapshot is not effective over the complete interval")
    if _aware(weights.known_at, "known_at") > base:
        raise ValueError("Weights were not known at the base timestamp")
    expected = {item.symbol for item in weights.weights}
    if len(prices) != len(expected) or {item.symbol for item in prices} != expected:
        raise ValueError("Every weighted constituent requires one aligned price pair")
    if weights.source_kind == "official_nse_indices" and (
        index_prices.source == "synthetic" or any(item.source == "synthetic" for item in prices)
    ):
        raise ValueError("Official-weight calculation cannot use synthetic prices")
    indexed = {item.symbol: item for item in prices}
    rows = []
    for weight in weights.weights:
        item = indexed[weight.symbol]
        if (_aware(item.base_at, "constituent base_at") != base or
                _aware(item.current_at, "constituent current_at") != current):
            raise ValueError("Constituent price timestamps must match the index exactly")
        stock_return = 100.0 * (item.current_price / item.base_price - 1.0)
        weighted = weight.percent / 100.0 * stock_return
        if not isfinite(stock_return) or not isfinite(weighted):
            raise ValueError("Constituent return overflowed")
        rows.append(ConstituentContribution(weight.symbol, weight.percent, stock_return,
                                            weighted, index_prices.base_price * weighted / 100.0))
    estimated_return = sum(row.weighted_return_percentage_points for row in rows)
    observed_return = 100.0 * (index_prices.current_price / index_prices.base_price - 1.0)
    if not isfinite(estimated_return) or not isfinite(observed_return):
        raise ValueError("Index contribution calculation overflowed")
    return ContributionResult(
        index_prices.base_at, index_prices.current_at, weights.source_kind, weights.source_uri,
        tuple(sorted(rows, key=lambda row: row.weighted_return_percentage_points, reverse=True)),
        estimated_return, observed_return, observed_return - estimated_return,
        sum(row.estimated_index_points for row in rows),
        index_prices.current_price - index_prices.base_price,
        "synthetic_demo" if weights.source_kind == "synthetic" else "calculation_only_source_not_independently_authenticated",
    )


def describe_constituent_breadth(result):
    """Aligned one-interval breadth and dispersion from a complete basket."""
    if not isinstance(result, ContributionResult) or not result.rows:
        raise ValueError("A complete contribution result is required")
    weighted_mean = result.weighted_return_percent
    dispersion = sqrt(sum(row.weight_percent / 100 *
                          (row.stock_return_percent - weighted_mean) ** 2
                          for row in result.rows))
    return {"advancing_banks": sum(row.stock_return_percent > 0 for row in result.rows),
            "declining_banks": sum(row.stock_return_percent < 0 for row in result.rows),
            "unchanged_banks": sum(row.stock_return_percent == 0 for row in result.rows),
            "advancing_weight_percent": sum(row.weight_percent for row in result.rows
                                             if row.stock_return_percent > 0),
            "weighted_dispersion_percentage_points": dispersion,
            "weight_concentration_hhi": sum((row.weight_percent / 100) ** 2
                                             for row in result.rows),
            "top_contributor": result.rows[0].symbol,
            "top_detractor": result.rows[-1].symbol,
            "residual_percentage_points": result.residual_percentage_points,
            "interpretation": "aligned_weighted_basket_descriptor_not_a_forecast"}


def describe_weighted_ma_breadth(weights, bank_candles, as_of, *, period=20):
    """Share of current index weight above each bank's completed-candle SMA."""
    if (not isinstance(weights, WeightSnapshot) or not isinstance(bank_candles, dict) or
            isinstance(period, bool) or not isinstance(period, int) or period < 2):
        raise ValueError("Complete weights, bank histories and a valid period are required")
    cutoff = _aware(as_of, "as_of")
    expected = {row.symbol for row in weights.weights}
    if set(bank_candles) != expected:
        raise ValueError("Every weighted bank needs its own candle history")
    if _aware(weights.known_at, "known_at") > cutoff or not (
            _aware(weights.valid_from, "valid_from") <= cutoff <
            _aware(weights.valid_until, "valid_until")):
        raise ValueError("Weights were not known and effective at the cutoff")
    above = []
    for weight in weights.weights:
        history = validate_series(list(bank_candles[weight.symbol]), as_of,
                                  expected_minutes=1)
        if (len(history) < period or history[-1].end != as_of or
                history[-1].kind != "equity" or
                _aware(history[-period].start, "first candle") <
                _aware(weights.valid_from, "valid_from")):
            raise ValueError("Bank candles or weight validity do not cover the MA window")
        if weights.source_kind == "official_nse_indices" and any(
                row.source == "synthetic" for row in history[-period:]):
            raise ValueError("Official weights cannot be paired with synthetic bank prices")
        sma = mean(row.close for row in history[-period:])
        above.append((weight.symbol, weight.percent, history[-1].close > sma))
    return {"as_of": as_of.isoformat(), "period_minutes": period,
            "banks_above_ma": sum(flag for _, _, flag in above),
            "weight_above_ma_percent": sum(weight for _, weight, flag in above if flag),
            "per_bank_above_ma": {symbol: flag for symbol, _, flag in above},
            "interpretation": "point_in_time_complete_weighted_breadth_not_a_forecast"}

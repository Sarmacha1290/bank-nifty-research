"""Validate Groww's timestamped, five-level NSE cash order-book feed.

Book quantities are outstanding orders. They do not classify executed trades.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite


@dataclass(frozen=True)
class DepthTick:
    symbol: str
    exchange_at: datetime
    received_at: datetime
    buy_levels: tuple[tuple[float, float], ...]
    sell_levels: tuple[tuple[float, float], ...]
    source: str = "Groww market depth feed"

    @property
    def buy_quantity(self):
        return sum(quantity for _, quantity in self.buy_levels) if self.buy_levels else None

    @property
    def sell_quantity(self):
        return sum(quantity for _, quantity in self.sell_levels) if self.sell_levels else None

    @property
    def best_bid(self):
        return self.buy_levels[0][0] if self.buy_levels else None

    @property
    def best_ask(self):
        return self.sell_levels[0][0] if self.sell_levels else None


def describe_depth(tick):
    """Visible quoted liquidity only; no executed trade-side inference."""
    if (not isinstance(tick, DepthTick) or tick.source != "Groww market depth feed" or
            tick.exchange_at.tzinfo is None or tick.received_at.tzinfo is None or
            len(tick.buy_levels) > 5 or len(tick.sell_levels) > 5):
        raise ValueError("A validated depth tick is required")
    if any(not isfinite(price) or price <= 0 or not isfinite(size) or size < 0
           for price, size in (*tick.buy_levels, *tick.sell_levels)):
        raise ValueError("Invalid visible depth values")
    bid, ask = tick.best_bid, tick.best_ask
    spread = ask - bid if bid is not None and ask is not None and ask >= bid else None
    if bid is not None and ask is not None and ask < bid:
        raise ValueError("Crossed top-of-book prices need separate investigation")
    bid_size = tick.buy_levels[0][1] if tick.buy_levels else None
    ask_size = tick.sell_levels[0][1] if tick.sell_levels else None
    top_total = bid_size + ask_size if bid_size is not None and ask_size is not None else None
    book_total = (tick.buy_quantity + tick.sell_quantity
                  if tick.buy_quantity is not None and tick.sell_quantity is not None else None)
    return {"symbol": tick.symbol, "exchange_at": tick.exchange_at.isoformat(),
            "receipt_lag_seconds": (tick.received_at - tick.exchange_at).total_seconds(),
            "best_bid": bid, "best_ask": ask, "spread": spread,
            "spread_bps": (10000 * spread / ((bid + ask) / 2)
                           if spread is not None else None),
            "top_level_imbalance": ((bid_size - ask_size) / top_total if top_total else None),
            "top5_imbalance": ((tick.buy_quantity - tick.sell_quantity) / book_total
                               if book_total else None),
            "microprice": ((ask * bid_size + bid * ask_size) / top_total
                           if spread is not None and top_total else None),
            "interpretation": "visible_orders_only_not_executed_buy_sell_volume"}


def estimate_visible_book_walk(tick, *, side, shares):
    """Hypothetical fill across five visible levels, with no execution claim."""
    describe_depth(tick)
    if side not in ("buy", "sell") or isinstance(shares, bool) or not isinstance(shares, (int, float)) or not isfinite(shares) or shares <= 0:
        raise ValueError("A buy/sell side and positive share quantity are required")
    levels = tick.sell_levels if side == "buy" else tick.buy_levels
    if not levels:
        return {"side": side, "requested_shares": shares, "visible_shares": 0,
                "filled_shares": 0, "unfilled_shares": shares,
                "average_price": None, "slippage_bps_from_best": None,
                "interpretation": "visible_book_only_no_execution_or_impact_model"}
    prices = [price for price, _ in levels]
    if any((right < left if side == "buy" else right > left)
           for left, right in zip(prices, prices[1:])):
        raise ValueError("Visible book level prices are not ordered")
    remaining, notional = float(shares), 0.0
    for price, available in levels:
        take = min(remaining, available)
        notional += take * price
        remaining -= take
        if remaining <= 0:
            break
    filled = float(shares) - remaining
    average = notional / filled if remaining == 0 and filled > 0 else None
    best = levels[0][0]
    slip = ((average / best - 1) * 10000 * (1 if side == "buy" else -1)
            if average is not None else None)
    return {"side": side, "requested_shares": shares,
            "visible_shares": sum(size for _, size in levels),
            "filled_shares": filled, "unfilled_shares": remaining,
            "average_price": average, "slippage_bps_from_best": slip,
            "interpretation": "visible_book_only_no_execution_or_impact_model"}


def _levels(book):
    if book is None:
        return ()
    if not isinstance(book, dict) or len(book) > 5:
        raise ValueError("Unexpected depth-book structure")
    result = []
    for rank, level in book.items():
        if str(rank) not in ("1", "2", "3", "4", "5") or not isinstance(level, dict):
            raise ValueError("Unexpected depth level")
        price, quantity = level.get("price"), level.get("qty")
        if (isinstance(price, bool) or not isinstance(price, (int, float)) or
                not isfinite(price) or price <= 0 or isinstance(quantity, bool) or
                not isinstance(quantity, (int, float)) or not isfinite(quantity) or quantity < 0):
            raise ValueError("Invalid depth price or quantity")
        result.append((int(rank), float(price), float(quantity)))
    result.sort()
    return tuple((price, quantity) for _, price, quantity in result)


def parse_depth_ticks(payload, instruments, received_at, *, max_age_seconds=15):
    """Return (valid ticks, rejected rows); missing/unchanged rows are not filled."""
    if not isinstance(received_at, datetime) or received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("Depth receipt time must be timezone-aware")
    if max_age_seconds <= 0:
        raise ValueError("Positive depth age limit required")
    nested = payload.get("NSE", {}).get("CASH", {}) if isinstance(payload, dict) else {}
    if not isinstance(nested, dict):
        return (), len(instruments)
    valid, rejected = [], 0
    for instrument in instruments:
        token = instrument["exchange_token"]
        row = nested.get(token)
        if row is None:
            continue
        try:
            if not isinstance(row, dict):
                raise ValueError("Malformed depth row")
            stamp = row.get("tsInMillis")
            # The official example appears seconds-sized despite its name;
            # reject that ambiguity until real feed units are confirmed.
            if (isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or
                    not isfinite(stamp) or not 1_000_000_000_000 <= stamp < 10_000_000_000_000):
                raise ValueError("Depth timestamp is not epoch milliseconds")
            exchange_at = datetime.fromtimestamp(stamp / 1000, timezone.utc)
            lag = (received_at - exchange_at).total_seconds()
            if not -2 <= lag <= max_age_seconds:
                raise ValueError("Depth update is stale or in the future")
            buy, sell = _levels(row.get("buyBook")), _levels(row.get("sellBook"))
            if not buy and not sell:
                raise ValueError("Empty depth book")
            valid.append(DepthTick(instrument["groww_symbol"], exchange_at,
                                   received_at.astimezone(timezone.utc), buy, sell))
        except (ValueError, OverflowError, OSError):
            rejected += 1
    return tuple(valid), rejected

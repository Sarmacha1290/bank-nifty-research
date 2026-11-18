"""Conservative trial aggregate of 14 near-time visible order books."""
from datetime import timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def align_latest_depth(latest, expected_symbols, *, max_exchange_span_seconds=2,
                       max_receipt_span_seconds=5):
    """Fail closed unless every bank has a near-time, complete two-sided book.

    Sums represent *visible orders* across different shares, never Bank Nifty
    index volume, completed buy/sell trades, or weighted index contribution.
    """
    expected = tuple(expected_symbols)
    if len(expected) != 14 or len(set(expected)) != 14:
        raise ValueError("A unique 14-bank reference list is required")
    if max_exchange_span_seconds <= 0 or max_receipt_span_seconds <= 0:
        raise ValueError("Positive alignment limits required")
    missing = [symbol for symbol in expected if symbol not in latest]
    result = {"status": "unavailable", "coverage": 14 - len(missing),
              "expected": 14, "missing_symbols": missing,
              "trial_exchange_span_limit_seconds": max_exchange_span_seconds,
              "trial_receipt_span_limit_seconds": max_receipt_span_seconds,
              "buy_shares": None, "sell_shares": None,
              "buy_order_value_inr": None, "sell_order_value_inr": None,
              "imbalance": None, "anchor_exchange_at": None, "available_at": None,
              "reason": "missing_bank_books" if missing else None,
              "meaning": "Unweighted visible top-five orders; not executed flow or index volume"}
    if missing:
        return result
    rows = [latest[symbol] for symbol in expected]
    if any(not row.buy_levels or not row.sell_levels for row in rows):
        result["reason"] = "incomplete_book_side"
        return result
    exchange = [row.exchange_at for row in rows]
    receipt = [row.received_at for row in rows]
    if any(value.tzinfo is None or value.utcoffset() is None for value in exchange + receipt):
        result["reason"] = "invalid_timestamp"
        return result
    exchange_span = (max(exchange) - min(exchange)).total_seconds()
    receipt_span = (max(receipt) - min(receipt)).total_seconds()
    result["exchange_span_seconds"] = exchange_span
    result["receipt_span_seconds"] = receipt_span
    result["anchor_exchange_at"] = max(exchange).isoformat()
    result["available_at"] = max(receipt).isoformat()
    if len({value.astimezone(IST).date() for value in exchange}) != 1:
        result["reason"] = "different_sessions"
        return result
    if exchange_span > max_exchange_span_seconds or receipt_span > max_receipt_span_seconds:
        result["reason"] = "books_not_near_time"
        return result
    if any(not -2 <= (got - sent).total_seconds() <= 15 for sent, got in zip(exchange, receipt)):
        result["reason"] = "stale_or_future_receipt"
        return result
    buy_shares = sum(row.buy_quantity for row in rows)
    sell_shares = sum(row.sell_quantity for row in rows)
    buy_value = sum(price * quantity for row in rows for price, quantity in row.buy_levels)
    sell_value = sum(price * quantity for row in rows for price, quantity in row.sell_levels)
    result.update(status="aligned_trial", reason=None, buy_shares=buy_shares,
                  sell_shares=sell_shares, buy_order_value_inr=buy_value,
                  sell_order_value_inr=sell_value,
                  imbalance=((buy_value - sell_value) / (buy_value + sell_value)
                             if buy_value + sell_value > 0 else None))
    return result

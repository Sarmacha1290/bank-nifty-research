"""Receipt-time bank quote snapshots and exploratory activity alerts.

Groww quote volume is cumulative traded shares. Buy/sell quantities are
outstanding orders, not buyer/seller-initiated executed volume.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import median
import re

IST = timezone(timedelta(hours=5, minutes=30))
MAX_AGGREGATE_SWEEP_SPAN_SECONDS = 5.0


@dataclass(frozen=True)
class BankQuote:
    symbol: str
    received_at: datetime
    volume: int | None
    total_buy_quantity: float | None
    total_sell_quantity: float | None
    last_price: float | None
    source: str = "Groww NSE cash quote"


def _number(value, *, integer=False, positive=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError("Invalid numeric quote field")
    if value < 0 or (positive and value == 0) or (integer and int(value) != value):
        raise ValueError("Invalid numeric quote range")
    return int(value) if integer else float(value)


def parse_bank_quote(response, symbol, received_at):
    """Keep absent quote fields absent; a receipt time is not exchange time."""
    if not isinstance(symbol, str) or re.fullmatch(r"[A-Z0-9]+", symbol) is None:
        raise ValueError("Invalid NSE bank symbol")
    if not isinstance(received_at, datetime) or received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("A timezone-aware receipt time is required")
    if not isinstance(response, dict) or response.get("status") != "SUCCESS" or not isinstance(response.get("payload"), dict):
        raise ValueError("Groww did not return a quote payload")
    payload = response["payload"]
    return BankQuote("NSE-" + symbol, received_at.astimezone(timezone.utc),
                     _number(payload.get("volume"), integer=True),
                     _number(payload.get("total_buy_quantity")),
                     _number(payload.get("total_sell_quantity")),
                     _number(payload.get("last_price"), positive=True))


def summarize_bank_quotes(quotes, expected_symbols):
    """Return an all-bank sum only when every expected symbol is represented."""
    expected = tuple(expected_symbols)
    if not expected or len(expected) != len(set(expected)):
        raise ValueError("Expected a unique dated bank list")
    rows = tuple(quotes)
    by_symbol = {row.symbol: row for row in rows}
    if len(by_symbol) != len(rows) or any(symbol not in expected for symbol in by_symbol):
        raise ValueError("Duplicate or unexpected bank quote")
    complete = len(rows) == len(expected)
    receipt_span = ((max(row.received_at for row in rows) -
                     min(row.received_at for row in rows)).total_seconds()
                    if rows else None)
    coherent_for_aggregate_spike = (complete and receipt_span is not None and
                                    receipt_span <= MAX_AGGREGATE_SWEEP_SPAN_SECONDS)
    def total(field):
        if not complete or any(getattr(row, field) is None for row in rows):
            return None
        return sum(getattr(row, field) for row in rows)
    buy, sell = total("total_buy_quantity"), total("total_sell_quantity")
    return {"coverage": len(rows), "expected": len(expected), "complete": complete,
            "receipt_span_seconds": receipt_span,
            "coherent_for_aggregate_spike": coherent_for_aggregate_spike,
            "cumulative_traded_shares": total("volume"),
            "outstanding_buy_shares": buy, "outstanding_sell_shares": sell,
            "order_book_imbalance": ((buy - sell) / (buy + sell) if buy is not None and sell is not None
                                     and buy + sell > 0 else None),
            "basis": "Serial receipt-time unweighted sum of constituent shares; not a simultaneous Bank Nifty index volume or executed buy/sell flow"}


def detect_activity_spikes(quotes, *, min_baseline=5, volume_multiple=3.0,
                           book_multiple=2.0, imbalance_min=0.6):
    """Exploratory same-session alerts; no signal or trade recommendation.

    Volume compares one interval's increase with earlier valid, similarly
    spaced intervals. Book alerts compare outstanding quantity with prior
    snapshots and require dominance of that side of the current book.
    """
    if min_baseline < 3 or volume_multiple <= 1 or book_multiple <= 1:
        raise ValueError("Trial thresholds must be conservative")
    rows = sorted(tuple(quotes), key=lambda row: (row.symbol, row.received_at))
    alerts = []
    for symbol in sorted({row.symbol for row in rows}):
        series = [row for row in rows if row.symbol == symbol]
        if len(series) < min_baseline + 2:
            continue
        latest = series[-1]
        day = latest.received_at.astimezone(IST).date()
        series = [row for row in series if row.received_at.astimezone(IST).date() == day]
        if len(series) < min_baseline + 2:
            continue
        deltas = []
        for prior, current in zip(series, series[1:]):
            seconds = (current.received_at - prior.received_at).total_seconds()
            if (10 <= seconds <= 40 and prior.volume is not None and current.volume is not None
                    and current.volume >= prior.volume):
                deltas.append((current.volume - prior.volume, seconds))
            else:
                deltas.append(None)
        recent = deltas[-min_baseline-1:]
        if (len(recent) == min_baseline + 1 and all(item is not None for item in recent)
                and all(0.8 <= item[1] / recent[-1][1] <= 1.2 for item in recent[:-1])):
            baseline = median(value for value, _ in recent[:-1])
            current = recent[-1][0]
            if baseline > 0 and current >= baseline * volume_multiple:
                alerts.append({"symbol": symbol, "kind": "traded_volume_surge",
                               "at": latest.received_at.isoformat(), "current": current,
                               "baseline": baseline, "multiple": current / baseline,
                               "meaning": "Traded shares increased; trade direction unknown"})
        prior_rows = series[-min_baseline-1:-1]
        if (len(prior_rows) != min_baseline or
                any(not 10 <= (right.received_at - left.received_at).total_seconds() <= 40
                    for left, right in zip(prior_rows, prior_rows[1:] + [latest]))):
            continue
        for side, opposite in (("buy", "sell"), ("sell", "buy")):
            field = f"total_{side}_quantity"
            own = getattr(latest, field)
            other = getattr(latest, f"total_{opposite}_quantity")
            history = [getattr(row, field) for row in prior_rows]
            if (own is None or other is None or len(history) != min_baseline
                    or any(value is None for value in history)):
                continue
            baseline = median(history)
            imbalance = (own - other) / (own + other) if own + other > 0 else 0
            if baseline > 0 and own >= baseline * book_multiple and imbalance >= imbalance_min:
                alerts.append({"symbol": symbol, "kind": f"outstanding_{side}_orders_surge",
                               "at": latest.received_at.isoformat(), "current": own,
                               "baseline": baseline, "multiple": own / baseline,
                               "meaning": f"Outstanding {side} quantity rose; not executed {side} trades"})
    return alerts


def detect_aggregate_spikes(sweeps, expected_symbols):
    """Alert only from complete, tightly timed unweighted bank sweeps."""
    aggregate = []
    for sweep in sweeps:
        rows = tuple(sweep)
        summary = summarize_bank_quotes(rows, expected_symbols)
        if not summary["coherent_for_aggregate_spike"]:
            continue
        aggregate.append(BankQuote("NSE-ALL14-UNWEIGHTED", max(row.received_at for row in rows),
                                   summary["cumulative_traded_shares"],
                                   summary["outstanding_buy_shares"],
                                   summary["outstanding_sell_shares"], None))
    return detect_activity_spikes(aggregate)

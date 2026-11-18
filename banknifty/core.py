"""Validated candles and session-aware windows for the data-foundation phase.

This module does not produce predictions. ``label_returns`` is exclusively for
offline evaluation: it deliberately consumes candles after the evaluation origin.
Online callers must use ``validate_series`` with their actual information cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from numbers import Real
from typing import Iterable, Literal


InstrumentKind = Literal["index", "equity", "future", "option"]
DEFAULT_HORIZONS = (15, 30, 60)


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _positive_minutes(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _horizons(values: Iterable[int]) -> tuple[int, ...]:
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ValueError("horizons must be a nonempty iterable of positive integers") from exc
    if not result:
        raise ValueError("horizons must not be empty")
    for value in result:
        _positive_minutes(value, "horizon")
    if len(set(result)) != len(result):
        raise ValueError("horizons must be unique")
    return result


def _number(value: float, name: str, *, positive: bool) -> None:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be finite and numeric")
    try:
        finite = isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} must be finite and numeric")
    if (positive and value <= 0) or (not positive and value < 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")


@dataclass(frozen=True)
class Candle:
    """One candle, with explicit interval boundaries and data provenance.

    A candle becomes available only when ``end <= as_of``. Spot indices do not
    have directly traded volume or open interest, so both fields are normalized
    to ``None`` for ``kind='index'`` even if a feed supplies placeholders.
    """

    symbol: str
    kind: InstrumentKind
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    source: str
    volume: float | None = None
    open_interest: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a nonempty string")
        if self.kind not in ("index", "equity", "future", "option"):
            raise ValueError("kind must be index, equity, future, or option")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a nonempty string")
        if _aware(self.start, "start") >= _aware(self.end, "end"):
            raise ValueError("candle start must be before end")
        for name in ("open", "high", "low", "close"):
            _number(getattr(self, name), name, positive=True)
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("OHLC values must satisfy low <= open/close <= high")
        if self.kind == "index":
            object.__setattr__(self, "volume", None)
            object.__setattr__(self, "open_interest", None)
        else:
            for name in ("volume", "open_interest"):
                value = getattr(self, name)
                if value is not None:
                    _number(value, name, positive=False)


@dataclass(frozen=True)
class Session:
    """Authoritative session supplied by the caller; no holiday inference."""

    open: datetime
    close: datetime
    source: str

    def __post_init__(self) -> None:
        if _aware(self.open, "session open") >= _aware(self.close, "session close"):
            raise ValueError("session open must be before session close")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("session source must be a nonempty string")


def prepare_windows(
    as_of: datetime,
    session: Session,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
) -> list[dict]:
    """Describe eligible horizons without generating a forecast.

    The origin must lie in [session.open, session.close). A target exactly at
    session.close is eligible; a target beyond it is not shortened or rolled.
    """
    origin = _aware(as_of, "as_of")
    if not isinstance(session, Session):
        raise ValueError("session must be a Session")
    opening = _aware(session.open, "session open")
    closing = _aware(session.close, "session close")
    result = []
    for minutes in _horizons(horizons):
        target = origin + timedelta(minutes=minutes)
        status = "ready"
        if not opening <= origin < closing:
            status = "market_closed"
        elif target > closing:
            status = "crosses_session_close"
        result.append({
            "horizon_minutes": minutes,
            "as_of": as_of.isoformat(),
            "target_at": target.astimezone(as_of.tzinfo).isoformat(),
            "status": status,
        })
    return result


def _checked_candles(candles: Iterable[Candle], minutes: int) -> list[Candle]:
    """Validate structure without applying an online information cutoff."""
    step = timedelta(minutes=_positive_minutes(minutes, "expected_minutes"))
    checked = list(candles)
    if any(not isinstance(candle, Candle) for candle in checked):
        raise ValueError("series must contain Candle instances")
    identities = {(c.symbol, c.kind, c.source) for c in checked}
    if len(identities) > 1:
        raise ValueError("series cannot mix instruments, kinds, or sources")
    checked.sort(key=lambda candle: _aware(candle.end, "candle end"))
    seen = set()
    previous_end = None
    for candle in checked:
        # Recheck the records so forged/deserialized dataclass instances cannot
        # bypass the same validation that applies at normal construction.
        candle.__post_init__()
        start = _aware(candle.start, "candle start")
        end = _aware(candle.end, "candle end")
        if end in seen:
            raise ValueError("duplicate candle end timestamp")
        seen.add(end)
        if end - start != step:
            raise ValueError("candle duration does not match expected_minutes")
        if previous_end is not None and start < previous_end:
            raise ValueError("overlapping candles in series")
        previous_end = end
    return checked


def validate_series(
    candles: list[Candle],
    as_of: datetime,
    expected_minutes: int = 1,
) -> list[Candle]:
    """Return a sorted, complete, single-instrument series available by as_of.

    Supply one continuous session slice at a time. This rejects a gap anywhere
    inside the supplied slice; it cannot infer missing candles before its first
    timestamp or after its last timestamp. The caller must additionally check
    required coverage/freshness for its own use case.
    """
    cutoff = _aware(as_of, "as_of")
    checked = _checked_candles(candles, expected_minutes)
    previous_end = None
    for candle in checked:
        start = _aware(candle.start, "candle start")
        end = _aware(candle.end, "candle end")
        if end > cutoff:
            raise ValueError("incomplete or future candle exceeds as_of")
        if previous_end is not None and start != previous_end:
            raise ValueError("missing interval or overlapping candles in series")
        previous_end = end
    return checked


def label_returns(
    candles: list[Candle],
    as_of: datetime,
    session: Session,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
) -> list[dict]:
    """Compute future realized returns for OFFLINE evaluation only.

    Candle END timestamps define the origin and every endpoint. Each eligible
    window requires the origin candle and every subsequent one-minute candle,
    all wholly inside the supplied session. Missing minutes return missing_data,
    never the value from a nearby row. Future candles are intentional here and
    must never be passed into online features or treated as model predictions.
    """
    windows = prepare_windows(as_of, session, horizons)
    checked = _checked_candles(candles, 1)
    opening = _aware(session.open, "session open")
    closing = _aware(session.close, "session close")
    origin = _aware(as_of, "as_of")
    by_end = {
        _aware(candle.end, "candle end"): candle
        for candle in checked
        if opening <= _aware(candle.start, "candle start")
        and _aware(candle.end, "candle end") <= closing
    }
    result = []
    for window in windows:
        row = {
            "horizon_minutes": window["horizon_minutes"],
            "target_at": window["target_at"],
            "status": window["status"],
            "return_pct": None,
        }
        if row["status"] == "ready":
            minutes = row["horizon_minutes"]
            required = (origin + timedelta(minutes=offset) for offset in range(minutes + 1))
            if any(timestamp not in by_end for timestamp in required):
                row["status"] = "missing_data"
            else:
                future = by_end[origin + timedelta(minutes=minutes)].close
                row["return_pct"] = 100.0 * (future / by_end[origin].close - 1.0)
        result.append(row)
    return result

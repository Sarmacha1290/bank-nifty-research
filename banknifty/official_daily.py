"""Read-only NSE daily index CSV archive, kept apart from sampled Groww bars."""
from contextlib import closing
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .groww import IST
from .market_calendar import regular_session, regular_session_date


HOST = "nsearchives.nseindia.com"
SOURCE_NAME = "NSE daily all-indices CSV"
MAX_BYTES = 2_000_000
HEADERS = ("Index Name", "Index Date", "Open Index Value", "High Index Value",
           "Low Index Value", "Closing Index Value")


@dataclass(frozen=True)
class OfficialDailyBar:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    source_url: str
    sha256_hex: str
    retrieved_at_utc: datetime

    def __post_init__(self):
        if self.source_url != official_url(self.day):
            raise ValueError("NSE source URL does not match the session date")
        if (len(self.sha256_hex) != 64 or
                any(character not in "0123456789abcdef" for character in self.sha256_hex)):
            raise ValueError("Invalid NSE source hash")
        if self.retrieved_at_utc.tzinfo is None or self.retrieved_at_utc.utcoffset() is None:
            raise ValueError("NSE retrieval time must be timezone-aware")
        if self.retrieved_at_utc <= regular_session(self.day).close.astimezone(timezone.utc):
            raise ValueError("Completed NSE daily OHLC cannot be retrieved before session close")
        for field in ("open", "high", "low", "close"):
            value = getattr(self, field)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"Invalid NSE {field}")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("NSE daily OHLC is inconsistent")

    @property
    def range_points(self):
        return self.high - self.low


def official_url(day):
    if not isinstance(day, date):
        raise TypeError("A calendar date is required")
    allowed, reason = regular_session_date(day)
    if not allowed:
        raise ValueError(f"No verified regular session: {reason}")
    return f"https://{HOST}/content/indices/ind_close_all_{day:%d%m%Y}.csv"


def _price(value, field):
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid NSE {field}") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"Invalid NSE {field}")
    return number


def parse_official_csv(payload, day, source_url, retrieved_at):
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_BYTES:
        raise ValueError("NSE CSV payload is empty or unexpectedly large")
    if source_url != official_url(day):
        raise ValueError("NSE source URL does not match the requested date")
    if (not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is None or
            retrieved_at.utcoffset() is None):
        raise ValueError("A timezone-aware retrieval time is required")
    try:
        content = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("NSE CSV is not UTF-8") from exc
    if content.lstrip().startswith(("<", "{")):
        raise ValueError("NSE did not return a CSV document")
    reader = csv.DictReader(StringIO(content, newline=""))
    if reader.fieldnames is None or not set(HEADERS).issubset(reader.fieldnames):
        raise ValueError("NSE daily CSV header did not match the reviewed format")
    matches = [row for row in reader if row.get("Index Name", "").strip().casefold() == "nifty bank"]
    if len(matches) != 1:
        raise ValueError("Expected exactly one Nifty Bank row in NSE daily CSV")
    row = matches[0]
    try:
        reported_day = datetime.strptime(row["Index Date"].strip(), "%d-%m-%Y").date()
    except (ValueError, AttributeError) as exc:
        raise ValueError("Invalid NSE index date") from exc
    if reported_day != day:
        raise ValueError("NSE daily row date does not match requested date")
    opening, high, low, close = (_price(row[field], field) for field in HEADERS[2:])
    if low > min(opening, close) or high < max(opening, close):
        raise ValueError("NSE daily OHLC is inconsistent")
    return OfficialDailyBar(day, opening, high, low, close, source_url,
                            sha256(payload).hexdigest(),
                            retrieved_at.astimezone(timezone.utc))


def fetch_official_day(day, *, now=None, timeout_seconds=8):
    current = datetime.now(IST) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Current time must be timezone-aware")
    if day >= current.astimezone(IST).date():
        raise ValueError("Only completed prior dates may be fetched")
    url = official_url(day)
    request = Request(url, headers={"Accept": "text/csv", "User-Agent": "BankNiftyResearch/0.30"})
    with urlopen(request, timeout=timeout_seconds) as response:
        final = urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname != HOST:
            raise ValueError("NSE CSV redirected outside the official archive")
        payload = response.read(MAX_BYTES + 1)
        retrieved = datetime.now(timezone.utc)
    return parse_official_csv(payload, day, url, retrieved)


def save_official_day(database, bar):
    if not isinstance(bar, OfficialDailyBar) or bar.source_url != official_url(bar.day):
        raise ValueError("Only validated NSE daily rows may be archived")
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS official_banknifty_daily (
                session_date_ist TEXT PRIMARY KEY, open TEXT NOT NULL,
                high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
                source_url TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                retrieved_at_utc TEXT NOT NULL)""")
            old = connection.execute("SELECT open, high, low, close FROM official_banknifty_daily "
                                     "WHERE session_date_ist=?", (bar.day.isoformat(),)).fetchone()
            prices = tuple(str(value) for value in (bar.open, bar.high, bar.low, bar.close))
            if old is not None:
                if old != prices:
                    raise ValueError("Conflicting NSE OHLC on a previously archived date")
                return "duplicate"
            connection.execute("INSERT INTO official_banknifty_daily VALUES (?,?,?,?,?,?,?,?)",
                               (bar.day.isoformat(), *prices, bar.source_url,
                                bar.sha256_hex, bar.retrieved_at_utc.isoformat()))
    return "inserted"


def load_official_days(database):
    path = Path(database)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='official_banknifty_daily' "
                                  "AND type='table'").fetchone():
            return []
        rows = connection.execute("SELECT session_date_ist, open, high, low, close, source_url, "
                                  "source_sha256, retrieved_at_utc FROM official_banknifty_daily "
                                  "ORDER BY session_date_ist").fetchall()
    return [OfficialDailyBar(date.fromisoformat(row[0]), *(Decimal(value) for value in row[1:5]),
                             row[5], row[6], datetime.fromisoformat(row[7])) for row in rows]


def recent_completed_regular_days(count, *, now=None):
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 30:
        raise ValueError("Request 1 to 30 completed sessions")
    current = datetime.now(IST) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Current time must be timezone-aware")
    day = current.astimezone(IST).date() - timedelta(days=1)
    found = []
    while len(found) < count and day.year == 2026:
        if regular_session_date(day)[0]:
            found.append(day)
        day -= timedelta(days=1)
    return tuple(found)


def sync_recent_official_days(database, count=5, *, now=None):
    """Fetch a small user-requested batch; report gaps instead of inventing rows."""
    days = recent_completed_regular_days(count, now=now)
    result = {"requested": len(days), "inserted": 0, "duplicates": 0, "failed": []}
    for index, day in enumerate(days):
        if index:
            time.sleep(0.1)
        try:
            bar = fetch_official_day(day, now=now)
            outcome = save_official_day(database, bar)
        except (OSError, ValueError, TimeoutError) as exc:
            result["failed"].append((day.isoformat(), type(exc).__name__))
        else:
            result["inserted" if outcome == "inserted" else "duplicates"] += 1
    return result


def older_or_missing_regular_days(database, count=20):
    """Find gaps inside the archive first, then older regular 2026 sessions."""
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
        raise ValueError("Request 1 to 20 older sessions")
    saved = load_official_days(database)
    if not saved:
        raise ValueError("Fetch recent NSE days before requesting older history")
    known = {bar.day for bar in saved}
    result = []
    cursor = saved[-1].day
    while cursor.year == 2026 and len(result) < count:
        if regular_session_date(cursor)[0] and cursor not in known:
            result.append(cursor)
        cursor -= timedelta(days=1)
    return tuple(result)


def sync_older_official_days(database, count=20, *, now=None):
    """Fetch a bounded batch of missing/older sessions with explicit failures."""
    days = older_or_missing_regular_days(database, count)
    result = {"requested": len(days), "inserted": 0, "duplicates": 0, "failed": []}
    for index, day in enumerate(days):
        if index:
            time.sleep(0.2)
        try:
            bar = fetch_official_day(day, now=now)
            outcome = save_official_day(database, bar)
        except (OSError, ValueError, TimeoutError) as exc:
            result["failed"].append((day.isoformat(), type(exc).__name__))
        else:
            result["inserted" if outcome == "inserted" else "duplicates"] += 1
    return result


def export_official_days_csv(database, destination):
    source, target = Path(database), Path(destination)
    if target.resolve() == source.resolve() or (
            source.exists() and target.exists() and source.samefile(target)):
        raise ValueError("Official range export cannot overwrite the market database")
    rows = load_official_days(database)
    if not rows:
        raise ValueError("No archived NSE daily index rows to export")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="",
                                         suffix=".csv", prefix=".banknifty-nse-daily-",
                                         dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream)
            writer.writerow(("session_date_ist", "official_open", "official_high",
                             "official_low", "official_close", "official_range_points",
                             "source", "source_url", "source_sha256", "retrieved_at_utc"))
            for row in rows:
                writer.writerow((row.day.isoformat(), row.open, row.high, row.low,
                                 row.close, row.range_points, SOURCE_NAME, row.source_url,
                                 row.sha256_hex, row.retrieved_at_utc.isoformat()))
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(rows)

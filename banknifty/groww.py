"""Small GET-only data adapter based on official Groww documentation.

Tokens are held in memory. Never put them in URLs, exception text or audit records.
The token itself may have broader permissions; this client exposes data reads only.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import csv
import io
import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .core import Candle

IST = timezone(timedelta(hours=5, minutes=30), "IST")
CATALOG_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"
BASE_URL = "https://api.groww.in/v1"


class DataAccessError(RuntimeError):
    """Contains only a safe, user-readable message."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataAccessError("Unexpected redirect. Connection stopped.")


def resolve_index(csv_text, trading_symbol):
    """Resolve one reviewed NSE cash-index identity from the current catalogue."""
    if trading_symbol not in ("BANKNIFTY", "NIFTY"):
        raise ValueError("Unsupported index symbol")
    rows = [row for row in csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
            if row.get("exchange") == "NSE" and row.get("segment") == "CASH"
            and row.get("instrument_type") == "IDX"
            and row.get("trading_symbol") == trading_symbol]
    if (len(rows) != 1 or rows[0].get("groww_symbol") != "NSE-" + trading_symbol or
            not rows[0].get("exchange_token")):
        raise DataAccessError("Cannot uniquely identify the selected NSE index in Groww's current catalogue.")
    return {key: rows[0][key] for key in
            ("exchange", "segment", "trading_symbol", "groww_symbol", "exchange_token")}


def resolve_banknifty(csv_text):
    return resolve_index(csv_text, "BANKNIFTY")


def resolve_nifty50(csv_text):
    return resolve_index(csv_text, "NIFTY")


def fetch_instrument_catalog_text():
    try:
        with build_opener(NoRedirect()).open(CATALOG_URL, timeout=30) as response:
            body = response.read(32_000_001)
        if len(body) > 32_000_000:
            raise DataAccessError("Instrument catalogue exceeds expected size.")
        return body.decode("utf-8-sig")
    except (URLError, TimeoutError, UnicodeError, OSError):
        raise DataAccessError("Could not retrieve Groww's instrument catalogue. Check the connection.") from None


def fetch_banknifty_instrument():
    return resolve_banknifty(fetch_instrument_catalog_text())


@dataclass
class GrowwDataClient:
    token: str = field(repr=False)

    def __post_init__(self):
        self.token = self.token.strip()
        if not self.token or any(c.isspace() for c in self.token):
            raise DataAccessError("Enter a valid access token without spaces.")

    def close(self):
        self.token = ""

    def _get(self, path, params, *, exact_decimals=False):
        if path not in ("/live-data/ltp", "/live-data/quote", "/historical/candles",
                        "/option-chain/exchange/NSE/underlying/BANKNIFTY"):
            raise DataAccessError("This application only supports approved market-data reads.")
        if not self.token:
            raise DataAccessError("The access token has been cleared. Enter it again.")
        request = Request(BASE_URL + path + "?" + urlencode(params), method="GET", headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json", "X-API-VERSION": "1.0"})
        try:
            with build_opener(NoRedirect()).open(request, timeout=25) as response:
                body = response.read(16_000_001)
            if len(body) > 16_000_000:
                raise DataAccessError("Response exceeds the expected size.")
            if exact_decimals:
                from .provider_adapters import decode_provider_json
                data = decode_provider_json(body)
            else:
                data = json.loads(body)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (401, 403):
                raise DataAccessError("Groww rejected access. Check the token and Trading API subscription.") from None
            if code == 429:
                raise DataAccessError("Groww's request limit was reached. Please try later.") from None
            raise DataAccessError("Groww could not serve the market-data request (HTTP " + str(code) + ").") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise DataAccessError("Could not read market data. Check the connection and try again.") from None
        if not isinstance(data, dict) or data.get("status") != "SUCCESS":
            raise DataAccessError("Groww did not return a successful data response. Check access and parameters.")
        return data

    def index_snapshot(self, instrument):
        symbol = instrument["exchange"] + "_" + instrument["trading_symbol"]
        result = self._get("/live-data/ltp", {"segment": "CASH", "exchange_symbols": symbol})
        value = result.get("payload", {}).get(symbol)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise DataAccessError("Groww returned no valid Bank Nifty value.")
        return {"source": "Groww", "symbol": instrument["groww_symbol"],
                "received_at": datetime.now(timezone.utc).isoformat(), "value": value,
                "exchange_timestamp": None, "freshness": "unverified",
                "purpose": "connection check; this response has no exchange timestamp"}

    def historical_candles(self, instrument, start, end):
        for value in (start, end):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Historical request times must include a timezone.")
        if not start < end or end - start > timedelta(days=30):
            raise ValueError("One-minute requests must span more than zero and at most 30 days.")
        return self._get("/historical/candles", {
            "exchange": instrument["exchange"], "segment": instrument["segment"],
            "groww_symbol": instrument["groww_symbol"], "candle_interval": "1minute",
            "start_time": int(start.timestamp()), "end_time": int(end.timestamp())})

    def banknifty_option_chain(self, expiry_date):
        """Read a named expiry. The response has no documented exchange timestamp."""
        if not isinstance(expiry_date, date) or isinstance(expiry_date, datetime):
            raise ValueError("Select an explicit Bank Nifty expiry date")
        return self._get("/option-chain/exchange/NSE/underlying/BANKNIFTY",
                         {"expiry_date": expiry_date.isoformat()})

    def equity_quote(self, trading_symbol):
        """Read one NSE cash quote; caller validates fields and receipt time."""
        if not isinstance(trading_symbol, str) or not trading_symbol or not trading_symbol.replace("-", "").isalnum():
            raise ValueError("Invalid NSE equity trading symbol")
        return self._get("/live-data/quote", {"exchange": "NSE", "segment": "CASH",
                                               "trading_symbol": trading_symbol})

    def banknifty_future_quote(self, trading_symbol):
        """Read one catalogue-resolved NSE Bank Nifty future; never place an order."""
        if (not isinstance(trading_symbol, str) or
                not trading_symbol.startswith("BANKNIFTY") or
                not trading_symbol.endswith("FUT") or
                not trading_symbol.isascii() or not trading_symbol.isalnum()):
            raise ValueError("Invalid Bank Nifty futures trading symbol")
        return self._get("/live-data/quote", {"exchange": "NSE", "segment": "FNO",
                                               "trading_symbol": trading_symbol})

    def option_quote(self, instrument):
        """Read an explicitly catalogue-resolved Bank Nifty option snapshot.

        Accepts ProviderInstrument only. Existing GET allowlist, response limit,
        timeout and redirect controls still apply. This response does not claim
        quote/OI timestamps; map it with provider_adapters.adapt_option_quote.
        """
        from .provider_adapters import plan_option_quote
        plan = plan_option_quote(instrument)
        return self._get(plan.path, dict(plan.params), exact_decimals=True)


def normalize_candles(response, *, symbol, kind, timestamp_basis, naive_timezone=None):
    """Explicit conventions required until provider timestamp semantics are verified."""
    if timestamp_basis not in ("start", "end"):
        raise ValueError("Confirm whether provider timestamps identify candle start or end.")
    if response.get("status") != "SUCCESS":
        raise ValueError("Cannot normalize a failed response.")
    payload = response.get("payload", {})
    if payload.get("interval_in_minutes") != 1:
        raise ValueError("Expected confirmed one-minute candles.")
    rows = payload.get("candles")
    if not isinstance(rows, list) or not rows:
        raise ValueError("No candle data returned.")
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) not in (5, 6, 7):
            raise ValueError("Unexpected Groww candle structure.")
        stamp = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            if naive_timezone is None:
                raise ValueError("Confirm the timezone of provider timestamps before normalizing.")
            stamp = stamp.replace(tzinfo=naive_timezone)
        start = stamp if timestamp_basis == "start" else stamp - timedelta(minutes=1)
        result.append(Candle(symbol=symbol, kind=kind, start=start, end=start + timedelta(minutes=1),
            open=row[1], high=row[2], low=row[3], close=row[4],
            volume=row[5] if len(row) > 5 else None,
            open_interest=row[6] if len(row) > 6 else None, source="Groww"))
    return result

"""Parse and verify a dated NSE Indices Nifty Bank weightage report.

The result is reference data, not an intraday point-in-time weight snapshot.
An as-of date alone does not establish publication time or later live weights.
"""

from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import re
from urllib.parse import urlparse


TITLE = "Constituents of NIFTY Bank"
ROW = re.compile(r"^([A-Z0-9]+)\s+(.+?)\s+Financial Services\s+"
                 r"([0-9]+(?:\.[0-9]+)?)\s+([0-9]+)\s+([0-9]+(?:\.[0-9]+)?)$")
HASH = re.compile(r"^[0-9a-f]{64}$")


def _official_url(value):
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in ("niftyindices.com", "www.niftyindices.com"):
        raise ValueError("Expected an NSE Indices HTTPS source URL")


def _validate_rows(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 14:
        raise ValueError("Nifty Bank report must contain a complete set of at most 14 rows")
    if any(not isinstance(item.get("symbol"), str) or
           not re.fullmatch(r"[A-Z0-9]+", item["symbol"]) for item in rows):
        raise ValueError("Invalid constituent symbol")
    if len({item["symbol"] for item in rows}) != len(rows):
        raise ValueError("Duplicate constituent symbol")
    if any(not isinstance(item["weight_percent"], (int, float)) or
           not isfinite(item["weight_percent"]) or item["weight_percent"] <= 0
           for item in rows):
        raise ValueError("Invalid constituent weight")
    if abs(sum(item["weight_percent"] for item in rows) - 100.0) > 0.1:
        raise ValueError("Constituent weights do not sum to 100 percent")


def parse_nifty_bank_report(text, *, source_url, source_pdf_sha256, retrieved_at):
    if not isinstance(text, str) or TITLE not in text:
        raise ValueError("Not an NSE Indices Nifty Bank constituent report")
    _official_url(source_url)
    if not isinstance(source_pdf_sha256, str) or not HASH.fullmatch(source_pdf_sha256):
        raise ValueError("Source PDF SHA-256 is required")
    if not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise ValueError("Retrieval timestamp must be timezone-aware")
    lines = [line.strip() for line in text.splitlines()]
    try:
        title_index = lines.index(TITLE)
        as_of = datetime.strptime(lines[title_index + 1], "%B %d, %Y").date()
    except (ValueError, IndexError) as exc:
        raise ValueError("Report date is missing or ambiguous") from exc
    rows = []
    for line in lines:
        match = ROW.fullmatch(line)
        if match:
            symbol, company, close, market_cap, weight = match.groups()
            rows.append({"symbol": symbol, "company": company,
                         "close_price_reported": float(close),
                         "index_mcap_crore_reported": int(market_cap),
                         "weight_percent": float(weight)})
    _validate_rows(rows)
    return {
        "index": "NIFTY Bank",
        "as_of_date": as_of.isoformat(),
        "retrieved_at_utc": retrieved_at.astimezone(timezone.utc).isoformat(),
        "source_url": source_url,
        "source_pdf_sha256": source_pdf_sha256,
        "reference_only": True,
        "usage_note": "Dated report; not proof of live intraday weights or publication time",
        "constituents": rows,
    }


def load_reference_weights(path):
    """Validate a saved report before displaying it; never infer live validity."""
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    if saved.get("index") != "NIFTY Bank" or saved.get("reference_only") is not True:
        raise ValueError("Not a reference-only NIFTY Bank report")
    datetime.fromisoformat(saved["as_of_date"])
    retrieved = datetime.fromisoformat(saved["retrieved_at_utc"])
    if retrieved.tzinfo is None or retrieved.utcoffset() is None:
        raise ValueError("Reference retrieval time is missing a timezone")
    _official_url(saved["source_url"])
    if not HASH.fullmatch(saved["source_pdf_sha256"]):
        raise ValueError("Reference source hash is invalid")
    _validate_rows(saved["constituents"])
    return saved

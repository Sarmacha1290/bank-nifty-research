"""Import one public NSE Indices monthly Bank weightage PDF as dated reference.

Requires pdfplumber for this optional import step. The main dashboard does not
need pdfplumber. This never marks the report as current intraday weights.
"""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zipfile import ZipFile

from banknifty.nse_weights import parse_nifty_bank_report


DEFAULT_URL = "https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataSep2026.zip"
DEFAULT_MEMBER = "NIFTY_Bank_Sep2026.pdf"


def import_report(url, member, output):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ("niftyindices.com", "www.niftyindices.com"):
        raise ValueError("Only the official NSE Indices HTTPS source is accepted")
    if Path(member).name != member or not member.lower().endswith(".pdf"):
        raise ValueError("Member must be one PDF filename inside the official ZIP")
    target = Path(output)
    if target.exists():
        raise ValueError("Reference file already exists; choose a new dated filename")
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError("This optional import requires pdfplumber") from exc
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as response:
        archive = response.read(10_000_001)
    if len(archive) > 10_000_000:
        raise ValueError("Official ZIP is unexpectedly large")
    with ZipFile(io.BytesIO(archive)) as zipped:
        info = zipped.getinfo(member)
        if info.file_size > 2_000_000:
            raise ValueError("Bank weightage PDF is unexpectedly large")
        pdf = zipped.read(info)
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        if len(document.pages) != 1:
            raise ValueError("Expected a one-page Bank weightage report")
        extracted = document.pages[0].extract_text() or ""
    report = parse_nifty_bank_report(extracted, source_url=url,
                                     source_pdf_sha256=sha256(pdf).hexdigest(),
                                     retrieved_at=datetime.now(timezone.utc))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description="Import official dated Bank Nifty weights as reference only")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--member", default=DEFAULT_MEMBER)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).parent / "reference" / "nse-bank-weights-2026-09-30.json")
    args = parser.parse_args()
    report = import_report(args.url, args.member, args.output)
    print(f"Saved {len(report['constituents'])} complete reference weights as of "
          f"{report['as_of_date']}; not marked current or forecast-ready.")


if __name__ == "__main__":
    main()

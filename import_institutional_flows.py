"""Stage a manually normalized institutional cash-flow report for review.

The source file must be independently checked. This command does not establish
that a file came from NSE and does not enable a forecast or order placement.
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path

from banknifty.institutional_flows import SCOPES, stage_file


def main():
    parser = argparse.ArgumentParser(description="Stage unverified delayed cash-flow context")
    parser.add_argument("csv_file", type=Path, help="Downloaded NSE or normalized CSV")
    parser.add_argument("--format", choices=("nse_download", "normalized"),
                        default="nse_download", dest="file_format")
    parser.add_argument("--scope", required=True, choices=sorted(SCOPES),
                        help="Select the scope checked against the report download control")
    parser.add_argument("--database", type=Path,
                        default=Path(__file__).parent / "data" / "market-data.sqlite3")
    args = parser.parse_args()
    result = stage_file(args.database, args.csv_file, scope=args.scope,
                        observed_at=datetime.now(timezone.utc),
                        file_format=args.file_format)
    print(f"Staged {result['rows']} {result['scope']} rows for {result['report_date']} "
          f"as unverified manual data. First observed {result['first_observed_utc']}. "
          "No forecast use.")


if __name__ == "__main__":
    main()

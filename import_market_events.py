"""Stage a source-linked event CSV for manual inspection, never trading."""
import argparse
from pathlib import Path

from banknifty.event_context import stage_event_csv


def main():
    parser = argparse.ArgumentParser(description="Stage bank and macro events for research")
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--database", type=Path,
                        default=Path(__file__).parent / "data" / "market-data.sqlite3")
    args = parser.parse_args()
    report = stage_event_csv(args.database, args.csv_file)
    print(f"Staged {report['inserted']} of {report['parsed']} records. "
          "Manual provenance unverified; no forecast or trading use.")


if __name__ == "__main__":
    main()

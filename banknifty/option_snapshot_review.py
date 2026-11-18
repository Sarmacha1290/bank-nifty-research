"""Retrospective receipt-time comparison of saved Groww option chains.

The chain has no exchange snapshot timestamp. Differences in reported OI or
volume are descriptive and cannot identify buyer/seller trade direction.
"""

from datetime import date, datetime, timedelta
import csv
import os
from pathlib import Path
import re
import tempfile

from .groww import IST
from .option_history import load_option_chain_snapshots


MAX_GAP = timedelta(minutes=30)
OPTION_SYMBOL = re.compile(r"BANKNIFTY[A-Z0-9]+\Z")


def _contracts(snapshot):
    try:
        contracts = snapshot["contracts"]
        if not isinstance(contracts, (list, tuple)) or not contracts:
            raise ValueError("Empty option contract set")
        indexed = {}
        for row in contracts:
            symbol = row["trading_symbol"]
            strike = row["strike"]
            side = row["side"]
            if (not isinstance(symbol, str) or OPTION_SYMBOL.fullmatch(symbol) is None or
                    not isinstance(strike, (int, float)) or isinstance(strike, bool) or
                    strike <= 0 or int(strike) != strike or side not in ("CE", "PE") or
                    not symbol.endswith(f"{int(strike)}{side}") or
                    any(isinstance(row[field], bool) or not isinstance(row[field], int) or
                        row[field] < 0 for field in ("open_interest", "volume")) or
                    symbol in indexed):
                raise ValueError("Invalid or duplicate saved option contract")
            indexed[symbol] = row
        return indexed
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("Saved option contract is malformed") from exc


def compare_saved_option_snapshots(previous, current):
    """Compare an ordered same-expiry pair without inventing market freshness."""
    if (previous.get("source") != "Groww Bank Nifty option-chain REST" or
            current.get("source") != previous.get("source") or
            previous.get("expiry_date") != current.get("expiry_date")):
        raise ValueError("Same-expiry Groww option snapshots are required")
    try:
        date.fromisoformat(current["expiry_date"])
        earlier = datetime.fromisoformat(previous["received_at_utc"])
        later = datetime.fromisoformat(current["received_at_utc"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Option receipt times are invalid") from exc
    if (earlier.tzinfo is None or earlier.utcoffset() is None or
            later.tzinfo is None or later.utcoffset() is None or
            not earlier < later or later - earlier > MAX_GAP or
            earlier.astimezone(IST).date() != later.astimezone(IST).date()):
        raise ValueError("Option snapshots require ordered same-day receipts within 30 minutes")
    if previous.get("exchange_at") is not None or current.get("exchange_at") is not None or (
            previous.get("freshness") != "unverified_exchange_time_absent" or
            current.get("freshness") != "unverified_exchange_time_absent"):
        raise ValueError("Option exchange freshness must remain unverified")
    old = _contracts(previous)
    new = _contracts(current)
    common = sorted(old.keys() & new.keys())
    rows = []
    for symbol in common:
        first, second = old[symbol], new[symbol]
        if (first["strike"] != second["strike"] or first["side"] != second["side"]):
            raise ValueError("Option contract identity or counts changed invalidly")
        rows.append({"symbol": symbol, "strike": first["strike"], "side": first["side"],
                     "oi_change_reported": second["open_interest"] - first["open_interest"],
                     "volume_change_reported": second["volume"] - first["volume"]})
    same_universe = old.keys() == new.keys()
    both_complete = all(snapshot.get("status") == "receipt_time_only_complete"
                        for snapshot in (previous, current))
    aggregate_ok = same_universe and both_complete
    return {
        "status": "comparable_contract_universe" if aggregate_ok else "partial_or_changed_universe",
        "expiry_date": current["expiry_date"],
        "previous_received_at_utc": earlier.isoformat(),
        "current_received_at_utc": later.isoformat(),
        "receipt_gap_seconds": (later - earlier).total_seconds(),
        "previous_contracts": len(old), "current_contracts": len(new),
        "common_contracts": len(common), "contract_rows": rows,
        "removed_contracts": [old[symbol] for symbol in sorted(old.keys() - new.keys())],
        "new_contracts": [new[symbol] for symbol in sorted(new.keys() - old.keys())],
        "call_oi_change_reported": (sum(row["oi_change_reported"] for row in rows
                                        if row["side"] == "CE") if aggregate_ok else None),
        "put_oi_change_reported": (sum(row["oi_change_reported"] for row in rows
                                       if row["side"] == "PE") if aggregate_ok else None),
        "call_volume_change_reported": (sum(row["volume_change_reported"] for row in rows
                                            if row["side"] == "CE") if aggregate_ok else None),
        "put_volume_change_reported": (sum(row["volume_change_reported"] for row in rows
                                           if row["side"] == "PE") if aggregate_ok else None),
        "interpretation": "reported_receipt_time_changes_not_trade_side_or_exchange_freshness",
    }


def review_latest_saved_option_pair(database):
    snapshots = load_option_chain_snapshots(database)
    if len(snapshots) < 2:
        return {"status": "need_two_saved_snapshots", "snapshots": len(snapshots),
                "comparison": None}
    latest = snapshots[-1]
    for prior in reversed(snapshots[:-1]):
        if prior.get("expiry_date") != latest.get("expiry_date"):
            continue
        try:
            comparison = compare_saved_option_snapshots(prior, latest)
        except ValueError:
            continue
        return {"status": "receipt_time_pair_reviewed", "snapshots": len(snapshots),
                "comparison": comparison}
    return {"status": "no_recent_same_expiry_pair", "snapshots": len(snapshots),
            "comparison": None}


def export_latest_option_changes_csv(database, destination):
    source, target = Path(database), Path(destination)
    if (target.resolve() == source.resolve() or
            (source.exists() and target.exists() and source.samefile(target))):
        raise ValueError("Option change CSV cannot overwrite the market archive")
    review = review_latest_saved_option_pair(source)
    result = review["comparison"]
    if result is None:
        raise ValueError("No recent same-expiry option pair to export")
    fields = ("expiry_date", "previous_received_at_utc", "current_received_at_utc",
              "receipt_gap_seconds", "comparison_status", "symbol", "strike", "side",
              "availability", "oi_change_reported", "volume_change_reported",
              "call_oi_change_reported", "put_oi_change_reported",
              "interpretation")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="",
                prefix=".option-changes-", suffix=".csv", dir=target.parent,
                delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            common = {"expiry_date": result["expiry_date"],
                      "previous_received_at_utc": result["previous_received_at_utc"],
                      "current_received_at_utc": result["current_received_at_utc"],
                      "receipt_gap_seconds": result["receipt_gap_seconds"],
                      "comparison_status": result["status"],
                      "call_oi_change_reported": result["call_oi_change_reported"],
                      "put_oi_change_reported": result["put_oi_change_reported"],
                      "interpretation": result["interpretation"]}
            for row in result["contract_rows"]:
                writer.writerow({**common, "symbol": row["symbol"], "strike": row["strike"],
                                 "side": row["side"], "availability": "both",
                                 "oi_change_reported": row["oi_change_reported"],
                                 "volume_change_reported": row["volume_change_reported"]})
            for availability, key in (("previous_only", "removed_contracts"),
                                      ("current_only", "new_contracts")):
                for row in result[key]:
                    writer.writerow({**common, "symbol": row["trading_symbol"],
                                     "strike": row["strike"], "side": row["side"],
                                     "availability": availability})
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return (len(result["contract_rows"]) + len(result["removed_contracts"]) +
            len(result["new_contracts"]))

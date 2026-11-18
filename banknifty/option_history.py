"""Archive sanitized option-chain observations without implying exchange freshness."""
from dataclasses import asdict
from contextlib import closing
import csv
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .agents.options_futures import (OptionChainAnalysis, OptionContract, SOURCE,
                                     analyze_option_chain)
from .storage import record_event


def save_option_chain_analysis(database, analysis):
    if not isinstance(analysis, OptionChainAnalysis) or analysis.source != SOURCE or (
            analysis.status not in ("receipt_time_only_complete", "receipt_time_only_partial") or
            analysis.exchange_at is not None):
        raise ValueError("Only receipt-time-only Groww option-chain analysis may be archived")
    if (not isinstance(analysis.expiry_date, date) or isinstance(analysis.expiry_date, datetime)
            or not isinstance(analysis.received_at_utc, datetime)
            or analysis.received_at_utc.tzinfo is None
            or analysis.received_at_utc.utcoffset() is None
            or not isinstance(analysis.contracts, tuple)
            or not 1 <= len(analysis.contracts) <= 2000):
        raise ValueError("Option-chain archive metadata is invalid")
    strikes = {}
    try:
        for contract in analysis.contracts:
            if not isinstance(contract, OptionContract) or contract.side not in ("CE", "PE"):
                raise ValueError("Invalid option contract")
            strike = str(int(contract.strike))
            if contract.strike != float(strike):
                raise ValueError("Invalid option strike")
            sides = strikes.setdefault(strike, {})
            if contract.side in sides:
                raise ValueError("Duplicate option contract")
            sides[contract.side] = {
                "trading_symbol": contract.trading_symbol,
                "open_interest": contract.open_interest,
                "volume": contract.volume,
                "ltp": contract.ltp,
                "greeks": {"iv": contract.iv_percent, "delta": contract.delta,
                           "gamma": contract.gamma, "theta": contract.theta,
                           "vega": contract.vega, "rho": contract.rho},
            }
        rebuilt = analyze_option_chain(
            {"status": "SUCCESS", "payload": {"underlying_ltp": analysis.underlying_ltp,
                                              "strikes": strikes}},
            analysis.expiry_date, analysis.received_at_utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Option-chain archive analysis failed source validation") from exc
    if rebuilt != analysis:
        raise ValueError("Option-chain archive totals or provenance are inconsistent")
    payload = asdict(analysis)
    payload["expiry_date"] = analysis.expiry_date.isoformat()
    payload["received_at_utc"] = analysis.received_at_utc.isoformat()
    payload["contracts"] = [asdict(row) for row in analysis.contracts]
    payload["freshness"] = "unverified_exchange_time_absent"
    return record_event(database, "option_chain_snapshot", payload)


def load_option_chain_snapshots(database):
    path = Path(database)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='events' AND type='table'").fetchone() is None:
            return []
        rows = connection.execute(
            "SELECT payload, sha256 FROM events WHERE event_type='option_chain_snapshot' "
            "ORDER BY recorded_at, id").fetchall()
    snapshots = []
    for payload, digest in rows:
        if sha256(payload.encode()).hexdigest() != digest:
            raise ValueError("Option-chain audit record failed its integrity check")
        record = json.loads(payload)
        if record.get("exchange_at") is not None or record.get("freshness") != "unverified_exchange_time_absent":
            raise ValueError("Option-chain snapshot has invalid freshness metadata")
        snapshots.append(record)
    return snapshots


def count_option_chain_snapshots(database):
    return len(load_option_chain_snapshots(database))


def export_option_chain_csv(database, destination):
    source, target = Path(database), Path(destination)
    if target.resolve() == source.resolve() or (
        target.exists() and source.exists() and target.samefile(source)
    ):
        raise ValueError("Export destination must not overwrite the archive database")
    snapshots = load_option_chain_snapshots(database)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("expiry_date", "received_at_utc", "exchange_at_utc", "trading_symbol",
                         "strike", "side", "ltp", "open_interest", "volume", "iv_percent",
                         "delta", "gamma", "theta", "vega", "rho",
                         "source", "freshness"))
        count = 0
        for snapshot in snapshots:
            for contract in snapshot["contracts"]:
                writer.writerow((snapshot["expiry_date"], snapshot["received_at_utc"], "",
                                 contract["trading_symbol"], contract["strike"], contract["side"],
                                 contract["ltp"], contract["open_interest"], contract["volume"],
                                 contract["iv_percent"], contract.get("delta"),
                                 contract.get("gamma"), contract.get("theta"),
                                 contract.get("vega"), contract.get("rho"),
                                 snapshot["source"], snapshot["freshness"]))
                count += 1
    return count

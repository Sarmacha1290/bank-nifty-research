"""Local, append-only research signals and *user-reported* manual executions.

This module has no transport, broker client, credential or execution interface.
Recording a signal never records a fill. Hash chaining detects accidental edits;
it is not an externally anchored signature or protection against a database owner.
Corrections are new events, and every exposure report is reconstructed from them.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping


class LedgerError(ValueError):
    """Invalid manual ledger operation; no event was committed."""


class DuplicateEvent(LedgerError):
    pass


class IntegrityError(LedgerError):
    pass


_ZERO_HASH = "0" * 64
_LEDGER_VALIDATION_BYTES = 64 * 1024 * 1024
_LEDGER_VALIDATION_EVENTS = 100_000
_SECRET_KEYS = {"token", "access_token", "api_key", "api_secret", "password",
                "authorization", "secret", "credentials", "private_key"}


def _json_value(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_value(value.to_dict())
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or key.lower().replace("-", "_") in _SECRET_KEYS:
                raise LedgerError("Only string keys without credential fields are accepted")
            result[key] = _json_value(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime):
        return _stamp(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise LedgerError("Non-finite numeric value")
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise LedgerError("Payload must contain finite JSON-compatible values")


def _canonical(value: Any) -> str:
    result = json.dumps(_json_value(value), ensure_ascii=True, allow_nan=False,
                        separators=(",", ":"), sort_keys=True)
    if len(result) > 2_000_000:
        raise LedgerError("Event exceeds the 2 MB local ledger limit")
    return result


def _stamp(value: datetime | str) -> str:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (TypeError, ValueError, OverflowError, AttributeError) as exc:
        raise LedgerError("An aware timestamp is required") from exc


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise LedgerError(f"Invalid {name}")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LedgerError(f"{name} must be a positive integer number of units")
    return value


def _price(value: Any) -> str:
    try:
        if isinstance(value, bool):
            raise ValueError
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0:
            raise ValueError
        return str(number.normalize())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LedgerError("Manual fill price must be finite and positive") from exc


def _fees(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
        if isinstance(value, bool) or not number.is_finite() or number < 0:
            raise ValueError
        return str(number.normalize())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LedgerError("Fees must be finite, nonnegative or explicitly unknown") from exc


def _plan(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if payload.get("status") == "no_trade":
        if payload.get("legs"):
            raise LedgerError("A no-trade signal cannot contain executable legs")
        return {}
    legs = payload.get("legs", [])
    if not isinstance(legs, list) or not 1 <= len(legs) <= 2:
        raise LedgerError("A candidate requires one long leg or a two-leg vertical spread")
    as_of = _stamp(payload.get("as_of"))
    plan = {}
    for leg in legs:
        if not isinstance(leg, dict):
            raise LedgerError("Invalid signal leg")
        contract = leg.get("contract", leg)
        if not isinstance(contract, dict):
            raise LedgerError("Invalid contract metadata")
        contract_id = _identifier(contract.get("contract_id"), "contract_id")
        if contract_id in plan or leg.get("side") not in {"BUY", "SELL"}:
            raise LedgerError("Duplicate contract or invalid leg side")
        for field in ("underlying", "exchange"):
            _identifier(contract.get(field), field)
        if contract.get("option_type") not in {"CALL", "PUT"} or contract.get("listed") is not True:
            raise LedgerError("Explicit listed CALL/PUT contract metadata is required")
        _price(contract.get("strike"))
        _price(contract.get("tick_size"))
        if (_stamp(contract.get("known_at")) > as_of
                or _stamp(contract.get("effective_from")) > as_of
                or _stamp(contract.get("expiry_at")) <= as_of
                or (contract.get("effective_until") is not None and _stamp(contract["effective_until"]) <= as_of)):
            raise LedgerError("Contract metadata must be known and effective at the signal cutoff")
        plan[contract_id] = {"side": leg["side"],
                             "ratio": _positive_int(leg.get("ratio", 1), "leg ratio"),
                             "lot_size": _positive_int(contract.get("lot_size"), "lot_size"),
                             "contract": contract}
    if len(plan) == 1 and next(iter(plan.values()))["side"] != "BUY":
        raise LedgerError("Naked selling is not a supported signal")
    if len(plan) == 2 and {leg["side"] for leg in plan.values()} != {"BUY", "SELL"}:
        raise LedgerError("Two-leg signals require an explicit long hedge")
    if any(leg["ratio"] != 1 for leg in plan.values()):
        raise LedgerError("Only equal-unit single-ratio structures are supported")
    if len(plan) == 2:
        contracts = [leg["contract"] for leg in plan.values()]
        keys = ("underlying", "exchange", "expiry_at", "option_type", "lot_size")
        if any(contracts[0][key] != contracts[1][key] for key in keys) or Decimal(str(contracts[0]["strike"])) == Decimal(str(contracts[1]["strike"])):
            raise LedgerError("Only compatible distinct-strike vertical spreads are supported")
    quantity = payload.get("quantity")
    if quantity is not None:
        _positive_int(quantity, "signal quantity")
        for leg in plan.values():
            if quantity * leg["ratio"] % leg["lot_size"]:
                raise LedgerError("Signal units must match the dated contract lot sizes")
    return plan


def _fill_payload(signal_id: str, contract_id: str, side: str, quantity: int,
                  price: Any, occurred_at: datetime | str,
                  external_fill_id: str | None = None, fees: Any = None) -> dict[str, Any]:
    if side not in {"BUY", "SELL"}:
        raise LedgerError("Manual fill side must be BUY or SELL")
    return {"signal_id": _identifier(signal_id, "signal_id"),
            "contract_id": _identifier(contract_id, "contract_id"), "side": side,
            "quantity": _positive_int(quantity, "fill quantity"), "price": _price(price),
            "occurred_at": _stamp(occurred_at),
            "external_fill_id": None if external_fill_id is None else _identifier(external_fill_id, "external_fill_id"),
            "fees": _fees(fees),
            "origin": "user_reported_manual_fill"}


def _state(events: list[dict[str, Any]]) -> tuple[dict, dict, dict]:
    signals, fills, latest = {}, {}, {}
    for event in events:
        payload = event["payload"]
        if event["kind"] == "signal":
            if hashlib.sha256(_canonical(payload["signal"]).encode("utf-8")).hexdigest() != payload["frozen_payload_sha256"]:
                raise IntegrityError("Frozen signal payload fingerprint failed")
            if payload["signal_id"] in signals:
                raise IntegrityError("Duplicate frozen signal")
            signals[payload["signal_id"]] = payload["signal"]
        elif event["kind"] == "fill":
            fills[event["event_id"]] = payload
            latest[event["event_id"]] = event["event_id"]
        elif event["kind"] == "correction":
            target = payload["target_event_id"]
            if target not in fills or payload["supersedes"] != latest[target]:
                raise IntegrityError("Correction history is inconsistent")
            fills[target] = payload["replacement"]
            latest[target] = event["event_id"]
        else:
            raise IntegrityError("Unsupported ledger event kind")
    return signals, fills, latest


def _validate_fills(signals: dict, fills: dict) -> dict[str, dict[str, int]]:
    positions = {sid: {cid: 0 for cid in _plan(signal)} for sid, signal in signals.items()}
    opened = {sid: dict.fromkeys(positions[sid], 0) for sid in signals}
    seen, external, previous_time = set(), set(), {}
    for fill in fills.values():
        if fill is None:
            continue
        sid = fill["signal_id"]
        if sid not in signals:
            raise LedgerError("Unknown signal")
        signal, plan = signals[sid], _plan(signals[sid])
        cid = fill["contract_id"]
        if cid not in plan:
            raise LedgerError("A fill must belong to an explicit candidate leg; no-trade signals cannot fill")
        leg = plan[cid]
        if fill["quantity"] % leg["lot_size"]:
            raise LedgerError("Fill units do not match the dated lot size")
        cutoff = _stamp(signal.get("as_of"))
        if fill["occurred_at"] < cutoff:
            raise LedgerError("A fill cannot occur before the frozen signal")
        if fill["occurred_at"] < previous_time.get(sid, cutoff):
            raise LedgerError("Manual fills must be entered in occurrence order")
        previous_time[sid] = fill["occurred_at"]
        fingerprint = _canonical({key: value for key, value in fill.items() if key not in {"fees", "origin"}})
        if fingerprint in seen:
            raise DuplicateEvent("Identical manual fill already recorded")
        seen.add(fingerprint)
        if fill.get("external_fill_id"):
            if fill["external_fill_id"] in external:
                raise DuplicateEvent("External fill reference already recorded")
            external.add(fill["external_fill_id"])
        sign = 1 if leg["side"] == "BUY" else -1
        current = positions[sid][cid] * sign
        opening = fill["side"] == leg["side"]
        if opening:
            opened[sid][cid] += fill["quantity"]
            limit = signal.get("quantity")
            if limit is not None and opened[sid][cid] > limit * leg["ratio"]:
                raise LedgerError("Manual opening exceeds the frozen signal quantity")
            current += fill["quantity"]
        else:
            if fill["quantity"] > current:
                raise LedgerError("Manual close would over-close or reverse the planned position")
            current -= fill["quantity"]
        positions[sid][cid] = current * sign
    return positions


class OptionsLedger:
    """Separate local SQLite event file; each method opens and closes a transaction."""

    def __init__(self, path: str | Path, *, read_only: bool = False):
        if type(read_only) is not bool:
            raise LedgerError("read_only must be boolean")
        self.path = Path(path).resolve()
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                raise LedgerError("Ledger does not exist; read-only access will not create it")
            with self._connect() as db:
                self._read(db)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if existing - {"options_events"}:
                raise LedgerError("Use a separate options ledger file, not the market database")
            db.execute("""CREATE TABLE IF NOT EXISTS options_events (
                sequence INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL,
                previous_sha256 TEXT NOT NULL, sha256 TEXT NOT NULL)""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS options_events_no_update
                BEFORE UPDATE ON options_events BEGIN SELECT RAISE(ABORT, 'append-only ledger'); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS options_events_no_delete
                BEFORE DELETE ON options_events BEGIN SELECT RAISE(ABORT, 'append-only ledger'); END""")
            self._read(db)

    @contextmanager
    def _connect(self):
        db = None
        try:
            db = (sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
                  if self.read_only else sqlite3.connect(self.path, timeout=10))
            with db:
                yield db
        except sqlite3.DatabaseError:
            raise IntegrityError("Ledger database could not be safely read or updated") from None
        finally:
            if db is not None:
                db.close()

    def _read(self, db) -> list[dict[str, Any]]:
        result, prior = [], _ZERO_HASH
        try:
            rows = db.execute("SELECT * FROM options_events ORDER BY sequence").fetchall()
            for expected, row in enumerate(rows, 1):
                sequence, event_id, kind, recorded_at, raw, previous, digest = row
                payload = json.loads(raw)
                event = {"sequence": sequence, "event_id": event_id, "kind": kind,
                         "recorded_at": recorded_at, "payload": payload, "previous_sha256": previous}
                calculated = hashlib.sha256(_canonical(event).encode("utf-8")).hexdigest()
                if sequence != expected or previous != prior or digest != calculated or raw != _canonical(payload):
                    raise IntegrityError("Ledger event hash chain failed integrity verification")
                if result and recorded_at < result[-1]["recorded_at"]:
                    raise IntegrityError("Ledger recording timestamps regressed")
                event["sha256"] = digest
                result.append(event)
                prior = digest
            signals, fills, _ = _state(result)
            _validate_fills(signals, fills)
            return result
        except (json.JSONDecodeError, KeyError, TypeError, sqlite3.DatabaseError) as exc:
            raise IntegrityError("Invalid ledger structure") from exc

    def _append(self, db, events, event_id, kind, recorded_at, payload, *, pre_commit=None):
        if self.read_only:
            raise LedgerError("Read-only ledger cannot append events")
        event_id = _identifier(event_id, "event_id")
        if any(item["event_id"] == event_id for item in events):
            raise DuplicateEvent("Event ID already recorded")
        recorded = _stamp(recorded_at)
        if events and recorded < events[-1]["recorded_at"]:
            raise LedgerError("Recording timestamps must not move backwards")
        event = {"sequence": len(events) + 1, "event_id": event_id, "kind": kind,
                 "recorded_at": recorded, "payload": json.loads(_canonical(payload)),
                 "previous_sha256": events[-1]["sha256"] if events else _ZERO_HASH}
        event["sha256"] = hashlib.sha256(_canonical(event).encode("utf-8")).hexdigest()
        prospective = events + [event]
        signals, fills, _ = _state(prospective)
        _validate_fills(signals, fills)
        values = (event["sequence"], event_id, kind, recorded, _canonical(payload),
                  event["previous_sha256"], event["sha256"])
        if pre_commit is not None:
            pre_commit()
        db.execute("INSERT INTO options_events VALUES (?, ?, ?, ?, ?, ?, ?)", values)
        if pre_commit is not None:
            pre_commit()
        return event

    def append_signal(self, signal_id: str, payload: Any, *, recorded_at: datetime | str,
                      pre_commit: Callable[[], None] | None = None,
                      recorded_at_clock: Callable[[], datetime] | None = None):
        """Append a frozen signal with optional current-publication checks.

        Guarded/clocked appends acquire an exclusive transaction before checking
        eligibility: rollback-journal readers wait here, not during COMMIT after
        a successful check. WAL readers do not block writer commits.
        ``recorded_at_clock`` samples an aware datetime after acquisition/read;
        ``recorded_at`` remains its minimum allowed time. This is recording time,
        not a claim about fsync completion or the signal's frozen data cutoff.
        ``pre_commit`` runs immediately before INSERT and again before COMMIT;
        raising rolls back the event. Callbacks must tolerate repeat calls and
        must not write the ledger or mutate the payload. Identical existing
        signals return without either callback because no insert occurs.
        """
        if self.read_only:
            raise LedgerError("Read-only ledger cannot append events")
        if pre_commit is not None and not callable(pre_commit):
            raise LedgerError("pre_commit must be callable")
        if recorded_at_clock is not None and not callable(recorded_at_clock):
            raise LedgerError("recorded_at_clock must be callable")
        signal_id = _identifier(signal_id, "signal_id")
        signal = json.loads(_canonical(payload))
        if not isinstance(signal, dict):
            raise LedgerError("Signal payload must be a mapping")
        if signal.get("signal_id", signal_id) != signal_id:
            raise LedgerError("Signal ID does not match payload")
        signal["signal_id"] = signal_id
        if signal.get("status") not in {"candidate", "no_trade"}:
            raise LedgerError("Explicit candidate or no_trade status is required")
        _plan(signal)
        if _stamp(signal.get("as_of")) > _stamp(recorded_at):
            raise LedgerError("Cannot record a future signal")
        frozen = hashlib.sha256(_canonical(signal).encode("utf-8")).hexdigest()
        wrapper = {"signal_id": signal_id, "signal": signal, "frozen_payload_sha256": frozen}
        with self._connect() as db:
            db.execute("BEGIN EXCLUSIVE" if pre_commit is not None or recorded_at_clock is not None
                       else "BEGIN IMMEDIATE")
            events = self._read(db)
            for event in events:
                if event["kind"] == "signal" and event["payload"]["signal_id"] == signal_id:
                    if event["payload"] == wrapper:
                        return event
                    raise DuplicateEvent("Frozen signal inputs cannot be replaced")
            if recorded_at_clock is not None:
                observed = recorded_at_clock()
                if not isinstance(observed, datetime) or observed.tzinfo is None or observed.utcoffset() is None:
                    raise LedgerError("recorded_at_clock must return a timezone-aware datetime")
                if _stamp(observed) < _stamp(recorded_at):
                    raise LedgerError("Recording clock must not precede the requested recording time")
                recorded_at = observed
            return self._append(db, events, "signal:" + signal_id, "signal", recorded_at, wrapper,
                                pre_commit=pre_commit)

    def record_fill(self, event_id: str, signal_id: str, contract_id: str, side: str,
                    quantity: int, price: Any, occurred_at: datetime | str,
                    recorded_at: datetime | str, external_fill_id: str | None = None, fees: Any = None):
        if self.read_only:
            raise LedgerError("Read-only ledger cannot append events")
        fill = _fill_payload(signal_id, contract_id, side, quantity, price, occurred_at, external_fill_id, fees)
        if fill["occurred_at"] > _stamp(recorded_at):
            raise LedgerError("A manual fill cannot occur after its recording timestamp")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            events = self._read(db)
            return self._append(db, events, event_id, "fill", recorded_at, fill)

    def correct_fill(self, correction_id: str, target_event_id: str,
                     replacement: Mapping[str, Any] | None, *, recorded_at: datetime | str, reason: str):
        if self.read_only:
            raise LedgerError("Read-only ledger cannot append events")
        reason = _identifier(reason, "correction reason")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            events = self._read(db)
            _, fills, latest = _state(events)
            if target_event_id not in fills:
                raise LedgerError("Corrections must identify an original manual fill event")
            original = next(item["payload"] for item in events if item["event_id"] == target_event_id)
            updated = None
            if replacement is not None:
                if not isinstance(replacement, Mapping):
                    raise LedgerError("Replacement must contain explicit manual fill fields")
                try:
                    updated = _fill_payload(**{key: replacement[key] for key in
                        ("signal_id", "contract_id", "side", "quantity", "price", "occurred_at")},
                        external_fill_id=replacement.get("external_fill_id"), fees=replacement.get("fees"))
                except KeyError as exc:
                    raise LedgerError("Replacement must contain all manual fill fields") from exc
                if updated["signal_id"] != original["signal_id"]:
                    raise LedgerError("Correction cannot move a fill to a different frozen signal")
                if updated["occurred_at"] > _stamp(recorded_at):
                    raise LedgerError("Corrected fill cannot occur in the future")
            return self._append(db, events, correction_id, "correction", recorded_at,
                {"target_event_id": target_event_id, "supersedes": latest[target_event_id],
                 "replacement": updated, "reason": reason})

    def events(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return self._read(db)

    def list_signals(self) -> list[dict[str, Any]]:
        return [event["payload"]["signal"] for event in self.events() if event["kind"] == "signal"]

    def exposure(self, signal_id: str) -> dict[str, Any]:
        events = self.events()
        signals, fills, _ = _state(events)
        if signal_id not in signals:
            raise LedgerError("Unknown signal")
        plan = _plan(signals[signal_id])
        positions = _validate_fills(signals, fills)[signal_id]
        longs = sum(max(value, 0) for value in positions.values())
        shorts = sum(max(-value, 0) for value in positions.values())
        any_fills = any(fill and fill["signal_id"] == signal_id for fill in fills.values())
        warnings = ["Manual entries are user reports, not broker-verified executions.",
                    "A planned stop is not a guaranteed loss cap; margin is separate from payoff."]
        matched, compatible = False, False
        if len(plan) == 2 and longs and shorts:
            contracts = [leg["contract"] for leg in plan.values()]
            compatible = all(contracts[0].get(key) is not None and contracts[0].get(key) == contracts[1].get(key)
                             for key in ("underlying", "exchange", "expiry_at", "option_type", "lot_size"))
            matched = compatible and longs == shorts and all(leg["ratio"] == 1 for leg in plan.values())
        matched_units = min(longs, shorts) if compatible else 0
        uncovered_units = shorts - matched_units
        if uncovered_units:
            status = "unhedged_short"
            warnings.append("UNHEDGED SHORT EXPOSURE: the planned spread loss bound does not apply.")
        elif matched:
            status = "hedged_open"
            warnings.append("Matched legs only: actual fill prices and fees must replace suggested prices in any payoff calculation.")
        elif longs:
            status = "long_only_open" if len(plan) == 1 else "partially_filled"
        else:
            status = "closed" if any_fills else "unfilled"
        requested = signals[signal_id].get("quantity")
        complete = bool(plan) and requested is not None and all(
            abs(positions[cid]) == requested * leg["ratio"] for cid, leg in plan.items())
        effective = [fill for fill in fills.values() if fill and fill["signal_id"] == signal_id]
        fees_known = bool(effective) and all(fill.get("fees") is not None for fill in effective)
        realized_gross, realized_net = None, None
        # Closed cash-flow arithmetic is distinct from a future expiry scenario.
        # Do not invent lot/cost allocation for any open or partly closed position.
        if status == "closed":
            gross = sum((Decimal(fill["price"]) * fill["quantity"] *
                         (1 if fill["side"] == "SELL" else -1) for fill in effective), Decimal(0))
            realized_gross = str(gross)
            if fees_known:
                realized_net = str(gross - sum((Decimal(fill["fees"]) for fill in effective), Decimal(0)))
        actual_loss, actual_profit = None, None
        # Exact expiry intrinsic payoff only. A close, partial execution or unknown
        # settlement metadata blocks this calculation rather than silently reusing
        # the candidate's entry prices. Additional future exit costs are unknown.
        no_closes = all(fill["side"] == plan[fill["contract_id"]]["side"] for fill in effective)
        if complete and no_closes and fees_known and (matched or len(plan) == 1):
            try:
                strikes = [Decimal(str(leg["contract"]["strike"])) for leg in plan.values()]
                rights = [leg["contract"]["option_type"] for leg in plan.values()]
                if any(not strike.is_finite() or strike <= 0 for strike in strikes) or any(right not in {"CALL", "PUT"} for right in rights):
                    raise ValueError
                premium = sum((Decimal(fill["price"]) * fill["quantity"] * (1 if fill["side"] == "BUY" else -1)
                               for fill in effective), Decimal(0))
                fee_total = sum((Decimal(fill["fees"]) for fill in effective), Decimal(0))
                payoffs = []
                for underlying in [Decimal(0), *strikes, max(strikes) * 2]:
                    intrinsic = Decimal(0)
                    for (cid, leg), strike, right in zip(plan.items(), strikes, rights):
                        value = max(Decimal(0), underlying - strike if right == "CALL" else strike - underlying)
                        intrinsic += value * positions[cid]
                    payoffs.append(intrinsic - premium - fee_total)
                actual_loss = str(max(Decimal(0), -min(payoffs)))
                actual_profit = None if len(plan) == 1 and rights[0] == "CALL" else str(max(payoffs))
                warnings.append("Expiry payoff includes recorded entry fees only; future exit/settlement costs remain additional.")
            except (KeyError, InvalidOperation, ValueError):
                warnings.append("Exact strike/right metadata is unavailable for an expiry payoff bound.")
        if not fees_known:
            warnings.append("Actual fees are unknown; a net expiry loss bound is unavailable.")
        return {"signal_id": signal_id, "positions": positions, "status": status,
                "signal_label": signals[signal_id].get("label", "unverified"),
                "execution_basis": "user_reported_manual_fills_not_broker_verified",
                "payoff_basis": "contractual_expiry_scenario_not_realized_pnl",
                "realized_pnl_basis": ("user_reported_closed_execution_not_broker_verified"
                                       if status == "closed" else "unavailable_until_fully_closed"),
                "realized_gross_pnl": realized_gross, "realized_net_pnl": realized_net,
                "complete_planned_position": complete,
                "contractual_bound_valid": actual_loss is not None,
                "fees_known": fees_known, "actual_max_loss_with_recorded_fees": actual_loss,
                "actual_max_profit_with_recorded_fees": actual_profit,
                "planned_price_bound_applies": False,
                "matched_units": matched_units,
                "unmatched_long_units": longs - matched_units,
                "unhedged_short_units": uncovered_units,
                "warnings": warnings}

    def export_csv(self, path: str | Path) -> int:
        destination = Path(path).resolve()
        if destination == self.path or (destination.exists() and destination.samefile(self.path)):
            raise LedgerError("Export cannot overwrite the ledger database")
        events = self.events()
        def safe(value):
            text = str(value)
            return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else text
        with destination.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("sequence", "event_id", "kind", "recorded_at", "payload_json", "previous_sha256", "sha256"))
            for event in events:
                writer.writerow([safe(event[key]) if key != "payload" else safe(_canonical(event[key]))
                                 for key in ("sequence", "event_id", "kind", "recorded_at", "payload", "previous_sha256", "sha256")])
        return len(events)


def validate_ledger(path: str | Path) -> dict[str, Any]:
    """Validate the exact ledger schema and all events without modifying a file.

    Intended for the actual SQLite snapshot entering a local backup. Bounds are
    64 MiB and 100,000 events; empty ledgers receive the same schema validation.
    Hashes establish internal consistency, not independent source authenticity.
    """
    target = Path(path)
    if (target.is_symlink() or getattr(target, "is_junction", lambda: False)()
            or not target.is_file() or target.stat().st_size > _LEDGER_VALIDATION_BYTES):
        raise LedgerError("Ledger validation requires a regular file at most 64 MiB")
    # Avoid __init__ reading rows before the bounded strict schema check.
    ledger = object.__new__(OptionsLedger)
    ledger.path, ledger.read_only = target.resolve(), True
    expected = {
        "options_events": ("table", """CREATE TABLE options_events (
            sequence INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL,
            previous_sha256 TEXT NOT NULL, sha256 TEXT NOT NULL)"""),
        "sqlite_autoindex_options_events_1": ("index", None),
    }
    for operation in ("UPDATE", "DELETE"):
        name = "options_events_no_" + operation.lower()
        expected[name] = ("trigger", f"CREATE TRIGGER {name} BEFORE {operation} ON options_events BEGIN SELECT RAISE(ABORT, 'append-only ledger'); END")
    def normalized(sql):
        return None if sql is None else "".join(sql.lower().split()).replace("ifnotexists", "")
    with ledger._connect() as db:
        db.execute("BEGIN")
        objects = db.execute("SELECT name,type,sql FROM sqlite_master").fetchall()
        if {row[0] for row in objects} != set(expected):
            raise IntegrityError("Ledger contains unsupported database schema: missing or unrelated schema objects")
        for name, kind, sql in objects:
            wanted_kind, wanted_sql = expected[name]
            if kind != wanted_kind or normalized(sql) != normalized(wanted_sql):
                raise IntegrityError("Ledger schema does not match the fixed application schema")
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        if pages * page_size > _LEDGER_VALIDATION_BYTES:
            raise LedgerError("Ledger validation exceeds the 64 MiB size limit")
        if db.execute("SELECT COUNT(*) FROM options_events").fetchone()[0] > _LEDGER_VALIDATION_EVENTS:
            raise LedgerError("Ledger validation exceeds the 100000 event limit")
        events = ledger._read(db)
        return {"valid": True, "schema_version": 1, "events": len(events),
                "last_sha256": events[-1]["sha256"] if events else None}


def read_ledger(path: str | Path) -> dict[str, Any]:
    """Validated local snapshot with zero schema changes or database writes.

    A missing file returns an explicit unavailable snapshot without creating any
    directory. Corruption raises IntegrityError; a consumer must not suppress it
    into an empty/healthy ledger. Callers control what local evidence they show.
    """
    target = Path(path)
    if not target.is_file():
        return {"available": False, "events": [], "signals": [], "exposures": {}, "last_sha256": None}
    ledger = OptionsLedger(target, read_only=True)
    events = ledger.events()
    # A consistent captured event set is used for all derived exposures.
    ledger.events = lambda: events
    signals = ledger.list_signals()
    return {"available": True, "events": events, "signals": signals,
            "exposures": {signal["signal_id"]: ledger.exposure(signal["signal_id"]) for signal in signals},
            "last_sha256": events[-1]["sha256"] if events else None}

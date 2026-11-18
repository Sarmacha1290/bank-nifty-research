"""Local connection window: the access token is never saved or printed."""
import argparse
from contextlib import ExitStack
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import queue
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk
import webbrowser

from banknifty.groww import (DataAccessError, GrowwDataClient, IST,
                            fetch_instrument_catalog_text, fetch_banknifty_instrument,
                            resolve_banknifty, resolve_nifty50)
from banknifty.expiry_context import record_expiry_reference
from banknifty.nifty50_probe import probe_nifty50_feed
from banknifty.rest_sampler import sample_rest_prices
from banknifty.rest_status import new_rest_status, save_rest_status, rest_status_note, validate_rest_status
from banknifty.constituents import resolve_bank_equities
from banknifty.derivative_catalog import nearest_banknifty_option_expiry
from banknifty.agents.options_futures import analyze_option_chain
from banknifty.option_history import save_option_chain_analysis
from banknifty.option_capture import capture_option_chains
from banknifty.capture_trial_history import (build_capture_trial_summary,
                                             save_capture_trial_summary)
from banknifty.capture_backup import backup_after_capture
from banknifty.feed_quality import MIN_SAMPLES_PER_ACCEPTED_MINUTE
from banknifty.futures import nearest_banknifty_future, parse_future_quote
from banknifty.futures_history import save_future_snapshot
from banknifty.futures_capture import capture_future_quotes
from banknifty.collect_activity import collect_bank_activity
from banknifty.parallel_capture import run_parallel_captures
from banknifty.nse_weights import load_reference_weights
from banknifty.history_audit import audit_session
from banknifty.market_calendar import regular_market_status
from banknifty.paper_pipeline import attempt_boundary_assessment
from banknifty.session_arm import PREPARE_LEAD, armable_open, wait_until
from banknifty.feed_quality_report import summarize_recorded_feed
from banknifty.trial_report import format_index_trial_note, summarize_index_trial
from banknifty.groww_feed_security import (GrowwFeedSecurityError,
                                          GrowwFeedSetupError, isolated_groww_feed)
from collect_groww import collect
from banknifty.storage import record_event


def capture_quality_note(coverage):
    """Describe full-day archive coverage without judging a short trial as a full day."""
    if coverage is None:
        return " No regular-session index minute was observed in the saved archive. "
    return (f" Saved session quality: {coverage['accepted_minutes']} of "
            f"{coverage['observed_minutes']} observed minutes accepted, "
            f"{coverage['rejected_minutes']} rejected. "
            f"{coverage['missing_whole_minutes']} of 375 full-day slots have no saved update; "
            "short trials leave other slots unrecorded. ")


def capture_completion_ok(result, quote_result, *, trial_report=None, include_banks=False,
                          include_depth=False, include_activity=False):
    """A capture passes only with a fully observed minute and reconciled archive."""
    if result["status"] not in ("completed", "session_ended") or result["inserted"] < 1:
        return False
    if trial_report is None or trial_report.get("full_accepted_minutes", 0) < 1:
        return False
    minimum = trial_report["full_accepted_minutes"] * MIN_SAMPLES_PER_ACCEPTED_MINUTE
    if (result["inserted"] < minimum or
            trial_report.get("saved_updates") != result["inserted"]):
        return False
    if include_banks and result["equity_status"] != "completed":
        return False
    if include_depth and result["depth_status"] != "completed":
        return False
    if include_activity:
        if quote_result is None or quote_result["status"] not in ("completed", "stopped"):
            return False
        if (not quote_result.get("saved") or quote_result.get("failures") != 0 or
                not (quote_result.get("summary") or {}).get("complete")):
            return False
    return True


def capture_reconciliation_note(result, trial_report):
    """Explain count discrepancies without treating missing diagnostics as zero."""
    if trial_report is None:
        return " Archive-count reconciliation unavailable. "
    saved = trial_report.get("saved_updates")
    if saved != result.get("inserted"):
        return (f" Recorder/archive mismatch: {result.get('inserted')} new index updates "
                f"versus {saved if saved is not None else 'unavailable'} unique updates "
                "received in this attempt. Review raw timestamps. ")
    return " Recorder/archive index counts agree. "


def save_safe_feed_status(data_dir, outcome, stage):
    """Persist only a fixed diagnostic label, never an SDK error or credential."""
    allowed_stages = {
        "instrument preparation", "Groww instrument catalogue",
        "Groww SDK client creation", "secure live-feed setup",
        "index recording", "capture review", "completed",
        "before_socket_credentials", "during_socket_credentials",
        "after_socket_credentials",
    }
    if outcome not in {"preparing", "failed", "completed"} or stage not in allowed_stages:
        raise ValueError("Invalid safe feed status")
    folder = Path(data_dir)
    status = {"outcome": outcome, "stage": stage,
              "checked_at_utc": datetime.now(timezone.utc).isoformat()}
    temporary = folder / "live-feed-status.tmp"
    temporary.write_text(json.dumps(status, indent=2), encoding="utf-8")
    temporary.replace(folder / "live-feed-status.json")


class ConnectionWindow:
    def _record_feed_stage(self, outcome, stage):
        try:
            save_safe_feed_status(self.data_dir, outcome, stage)
        except (OSError, ValueError):
            pass  # Diagnostic failure must not interrupt the collector.

    def __init__(self, root, data_dir):
        self.root, self.data_dir = root, Path(data_dir)
        self.messages = queue.Queue()
        self.busy = False
        self.stop_event = threading.Event()
        self._rest_worker = None
        self._closing = False
        self._close_callbacks = []
        root.title("Bank Nifty - Groww connection (REST fallback)")
        root.geometry("700x820")
        root.minsize(640, 600)
        shell = ttk.Frame(root)
        shell.pack(fill="both", expand=True)
        self.scroll_canvas = tk.Canvas(shell, highlightthickness=0)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=self.scroll_canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.scroll_canvas.configure(yscrollcommand=scrollbar.set)
        self.scroll_canvas.pack(side="left", fill="both", expand=True)
        frame = ttk.Frame(self.scroll_canvas, padding=18)
        self._frame_item = self.scroll_canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>", lambda event: self.scroll_canvas.configure(
            scrollregion=self.scroll_canvas.bbox("all")))
        self.scroll_canvas.bind("<Configure>", lambda event: self.scroll_canvas.itemconfigure(
            self._frame_item, width=event.width))
        def mousewheel(event):
            if event.delta:
                units = max(1, abs(event.delta) // 120)
                self.scroll_canvas.yview_scroll(-units if event.delta > 0 else units, "units")
            return "break"
        root.bind("<MouseWheel>", mousewheel, add="+")
        ttk.Label(frame, text="Connect your Groww data", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Phase 1  |  Forecast horizons: 15, 30 and 60 minutes",
                  font=("Segoe UI", 10)).pack(anchor="w", pady=(4, 10))
        ttk.Label(frame, text="Sign in to Groww and generate an Access Token.\nPaste it below to check Bank Nifty data access.",
                  wraplength=560).pack(anchor="w")
        ttk.Button(frame, text="Open Groww API keys", command=lambda: webbrowser.open(
            "https://groww.in/trade-api/api-keys")).pack(anchor="w", pady=10)
        ttk.Label(frame, text="Access Token").pack(anchor="w")
        self.token = ttk.Entry(frame, show="*", width=76)
        self.token.pack(fill="x", pady=(4, 6))
        self.button = ttk.Button(frame, text="Check data access", command=self.connect)
        self.button.pack(anchor="w")
        self.nifty_button = ttk.Button(frame, text="Check Nifty 50 feed (30 seconds, read-only)",
                                       command=self.check_nifty50)
        self.nifty_button.pack(anchor="w", pady=(6, 0))
        duration_row = ttk.Frame(frame)
        duration_row.pack(anchor="w", pady=(10, 6))
        ttk.Label(duration_row, text="Recording limit (minutes): ").pack(side="left")
        self.capture_minutes = ttk.Combobox(duration_row, values=("1", "5", "90", "390"), state="readonly", width=6)
        self.capture_minutes.set("5")
        self.capture_minutes.pack(side="left")
        self.rest_button = ttk.Button(frame, text="Record sampled Bank Nifty prices (REST, read-only)",
                                      command=self.capture_rest)
        self.rest_button.pack(anchor="w", pady=(6, 0))
        self.stop_button = ttk.Button(frame, text="Stop recording", command=self.stop_capture, state="disabled")
        self.stop_button.pack(anchor="w", pady=(6, 0))
        ttk.Label(frame, text="Fallback: one price request every five seconds. Saves laptop receipt times only; no exchange timestamps, official candles, forecasts or orders.",
                  wraplength=620).pack(anchor="w", pady=(2, 0))
        self.status = tk.StringVar(value=rest_status_note(self.data_dir) + " Waiting for your token. The input field clears after each action; no token is saved by this app.")
        ttk.Label(frame, textvariable=self.status, wraplength=620).pack(anchor="w", pady=(8, 0))
        ttk.Label(frame, text="Audit Groww's raw one-minute history for a known regular NSE trading day (YYYY-MM-DD).").pack(anchor="w", pady=(10, 4))
        self.audit_date = ttk.Entry(frame, width=18)
        self.audit_date.pack(anchor="w", pady=(0, 6))
        self.audit_button = ttk.Button(frame, text="Audit one trading day", command=self.audit_history)
        self.audit_button.pack(anchor="w")
        option_row = ttk.Frame(frame)
        option_row.pack(anchor="w", pady=(10, 0))
        self.option_button = ttk.Button(option_row, text="Check Bank Nifty option chain",
                                        command=self.check_option_chain)
        self.option_button.pack(side="left")
        self.option_record_button = ttk.Button(option_row, text="Record options for 30 minutes",
            command=self.record_option_chain)
        self.option_record_button.pack(side="left", padx=8)
        ttk.Label(frame, text="Read-only nearest listed expiry. Recording repeats every five minutes during regular hours. Groww's chain has no exchange timestamp; no live signal is established.",
                  wraplength=620).pack(anchor="w", pady=(4, 0))
        future_row = ttk.Frame(frame)
        future_row.pack(anchor="w", pady=(8, 0))
        self.future_button = ttk.Button(future_row, text="Check Bank Nifty future (read-only)",
                                        command=self.check_future)
        self.future_button.pack(side="left")
        self.future_record_button = ttk.Button(future_row, text="Record future for 30 minutes",
            command=self.record_future)
        self.future_record_button.pack(side="left", padx=8)
        ttk.Label(frame, text="Nearest contract only; repeated quotes are five minutes apart. Last-trade time may be stale; no forecast or order.",
                  wraplength=620).pack(anchor="w", pady=(3, 0))
        ttk.Label(frame, text="During regular NSE hours, record timestamped index updates. No trades or official candles are created.",
                  wraplength=620).pack(anchor="w", pady=(10, 6))
        ttk.Label(frame, text="Index feed recording uses the recording limit selected above.").pack(anchor="w", pady=(0, 6))
        self.include_banks = tk.BooleanVar(value=False)
        self.banks_option = ttk.Checkbutton(
            frame, text="Also record 14 bank equity prices (experimental)", variable=self.include_banks)
        self.banks_option.pack(anchor="w", pady=(0, 4))
        self.include_depth = tk.BooleanVar(value=False)
        self.depth_option = ttk.Checkbutton(
            frame, text="Also record 14-bank five-level order-book depth (experimental)",
            variable=self.include_depth)
        self.depth_option.pack(anchor="w", pady=(0, 4))
        self.include_activity = tk.BooleanVar(value=False)
        self.activity_option = ttk.Checkbutton(
            frame, text="Also record 14-bank volume and waiting orders (experimental)",
            variable=self.include_activity)
        self.activity_option.pack(anchor="w", pady=(0, 4))
        ttk.Label(frame, text="Uses the dated NSE list for symbols only. Saved prices may not align with index ticks; no contribution is calculated.",
                  wraplength=620).pack(anchor="w", pady=(0, 4))
        self.capture_button = ttk.Button(frame, text="Record live Bank Nifty data", command=self.capture_live)
        self.capture_button.pack(anchor="w")
        self.arm_button = ttk.Button(frame, text="Arm full-session recording (09:00–09:15 IST)",
                                     command=self.arm_full_session)
        self.arm_button.pack(anchor="w", pady=(6, 0))
        ttk.Label(frame, text="Enter today's token in the masked field after 09:00. Keep this window and laptop awake. "
                  "The token remains in memory only; recording begins at 09:15 if preparation succeeds.",
                  wraplength=620).pack(anchor="w", pady=(3, 0))
        self.activity_button = ttk.Button(frame, text="Record 14-bank volume & order book", command=self.capture_activity)
        self.activity_button.pack(anchor="w", pady=(6, 0))
        ttk.Label(frame, text="Read-only quotes every 15 seconds. Buy/sell quantities are waiting orders, not completed trades.",
                  wraplength=620).pack(anchor="w", pady=(3, 0))
        self._closed = False
        self._poll_id = root.after(150, self.poll)
        root.bind("<Destroy>", self._on_destroy, add="+")

    def connect(self):
        if self.busy:
            return
        token = self.token.get().strip()
        self.token.delete(0, "end")
        if not token:
            self.status.set("Paste the Access Token from Groww into this window.")
            return
        self.busy = True
        self.button.config(state="disabled")
        self.audit_button.config(state="disabled")
        self.capture_button.config(state="disabled")
        self.arm_button.config(state="disabled")
        self.banks_option.config(state="disabled")
        self.depth_option.config(state="disabled")
        self.activity_option.config(state="disabled")
        self.option_button.config(state="disabled")
        self.option_record_button.config(state="disabled")
        self.future_button.config(state="disabled")
        self.future_record_button.config(state="disabled")
        self.nifty_button.config(state="disabled")
        self.activity_button.config(state="disabled")
        self.status.set("Checking the current instrument catalogue and your data access...")
        threading.Thread(target=self.check_access, args=(token,), daemon=True).start()

    def audit_history(self):
        if self.busy:
            return
        try:
            session_date = date.fromisoformat(self.audit_date.get().strip())
            if session_date > datetime.now(IST).date():
                raise ValueError("future date")
        except ValueError:
            self.status.set("Enter a past regular trading date in YYYY-MM-DD format.")
            return
        token = self.token.get().strip()
        self.token.delete(0, "end")
        if not token:
            self.status.set("Paste today's Groww Access Token to audit history.")
            return
        self.busy = True
        self.button.config(state="disabled")
        self.audit_button.config(state="disabled")
        self.capture_button.config(state="disabled")
        self.arm_button.config(state="disabled")
        self.banks_option.config(state="disabled")
        self.depth_option.config(state="disabled")
        self.activity_option.config(state="disabled")
        self.option_button.config(state="disabled")
        self.option_record_button.config(state="disabled")
        self.future_button.config(state="disabled")
        self.future_record_button.config(state="disabled")
        self.nifty_button.config(state="disabled")
        self.activity_button.config(state="disabled")
        self.status.set("Checking raw candle coverage. No market prices will be archived yet...")
        threading.Thread(target=self.check_history, args=(token, session_date), daemon=True).start()

    def capture_live(self):
        if self.busy:
            return
        market_status = regular_market_status(datetime.now(timezone.utc))
        if market_status != "regular_session":
            self.status.set(f"NSE regular capture unavailable ({market_status}). The reviewed 2026 calendar and 09:15-15:30 IST window apply.")
            return
        token = self.token.get().strip()
        self.token.delete(0, "end")
        if not token:
            self.status.set("Paste today's Groww Access Token to record the live feed.")
            return
        self._begin_feed_capture(token, int(self.capture_minutes.get()))

    def capture_rest(self):
        if self.busy:
            return
        market_status = regular_market_status(datetime.now(timezone.utc))
        if market_status != "regular_session":
            self.status.set(f"REST sampling requires regular NSE hours ({market_status}).")
            return
        token = self.token.get().strip()
        self.token.delete(0, "end")
        if not token:
            self.status.set("Paste today's Groww Access Token before REST sampling.")
            return
        minutes = int(self.capture_minutes.get())
        self.stop_event.clear()
        self.busy = True
        self.rest_button.config(state="disabled")
        self.capture_minutes.config(state="disabled")
        self.stop_button.config(state="normal")
        self.status.set(f"Sampling Bank Nifty REST prices for up to {minutes} minutes; receipt-time observations only...")
        self._rest_worker = threading.Thread(target=self.read_rest, args=(token, minutes), daemon=False)
        self._rest_worker.start()

    def read_rest(self, token, minutes):
        client = None
        attempt = new_rest_status(minutes)
        status_saved = False

        def persist():
            nonlocal status_saved
            attempt["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            try:
                save_rest_status(self.data_dir, attempt)
                status_saved = True
            except Exception:
                status_saved = False  # Never expose filesystem or provider exception text.

        def progress(count, value, observed):
            attempt["saved"] = count  # Called only after the observation commit.
            persist()
            self.messages.put(("progress", f"REST samples saved: {count}; last observed price "
                f"{value:,.2f} at laptop receipt {observed}. Exchange freshness unverified."))

        persist()  # Replace an older success before starting any preparation.
        try:
            if self.stop_event.is_set():
                attempt["outcome"] = "stopped"
            else:
                instrument = fetch_banknifty_instrument()
                if self.stop_event.is_set():
                    attempt["outcome"] = "stopped"
                else:
                    attempt["stage"] = "client creation"
                    persist()
                    client = GrowwDataClient(token)
                    token = ""
                    attempt["stage"] = "sampling"
                    persist()
                    result = sample_rest_prices(client, instrument,
                        self.data_dir / "market-data.sqlite3", minutes=minutes,
                        stop_event=self.stop_event, on_sample=progress)
                    candidate = {**attempt, "outcome": result["status"], "saved": result["saved"]}
                    validate_rest_status(candidate)
                    if candidate["outcome"] not in {"completed", "stopped", "market_closed", "access_error", "archive_error"}:
                        raise ValueError("Invalid sampler outcome")
                    attempt = candidate
                    if attempt["outcome"] in {"completed", "stopped", "market_closed"}:
                        attempt["stage"] = "finished"
        except Exception:
            attempt["outcome"] = ("preparation_failed" if attempt["stage"] in
                {"instrument catalogue", "client creation"} else "unexpected_error")
        finally:
            token = ""
            if client is not None:
                try:
                    client.close()
                except Exception:
                    attempt["cleanup_ok"] = False
            # A failed diagnostic write cannot relabel a completed price capture.
            attempt["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            try:
                record_event(self.data_dir / "market-data.sqlite3", "rest_sampling_trial",
                    {**attempt, "history_saved": True, "status": attempt["outcome"],
                     "source": "Groww", "symbol": "NSE-BANKNIFTY"})
                attempt["history_saved"] = True
            except Exception:
                attempt["history_saved"] = False
            persist()
            diagnostics = (" Attempt history and latest diagnostic saved." if status_saved and attempt["history_saved"]
                else " Attempt history unavailable; latest diagnostic saved." if status_saved
                else " Latest diagnostic unavailable; attempt history saved." if attempt["history_saved"]
                else " Attempt diagnostics could not be saved; older saved status may be stale.")
            cleanup = "" if attempt["cleanup_ok"] else " Client cleanup failed; close this window before another attempt."
            ok = (attempt["outcome"] in {"completed", "stopped"} and attempt["saved"] > 0
                  and attempt["cleanup_ok"] and status_saved and attempt["history_saved"])
            self.messages.put((ok, f"REST sampling {attempt['outcome'].replace('_', ' ')} "
                f"at {attempt['stage']}: {attempt['saved']} receipt-time observations confirmed saved."
                + diagnostics + cleanup + " Exchange freshness is unverified; forecast readiness is unchanged."))

    def arm_full_session(self):
        if self.busy:
            return
        try:
            opening = armable_open(datetime.now(timezone.utc))
        except ValueError as exc:
            self.status.set(str(exc))
            return
        token = self.token.get().strip()
        self.token.delete(0, "end")
        if not token:
            self.status.set("Paste today's Groww Access Token to arm the full session.")
            return
        self._begin_feed_capture(token, 390, scheduled_open=opening)

    def _begin_feed_capture(self, token, minutes, *, scheduled_open=None):
        include_banks = self.include_banks.get()
        include_depth = self.include_depth.get()
        include_activity = self.include_activity.get()
        self.stop_event.clear()
        self.busy = True
        for button in (self.button, self.audit_button, self.capture_button, self.arm_button,
                       self.banks_option, self.depth_option, self.activity_option,
                       self.option_button, self.option_record_button, self.activity_button):
            button.config(state="disabled")
        self.future_button.config(state="disabled")
        self.future_record_button.config(state="disabled")
        self.nifty_button.config(state="disabled")
        self.capture_minutes.config(state="disabled")
        self.stop_button.config(state="normal")
        if scheduled_open is None:
            self.status.set(f"Recording timestamped Bank Nifty{' and bank' if include_banks else ''}"
                            f"{' and depth' if include_depth else ''} updates"
                            f"{' plus bank quotes' if include_activity else ''} for up to {minutes} minutes...")
        else:
            self.status.set(f"Armed for {scheduled_open:%H:%M} IST. Keep the app and laptop awake; "
                            "today's token is held in memory only until recording starts.")
        threading.Thread(target=self.check_feed,
                         args=(token, minutes, include_banks, include_depth, include_activity),
                         kwargs={"scheduled_open": scheduled_open}, daemon=True).start()

    def check_option_chain(self):
        if self.busy:
            return
        token = self.token.get().strip()
        self.token.delete(0, "end")
        if not token:
            self.status.set("Paste today's Groww Access Token to check the option chain.")
            return
        self.busy = True
        for button in (self.button, self.audit_button, self.capture_button, self.arm_button,
                       self.banks_option, self.depth_option, self.activity_option,
                       self.option_button, self.option_record_button, self.activity_button):
            button.config(state="disabled")
        self.future_button.config(state="disabled")
        self.future_record_button.config(state="disabled")
        self.nifty_button.config(state="disabled")
        self.status.set("Reading the nearest listed Bank Nifty option expiry. No order will be placed...")
        threading.Thread(target=self.read_option_chain, args=(token,), daemon=True).start()

    def record_option_chain(self):
        if self.busy:
            return
        market_status = regular_market_status(datetime.now(timezone.utc))
        if market_status != "regular_session":
            self.status.set(f"Option recording requires regular NSE hours ({market_status}).")
            return
        token = self.token.get().strip()
        self.token.delete(0, "end")
        if not token:
            self.status.set("Paste today's Groww Access Token to record option chains.")
            return
        self.stop_event.clear()
        self.busy = True
        for button in (self.button, self.audit_button, self.capture_button, self.arm_button,
                       self.banks_option, self.depth_option, self.activity_option,
                       self.option_button, self.option_record_button, self.future_button,
                       self.future_record_button,
                       self.activity_button, self.nifty_button):
            button.config(state="disabled")
        self.capture_minutes.config(state="disabled")
        self.stop_button.config(state="normal")
        self.status.set("Recording nearest-expiry option chains every five minutes for up to 30 minutes. No orders will be placed...")
        threading.Thread(target=self.run_option_recording, args=(token,), daemon=True).start()

    def run_option_recording(self, token):
        client = None
        try:
            client = GrowwDataClient(token)
            token = ""
            catalog = fetch_instrument_catalog_text()
            expiry = nearest_banknifty_option_expiry(catalog, datetime.now(IST).date())
            result = capture_option_chains(client, expiry,
                self.data_dir / "market-data.sqlite3", duration_seconds=1800,
                interval_seconds=300,
                stop_requested=lambda: (self.stop_event.is_set() or
                    regular_market_status(datetime.now(timezone.utc)) != "regular_session"),
                on_progress=lambda progress: self.messages.put(("progress",
                    f"Option chains: {progress['saved']} saved for {expiry}; latest "
                    f"{progress['complete_pairs']}/{progress['strikes']} paired strikes. "
                    "Exchange freshness remains unverified.")))
            self.messages.put((result["status"] == "completed" and result["saved"] >= 2,
                f"Option recording {result['status']}: {result['saved']} saved of "
                f"{result['attempts']} request(s) for {expiry}. "
                + (f"Issue: {result['last_error']}. " if result['last_error'] else "")
                + "Saved snapshots are receipt-time only, not buy/sell flow or a forecast."))
        except (DataAccessError, ValueError) as exc:
            self.messages.put((False, f"Option recording stopped before capture: {exc}"))
        except Exception:
            self.messages.put((False, "Option recording stopped unexpectedly. Any saved snapshots remain in the archive; no orders were placed."))
        finally:
            token = ""
            if client is not None:
                client.close()

    def capture_activity(self):
        if self.busy:
            return
        market_status = regular_market_status(datetime.now(timezone.utc))
        if market_status != "regular_session":
            self.status.set(f"Bank activity recording requires regular NSE hours ({market_status}).")
            return
        token = self.token.get().strip()
        self.token.delete(0, "end")
        if not token:
            self.status.set("Paste today's Groww Access Token to record bank activity.")
            return
        minutes = int(self.capture_minutes.get())
        self.stop_event.clear()
        self.busy = True
        for button in (self.button, self.audit_button, self.capture_button, self.arm_button,
                       self.banks_option, self.depth_option, self.activity_option,
                       self.option_button, self.option_record_button, self.future_button,
                       self.future_record_button, self.activity_button):
            button.config(state="disabled")
        self.capture_minutes.config(state="disabled")
        self.nifty_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.status.set("Recording receipt-time quotes for 14 banks; no trades will be placed...")
        threading.Thread(target=self.check_activity, args=(token, minutes), daemon=True).start()

    def check_activity(self, token, minutes):
        client = None
        try:
            client = GrowwDataClient(token)
            token = ""
            report = load_reference_weights(Path(__file__).resolve().parent /
                                            "reference" / "nse-bank-weights-2026-09-30.json")
            catalog = fetch_instrument_catalog_text()
            equities = resolve_bank_equities(catalog, [row["symbol"] for row in report["constituents"]])
            result = collect_bank_activity(client, equities, self.data_dir / "market-data.sqlite3",
                duration_seconds=minutes * 60,
                stop_requested=lambda: (self.stop_event.is_set() or
                    regular_market_status(datetime.now(timezone.utc)) != "regular_session"),
                on_progress=lambda progress: self.messages.put(("progress",
                    f"Bank quotes: {progress['sweeps']} sweeps, {progress['saved']} saved rows, "
                    f"{progress['summary']['coverage']}/14 in latest sweep, "
                    f"receipt span {progress['summary']['receipt_span_seconds']:.1f}s, "
                    f"{len(progress['alerts'])} exploratory alerts. Buy/sell are outstanding orders.")))
            self.messages.put((result["status"] == "completed",
                f"Bank activity {result['status']}: {result['sweeps']} sweeps, {result['saved']} saved quotes, "
                f"{result['failures']} failed reads. Latest sweep: "
                f"{result['summary']['coverage'] if result['summary'] else 0}/14 banks; "
                + (f"receipt span {result['summary']['receipt_span_seconds']:.1f}s; "
                   if result['summary'] else "")
                + f"{len(result['alerts'])} exploratory alerts. "
                + (f"Last issue: {result['last_error']} " if result['last_error'] else "")
                + "No executed buy/sell split or forecast."))
        except (DataAccessError, ValueError) as exc:
            self.messages.put((False, f"Bank activity capture stopped: {exc}"))
        except Exception:
            self.messages.put((False, "Bank activity capture stopped. Saved partial data remains; no orders were placed."))
        finally:
            token = ""
            if client is not None:
                client.close()

    def read_option_chain(self, token):
        client = None
        try:
            client = GrowwDataClient(token)
            token = ""
            catalog = fetch_instrument_catalog_text()
            expiry = nearest_banknifty_option_expiry(catalog, datetime.now(IST).date())
            response = client.banknifty_option_chain(expiry)
            analysis = analyze_option_chain(response, expiry, datetime.now(timezone.utc))
            save_option_chain_analysis(self.data_dir / "market-data.sqlite3", analysis)
            oi_ratio = f"{analysis.oi_pcr:.2f}" if analysis.oi_pcr is not None else "unavailable (partial chain)"
            self.messages.put((True, f"Bank Nifty {expiry} option-chain snapshot saved: "
                f"{analysis.complete_pairs}/{analysis.strikes} paired strikes; OI put/call ratio {oi_ratio}. "
                "Receipt time only; exchange freshness, predictive value and trading signals remain unverified."))
        except (DataAccessError, ValueError) as exc:
            self.messages.put((False, f"Option-chain check stopped: {exc}"))
        except Exception:
            self.messages.put((False, "Option-chain check could not finish. No forecast or trade was created."))
        finally:
            token = ""
            if client is not None:
                client.close()

    def record_future(self):
        if self.busy:
            return
        market_status = regular_market_status(datetime.now(timezone.utc))
        if market_status != "regular_session":
            self.status.set(f"Futures recording requires regular NSE hours ({market_status}).")
            return
        token = self.token.get().strip()
        self.token.delete(0, "end")
        if not token:
            self.status.set("Paste today's Groww Access Token to record futures quotes.")
            return
        self.stop_event.clear()
        self.busy = True
        for button in (self.button, self.audit_button, self.capture_button, self.arm_button,
                       self.banks_option, self.depth_option, self.activity_option,
                       self.option_button, self.option_record_button, self.future_button,
                       self.future_record_button, self.activity_button, self.nifty_button):
            button.config(state="disabled")
        self.capture_minutes.config(state="disabled")
        self.stop_button.config(state="normal")
        self.status.set("Recording nearest Bank Nifty future every five minutes for up to 30 minutes. No orders will be placed...")
        threading.Thread(target=self.run_future_recording, args=(token,), daemon=True).start()

    def run_future_recording(self, token):
        client = None
        try:
            client = GrowwDataClient(token)
            token = ""
            instrument = nearest_banknifty_future(fetch_instrument_catalog_text(),
                                                   datetime.now(IST).date())
            result = capture_future_quotes(client, instrument,
                self.data_dir / "market-data.sqlite3", duration_seconds=1800,
                interval_seconds=300,
                stop_requested=lambda: (self.stop_event.is_set() or
                    regular_market_status(datetime.now(timezone.utc)) != "regular_session"),
                on_progress=lambda progress: self.messages.put(("progress",
                    f"Futures quotes: {progress['saved']} saved for {instrument.trading_symbol}; "
                    + (f"last trade age {progress['last_trade_age_seconds']:.1f}s. "
                       if progress['last_trade_age_seconds'] is not None else
                       "last trade time unavailable. ")
                    + "A recent laptop receipt does not establish a recent trade.")))
            self.messages.put((result["status"] == "completed" and result["saved"] >= 2,
                f"Futures recording {result['status']}: {result['saved']} saved of "
                f"{result['attempts']} request(s) for {instrument.trading_symbol}. "
                + (f"Issue: {result['last_error']}. " if result['last_error'] else "")
                + "Trade times remain separate from receipt times; no forecast or order was created."))
        except (DataAccessError, ValueError) as exc:
            self.messages.put((False, f"Futures recording stopped before capture: {exc}"))
        except Exception:
            self.messages.put((False, "Futures recording stopped unexpectedly. Any saved quotes remain archived; no orders were placed."))
        finally:
            token = ""
            if client is not None:
                client.close()

    def check_future(self):
        if self.busy:
            return
        market_status = regular_market_status(datetime.now(timezone.utc))
        if market_status != "regular_session":
            self.status.set(f"Futures check requires regular NSE hours ({market_status}).")
            return
        token = self.token.get().strip()
        self.token.delete(0, "end")
        if not token:
            self.status.set("Paste today's Groww Access Token to check a Bank Nifty future.")
            return
        self.busy = True
        for button in (self.button, self.audit_button, self.capture_button, self.arm_button,
                       self.banks_option, self.depth_option, self.activity_option,
                       self.option_button, self.option_record_button, self.future_button,
                       self.future_record_button, self.activity_button):
            button.config(state="disabled")
        self.nifty_button.config(state="disabled")
        self.status.set("Reading one nearest-expiry Bank Nifty futures quote. No order will be placed...")
        threading.Thread(target=self.read_future, args=(token,), daemon=True).start()

    def read_future(self, token):
        client = None
        try:
            client = GrowwDataClient(token)
            token = ""
            instrument = nearest_banknifty_future(fetch_instrument_catalog_text(),
                                                   datetime.now(IST).date())
            response = client.banknifty_future_quote(instrument.trading_symbol)
            snapshot = parse_future_quote(response, instrument, datetime.now(timezone.utc))
            save_future_snapshot(self.data_dir / "market-data.sqlite3", snapshot)
            trade_time = (snapshot.last_trade_at_utc.astimezone(IST).strftime("%H:%M:%S IST")
                          if snapshot.last_trade_at_utc else "unavailable")
            self.messages.put((True, f"Bank Nifty {instrument.expiry_date} future saved: "
                f"last price ₹{snapshot.last_price:,.2f}, volume {snapshot.volume}, "
                f"open interest {snapshot.open_interest}; last trade {trade_time}. "
                "This is a single quote; freshness and predictive value are unverified."))
        except (DataAccessError, ValueError) as exc:
            self.messages.put((False, f"Futures check stopped: {exc}"))
        except Exception:
            self.messages.put((False, "Futures check could not finish. No forecast or trade was created."))
        finally:
            token = ""
            if client is not None:
                client.close()

    def stop_capture(self):
        self.stop_event.set()
        self.stop_button.config(state="disabled")
        self.status.set("Cancelling preparation or stopping after the current market-data request. Saved records remain on disk.")

    def _on_destroy(self, event):
        if event.widget is self.root:
            self.shutdown()

    def shutdown(self):
        """Stop capture and cancel UI polling when the window closes."""
        self.stop_event.set()
        self._closed = True
        pending = getattr(self, "_poll_id", None)
        self._poll_id = None
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass

    def request_close(self, on_closed=None):
        """Keep Tk alive while a REST worker finishes its bounded request/review."""
        if on_closed is not None and on_closed not in self._close_callbacks:
            self._close_callbacks.append(on_closed)
        if self._closing:
            return
        self._closing = True
        self.shutdown()
        self.token.delete(0, "end")
        self.busy = True  # No new action while the close is pending.
        self.stop_button.config(state="disabled")
        self.status.set("Stopping after the current request and saving the attempt review. "
                        "This window will close when the REST recorder has finished.")

        def finish():
            if self._rest_worker is not None and self._rest_worker.is_alive():
                self.root.after(100, finish)
                return
            self.root.destroy()
            callbacks, self._close_callbacks = self._close_callbacks, []
            for callback in callbacks:
                callback()

        finish()

    def check_feed(self, token, minutes, include_banks=False, include_depth=False,
                   include_activity=False,
                   *, scheduled_open=None):
        activity_client = None
        feed_session = ExitStack()
        attempt_started_at = datetime.now(timezone.utc)
        trial_started_at = None
        review_saved = False
        stage = "instrument preparation"
        self._record_feed_stage("preparing", stage)
        try:
            if scheduled_open is not None:
                if not wait_until(scheduled_open - PREPARE_LEAD, self.stop_event):
                    self._save_failed_capture(attempt_started_at, "preparation_cancelled",
                        include_banks, include_depth, include_activity)
                    self.messages.put((False, "Armed recording cancelled before preparation. No feed was opened."))
                    return
                self.messages.put(("progress", "Preparing Groww feed and instrument list before 09:15 IST..."))
            from growwapi import GrowwAPI
            stage = "Groww instrument catalogue"
            catalog = fetch_instrument_catalog_text()
            instrument = resolve_banknifty(catalog)
            if include_banks or include_depth or include_activity:
                report = load_reference_weights(Path(__file__).resolve().parent /
                                                "reference" / "nse-bank-weights-2026-09-30.json")
                equities = resolve_bank_equities(
                    catalog, [row["symbol"] for row in report["constituents"]])
            else:
                equities = ()
            try:
                record_expiry_reference(self.data_dir / "market-data.sqlite3",
                                        catalog, datetime.now(timezone.utc))
            except (DataAccessError, OSError, sqlite3.Error, ValueError):
                # Index collection is useful even when derivative metadata is absent.
                pass
            if include_activity:
                activity_client = GrowwDataClient(token)
            stage = "Groww SDK client creation"
            api = GrowwAPI(token)
            stage = "secure live-feed setup"
            feed = feed_session.enter_context(isolated_groww_feed(api))
            token = ""
            if scheduled_open is not None:
                self.messages.put(("progress", "Groww feed prepared. Waiting for 09:15 IST; keep laptop awake."))
                if not wait_until(scheduled_open, self.stop_event):
                    self._save_failed_capture(attempt_started_at, "preparation_cancelled",
                        include_banks, include_depth, include_activity)
                    self.messages.put((False, "Armed recording cancelled before market open. No updates were saved."))
                    return
                if datetime.now(IST) > scheduled_open + timedelta(seconds=10):
                    self.messages.put(("progress", "Feed preparation finished after opening; first-minute quality may be incomplete."))
            database = self.data_dir / "market-data.sqlite3"
            index_finished_at = None
            def index_job():
                nonlocal index_finished_at
                try:
                    return collect(feed, instrument, database,
                                 duration_seconds=minutes * 60, stop_requested=self.stop_event.is_set,
                                 equity_instruments=equities if include_banks else (),
                                 depth_instruments=equities if include_depth else (),
                                 on_progress=lambda progress: self.messages.put(("progress",
                                     f"Recording: {progress['inserted']} index updates, "
                                     f"{progress['equity_inserted']} bank prices, "
                                     f"{progress['depth_inserted']} five-level books saved; "
                                     f"{progress['depth_rejected']} depth rows rejected. "
                                     "The main screens can be refreshed during capture.")),
                                 on_completed_minute=lambda boundary, generated_at:
                                 attempt_boundary_assessment(database, boundary, generated_at))
                finally:
                    # Quote shutdown can finish later; it must not lengthen the
                    # index feed's sampled-minute coverage window.
                    index_finished_at = datetime.now(timezone.utc)
            def activity_job(cancelled):
                try:
                    return collect_bank_activity(activity_client, equities, database,
                        duration_seconds=minutes * 60,
                        stop_requested=lambda: (cancelled() or self.stop_event.is_set() or
                            regular_market_status(datetime.now(timezone.utc)) != "regular_session"),
                        on_progress=lambda progress: self.messages.put(("progress",
                            f"Bank quote sweep: {progress['sweeps']} rounds, "
                            f"{progress['saved']} saved rows; latest {progress['summary']['coverage']}/14 banks. "
                            "Buy/sell quantities are waiting orders.")))
                finally:
                    activity_client.close()
            trial_started_at = datetime.now(timezone.utc)
            stage = "index recording"
            result, quote_result = run_parallel_captures(
                index_job, activity_job if include_activity else None,
                on_slow_shutdown=lambda: self.messages.put(("progress",
                    "Index recording ended; waiting for the current bank quote request to finish.")))
            trial_finished_at = datetime.now(timezone.utc)
            stage = "capture review"
            try:
                trial_report = summarize_index_trial(database, trial_started_at,
                                                     index_finished_at or trial_finished_at)
                trial_note = format_index_trial_note(trial_report)
            except (OSError, sqlite3.Error, ValueError):
                trial_report = None
                trial_note = " Current-trial diagnostics unavailable; saved updates remain intact. "
            bank_result = (f" Banks: {result['equity_status']}, "
                           f"{result['equity_inserted']} new, {result['equity_duplicates']} repeats, "
                           f"{result['equity_rejected']} rejected; "
                           f"{result.get('equity_archive_busy_retries', 0)} temporary archive-lock retries; "
                           f"{result['equity_symbols_with_updates']}/{result['equity_expected_symbols']} banks "
                           "had new updates. " if include_banks else " ")
            depth_result = (f" Depth: {result['depth_status']}, "
                            f"{result['depth_inserted']} new, {result['depth_rejected']} rejected; "
                            f"{result.get('depth_archive_busy_retries', 0)} temporary archive-lock retries; "
                            f"{result['depth_symbols_with_updates']}/{result['depth_expected_symbols']} banks "
                            f"had timestamped books; {result['depth_alerts']} trial depth alerts, "
                            f"{result['depth_alert_errors']} alert errors. " if include_depth else " ")
            quote_note = (f" Quote sweeps: {quote_result['status']}, "
                          f"{quote_result['sweeps'] if quote_result['sweeps'] is not None else 'unknown'} sweeps, "
                          f"{quote_result['saved'] if quote_result['saved'] is not None else 'unknown'} bank rows, "
                          f"{quote_result['failures'] if quote_result['failures'] is not None else 'unknown'} failed reads. "
                          if quote_result else " ")
            session_date = scheduled_open.date() if scheduled_open is not None else datetime.now(IST).date()
            try:
                coverage = next((row for row in summarize_recorded_feed(database)
                                 if row["date_ist"] == session_date.isoformat()), None)
            except (OSError, sqlite3.Error, ValueError):
                coverage_note = " Minute-quality review unavailable; saved updates remain intact. "
            else:
                coverage_note = capture_quality_note(coverage)
            complete = capture_completion_ok(result, quote_result,
                trial_report=trial_report,
                include_banks=include_banks, include_depth=include_depth,
                include_activity=include_activity)
            try:
                saved_summary = build_capture_trial_summary(
                    trial_started_at, trial_finished_at, result, trial_report, quote_result,
                    include_banks=include_banks, include_depth=include_depth,
                    include_activity=include_activity, accepted=complete,
                    index_finished_at=index_finished_at)
                save_capture_trial_summary(database, saved_summary)
                review_saved = True
                review_note = " Capture review saved locally. "
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
                complete = False
                review_note = " Capture review could not be saved; inspect raw data before another trial. "
            backup_note = self._backup_recording(trial_started_at)
            self._record_feed_stage("completed", "completed")
            self.messages.put((complete,
                f"{'Capture saved; minimal local gate passed, review full coverage' if complete else 'Capture needs review'} "
                f"(index {result['status']}): {result['inserted']} new updates, "
                f"{result['duplicates']} repeats, {result['rejected']} rejected, "
                f"{result.get('archive_busy_retries', 0)} temporary archive-lock retries. "
                + bank_result + depth_result + quote_note +
                f"{result['paper_logged']} experimental paper scores fixed, {result['paper_errors']} paper errors. "
                + trial_note + capture_reconciliation_note(result, trial_report) +
                coverage_note + review_note + backup_note +
                "Saved updates remain available; no forecast candles created."))
        except ImportError:
            self._record_feed_stage("failed", stage)
            if not review_saved:
                self._save_failed_capture(attempt_started_at, "preparation_failed",
                    include_banks, include_depth, include_activity)
            self.messages.put((False, "Groww's Python SDK is not installed. Install growwapi before live capture."))
        except GrowwFeedSecurityError as exc:
            self._record_feed_stage("failed", stage)
            if not review_saved:
                self._save_failed_capture(attempt_started_at, "preparation_failed",
                    include_banks, include_depth, include_activity)
            self.messages.put((False, f"Live capture blocked by local SDK credential safeguard: {exc}"))
        except GrowwFeedSetupError as exc:
            self._record_feed_stage("failed", exc.stage)
            if not review_saved:
                self._save_failed_capture(attempt_started_at, "preparation_failed",
                    include_banks, include_depth, include_activity)
            self.messages.put((False, f"Groww live-feed setup stopped {str(exc)}. "
                "This identifies a setup stage only; no orders were placed."))
        except DataAccessError as exc:
            self._record_feed_stage("failed", stage)
            if not review_saved:
                self._save_failed_capture(attempt_started_at,
                    "capture_exception" if trial_started_at is not None else "preparation_failed",
                    include_banks, include_depth, include_activity)
            backup_note = self._backup_recording(trial_started_at) if trial_started_at is not None else ""
            self.messages.put((False, f"Live capture stopped during {stage}: {exc}" + backup_note))
        except Exception:
            self._record_feed_stage("failed", stage)
            if not review_saved:
                self._save_failed_capture(attempt_started_at,
                    "capture_exception" if trial_started_at is not None else "preparation_failed",
                    include_banks, include_depth, include_activity)
            backup_note = self._backup_recording(trial_started_at) if trial_started_at is not None else ""
            self.messages.put((False, f"Live capture stopped during {stage}. Check Groww access and the connection; no orders were placed." + backup_note))
        finally:
            token = ""
            try:
                feed_session.close()
            except (OSError, GrowwFeedSecurityError):
                self.messages.put((False, "Groww socket shutdown or credential cleanup failed. Close this app before the next capture and review local security status."))
            if activity_client is not None:
                activity_client.close()

    def _save_failed_capture(self, started_at, status, include_banks, include_depth,
                             include_activity):
        """Best-effort failure audit; never store a token or exception text."""
        try:
            summary = build_capture_trial_summary(
                started_at, datetime.now(timezone.utc), {"status": status}, None, None,
                include_banks=include_banks, include_depth=include_depth,
                include_activity=include_activity, accepted=False)
            save_capture_trial_summary(self.data_dir / "market-data.sqlite3", summary)
            return True
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            return False

    def _backup_recording(self, started_at):
        """Protect saved raw rows after a collector starts, even if its review fails."""
        database = self.data_dir / "market-data.sqlite3"
        if started_at is None or not database.is_file():
            return " No database was saved for an automatic copy. "
        try:
            backup_after_capture(database, started_at, self.data_dir / "capture-backups")
        except (OSError, sqlite3.Error, ValueError):
            return " Automatic local database copy failed; use Saved history to back up the market database. "
        return " Verified local database copy saved on this laptop. "

    def check_history(self, token, session_date):
        client = None
        try:
            client = GrowwDataClient(token)
            token = ""
            instrument = fetch_banknifty_instrument()
            opening = datetime.combine(session_date, time(9, 15), IST)
            closing = datetime.combine(session_date, time(15, 30), IST)
            response = client.historical_candles(instrument, opening, closing)
            report = audit_session(response, session_date)
            report["checked_at"] = datetime.now(timezone.utc).isoformat()
            report["symbol"] = instrument["groww_symbol"]
            record_event(self.data_dir / "market-data.sqlite3", "history_audit", report)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.data_dir / "history-audit.tmp"
            temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
            temporary.replace(self.data_dir / "history-audit-latest.json")
            self.messages.put((True, f"{session_date}: {report['returned_rows']} rows; "
                f"first {report['first_timestamp'] or 'none'}, last {report['last_timestamp'] or 'none'}. "
                f"Result: {report['status'].replace('_', ' ')}. Saved audit only; no prices archived or forecasts enabled."))
        except (DataAccessError, ValueError) as exc:
            self.messages.put((False, str(exc)))
        except Exception:
            self.messages.put((False, "History audit could not finish. No market prices were archived."))
        finally:
            token = ""
            if client is not None:
                client.close()

    def check_access(self, token):
        client = None
        stage = "token preparation"
        succeeded = False
        response_received = False
        message = "Connection check could not finish."

        def persist_status(status):
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                temporary = self.data_dir / "connection-status.tmp"
                temporary.write_text(json.dumps(status, indent=2), encoding="utf-8")
                temporary.replace(self.data_dir / "connection-status.json")
                return True
            except Exception:
                return False

        try:
            client = GrowwDataClient(token)
            token = ""
            stage = "instrument catalogue"
            instrument = fetch_banknifty_instrument()
            stage = "Groww price response"
            snapshot = client.index_snapshot(instrument)
            response_received = True
            stage = "local audit save"
            event_id = record_event(self.data_dir / "market-data.sqlite3", "connection_snapshot", snapshot)
            status = {"connected": True, "checked_at": datetime.now(timezone.utc).isoformat(),
                      "symbol": snapshot["symbol"], "audit_event": event_id,
                      "forecast_models_ready": False, "horizons_minutes": [15, 30, 60],
                      "freshness": "unverified; latest price has no exchange timestamp"}
            stage = "local status save"
            succeeded = persist_status(status)
            message = ("Groww returned a Bank Nifty price response. The token field was cleared. "
                       "This check does not establish data freshness.")
            if not succeeded:
                message += " Local status could not be saved; older saved status may be stale."
        except Exception:
            status = {"connected": False, "checked_at": datetime.now(timezone.utc).isoformat(),
                      "reason": f"Connection check failed during {stage}.", "forecast_models_ready": False,
                      "price_response_received": response_received,
                      "horizons_minutes": [15, 30, 60]}
            saved = persist_status(status)
            message = f"Connection check could not finish during {stage}. "
            message += ("A price response arrived, but its local review could not finish. " if response_received
                        else "No successful price response was established. Check Groww access and the connection. ")
            if not saved:
                message += "Local status could not be saved; older saved status may be stale."
        finally:
            token = ""
            if client is not None:
                try:
                    client.close()
                except Exception:
                    succeeded = False
                    message += " Client cleanup failed; close this window before another attempt."
            self.messages.put((succeeded, message))

    def check_nifty50(self):
        """Start an isolated fallback feed check without touching Bank Nifty data."""
        if self.busy:
            return
        market_status = regular_market_status(datetime.now(timezone.utc))
        if market_status != "regular_session":
            self.status.set(f"Nifty 50 feed check requires regular NSE hours ({market_status}).")
            return
        token = self.token.get().strip()
        self.token.delete(0, "end")
        if not token:
            self.status.set("Paste today's Groww Access Token to check the Nifty 50 feed.")
            return
        self.stop_event.clear()
        self.busy = True
        for button in (self.button, self.audit_button, self.capture_button, self.arm_button,
                       self.banks_option, self.depth_option, self.activity_option,
                       self.option_button, self.option_record_button, self.future_button,
                       self.future_record_button, self.activity_button,
                       self.nifty_button):
            button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.status.set("Checking Nifty 50 index updates for 30 seconds; nothing will be saved or traded...")
        threading.Thread(target=self.read_nifty50_probe, args=(token,), daemon=True).start()

    def read_nifty50_probe(self, token):
        feed_session = ExitStack()
        try:
            from growwapi import GrowwAPI
            instrument = resolve_nifty50(fetch_instrument_catalog_text())
            feed = feed_session.enter_context(isolated_groww_feed(GrowwAPI(token)))
            token = ""
            result = probe_nifty50_feed(feed, instrument,
                stop_requested=self.stop_event.is_set)
            success = result["status"] == "completed" and result["unique"] > 0
            self.messages.put((success,
                f"Nifty 50 feed check {result['status']}: {result['unique']} distinct "
                f"timestamped updates, {result['repeats']} repeats, {result['rejected']} rejected. "
                "No data was saved; this only checks access and does not validate forecast quality."))
        except ImportError:
            self.messages.put((False, "Groww's Python SDK is not installed. Nifty 50 feed was not checked."))
        except GrowwFeedSecurityError:
            self.messages.put((False, "Nifty 50 feed blocked by the local SDK credential safeguard."))
        except GrowwFeedSetupError as exc:
            self.messages.put((False, f"Nifty 50 feed setup stopped {str(exc)}. "
                "This identifies a setup stage only; no data or orders were saved."))
        except DataAccessError as exc:
            self.messages.put((False, f"Nifty 50 feed check stopped: {exc}"))
        except Exception:
            self.messages.put((False, "Nifty 50 feed check could not finish. No data or orders were saved."))
        finally:
            token = ""
            try:
                feed_session.close()
            except (OSError, GrowwFeedSecurityError):
                self.messages.put((False, "Groww socket cleanup failed. Close this app before another feed check."))

    def poll(self):
        if getattr(self, "_closed", False):
            return
        pending = getattr(self, "_poll_id", None)
        if pending is not None:
            self.root.after_cancel(pending)
        self._poll_id = None
        try:
            succeeded, message = self.messages.get_nowait()
        except queue.Empty:
            pass
        else:
            if succeeded == "progress":
                self.status.set(message)
                self._poll_id = self.root.after(150, self.poll)
                return
            self.busy = False
            self.button.config(state="normal")
            self.audit_button.config(state="normal")
            self.capture_button.config(state="normal")
            self.arm_button.config(state="normal")
            self.banks_option.config(state="normal")
            self.depth_option.config(state="normal")
            self.activity_option.config(state="normal")
            self.option_button.config(state="normal")
            self.option_record_button.config(state="normal")
            self.future_button.config(state="normal")
            self.future_record_button.config(state="normal")
            self.nifty_button.config(state="normal")
            self.rest_button.config(state="normal")
            self.activity_button.config(state="normal")
            self.capture_minutes.config(state="readonly")
            self.stop_button.config(state="disabled")
            self.status.set(message)
        self._poll_id = self.root.after(150, self.poll)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "data")
    args = parser.parse_args()
    root = tk.Tk()
    window = ConnectionWindow(root, args.data_dir)
    root.protocol("WM_DELETE_WINDOW", window.request_close)
    root.mainloop()


if __name__ == "__main__":
    main()

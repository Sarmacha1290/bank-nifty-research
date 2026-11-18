"""Local Phase 1 desktop application. No AI calls or automatic network traffic."""
import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser
from zipfile import BadZipFile

from banknifty.options_workspace import create_options_panel
from banknifty import __version__
from banknifty.groww import IST
from banknifty.history import export_csv, list_datasets, load_candles, save_candles
from banknifty.agents.luna import assess
from banknifty.agents.bull_bear import build_bull_case, build_bear_case
from banknifty.agents.master import review_bundle
from banknifty.agents.index_contribution import (PricePair, Weight, WeightSnapshot,
                                                 calculate_contributions)
from banknifty.agents.options_futures import analyze_option_chain, describe_option_structure
from banknifty.option_history import count_option_chain_snapshots, export_option_chain_csv
from banknifty.option_snapshot_review import (review_latest_saved_option_pair,
                                              export_latest_option_changes_csv)
from banknifty.futures_history import load_future_snapshots, export_future_snapshots_csv
from banknifty.futures_review import review_latest_saved_future_pair
from banknifty.capture_trial_history import (load_capture_trial_summaries,
                                             format_capture_trial_summary,
                                             export_capture_trial_csv)
from banknifty.market_activity import summarize_bank_quotes
from banknifty.market_activity_history import (count_bank_quotes, export_activity_alerts_csv,
    export_bank_quotes_csv, load_activity_alerts, load_bank_quote_sweeps)
from banknifty.depth_history import count_depth_ticks, export_depth_ticks_csv, latest_depth_by_symbol
from banknifty.depth_alignment import align_latest_depth
from banknifty.daily_range import (build_daily_range_history, export_daily_range_csv,
                                   compare_official_and_sampled, export_range_comparison_csv)
from banknifty.daily_reference import (evaluate_daily_reference,
                                       summarize_daily_reference,
                                       export_daily_reference_csv)
from banknifty.daily_paper import (record_next_reference, load_scored_references,
                                   export_daily_paper_csv)
from banknifty.official_daily import (load_official_days, sync_recent_official_days,
                                      sync_older_official_days, export_official_days_csv)
from banknifty.session_diagnostics import (build_session_diagnostics,
                                           export_session_diagnostics_csv)
from banknifty.equity_history import (count_equity_ticks, equity_symbol_counts,
                                      equity_coverage, export_equity_ticks_csv)
from banknifty.alignment import audit_latest_recorded_session
from banknifty.bank_move_review import (review_latest_recorded_bank_moves,
                                        export_latest_bank_moves_csv)
from banknifty.nse_weights import load_reference_weights
from banknifty.technical_features import build_technical_snapshot
from banknifty.indicator_library import calculate_price_indicators
from banknifty.candle_patterns import analyze_candle_patterns
from banknifty.candle_charts import completed_chart_bars
from banknifty.expiry_context import expiry_features, load_expiry_references
from banknifty.tick_history import count_index_ticks, export_index_ticks_csv, load_index_ticks
from banknifty.rest_status import rest_status_note
from banknifty.rest_sampler import (summarize_rest_observations,
                                    audit_rest_receipt_integrity,
                                    summarize_rest_session_coverage,
                                    export_rest_observations_csv)
from banknifty.feed_quality import inspect_sampled_minutes, missing_minutes_between_observations
from banknifty.feed_quality_report import export_feed_quality_csv, summarize_recorded_feed
from banknifty.feed_technical import build_feed_evidence
from banknifty.agents.verification import verify_bundle
from banknifty.agents.risk import check_risk
from banknifty.market_calendar import regular_session
from banknifty.evaluation import match_outcomes
from banknifty.backup import backup_database
from banknifty.project_backup import backup_project, rehearse_project_restore
from banknifty.project_map import generate_project_map
from banknifty.backtest import evaluate_recorded_database, export_recorded_comparison_csv
from banknifty.readiness import check_local_readiness
from banknifty.training_readiness import assess_training_readiness
from banknifty.training_dataset import (build_training_research_rows,
                                        export_training_research_csv)
from banknifty.expiry_regime_review import review_expiry_regimes
from banknifty.event_context import load_known_events, stage_event_csv
from banknifty.paper_log import (count_paper_assessments, paper_outcome_summary,
                                 export_paper_report_csv, record_paper_assessments,
                                 resolve_paper_outcomes)
from banknifty.workspace import AGENTS, DEMO_DATASET, read_connection_status, read_history_audit, synthetic_session
from banknifty.institutional_flows import latest_staged
from connect_groww import ConnectionWindow


BG, PANEL, INK, MUTED, ACCENT = "#f1f4f8", "#ffffff", "#16283b", "#65778a", "#087e83"


class Dashboard:
    def __init__(self, root, data_dir, *, offline=False):
        self.root, self.data_dir = root, Path(data_dir)
        self._closing = False
        self.offline = offline
        self.database = self.data_dir / "market-data.sqlite3"
        self.candles = []
        self.connection_window = None
        self.connection_controller = None
        root.title(f"Bank Nifty {__version__} | Offline signals research" if offline else f"Bank Nifty {__version__} | Research workspace")
        root.geometry(f"{min(1160, root.winfo_screenwidth() - 60)}x{min(780, root.winfo_screenheight() - 100)}")
        root.minsize(1000, 660)
        root.configure(bg=BG)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=INK, font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 8))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(20, 10), font=("Segoe UI", 10))
        style.configure("TNotebook.Tab", background="#e0e7ef", foreground=MUTED)
        style.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", ACCENT)])
        style.configure("TButton", background=PANEL, foreground=INK)
        style.map("TButton", background=[("active", "#e0f1f0")])
        style.configure("Treeview", rowheight=29, font=("Segoe UI", 10), background=PANEL, fieldbackground=PANEL)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"), padding=7)
        style.map("Treeview", background=[("selected", ACCENT)])

        header = tk.Frame(root, bg=INK, padx=28, pady=16)
        header.pack(fill="x")
        tk.Label(header, text="BANK NIFTY", bg=INK, fg="#75ded6", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(header, text="Research workspace", bg=INK, fg=PANEL, font=("Segoe UI", 24, "bold")).pack(anchor="w")
        tk.Label(header, text="Offline options research  /  Saved market history     •     15 min · 30 min · 60 min · full-day range", bg=INK, fg="#c5d2df", font=("Segoe UI", 10)).pack(anchor="w", pady=(5, 0))
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True, padx=22, pady=(16, 8))
        self.overview = ttk.Frame(self.tabs, padding=12)
        self.agents = ttk.Frame(self.tabs, padding=12)
        self.history = ttk.Frame(self.tabs, padding=12)
        self.activity_page = ttk.Frame(self.tabs, padding=12)
        self.depth_page = ttk.Frame(self.tabs, padding=12)
        self.range_page = ttk.Frame(self.tabs, padding=12)
        self.setup = ttk.Frame(self.tabs, padding=12)
        self.options_page = create_options_panel(self.tabs, storage_dir=self.data_dir / "options-research")
        for page, title in ((self.overview, "Overview"), (self.agents, "Agent workbench"),
                            (self.options_page, "Options Research"),
                            (self.history, "Saved history"), (self.activity_page, "Bank activity"),
                            (self.depth_page, "Order depth"), (self.range_page, "Daily range"),
                            (self.setup, "Setup & next steps")):
            self.tabs.add(page, text=title)
        self.notice = tk.StringVar(value="Offline signals research. No live verification or calibrated forecast.")
        tk.Label(root, textvariable=self.notice, bg=BG, fg=MUTED, anchor="w", padx=28, pady=10, font=("Segoe UI", 9), wraplength=1020).pack(side="bottom", fill="x", before=self.tabs)
        self.build_overview()
        self.build_agents()
        self.build_history()
        self.build_activity()
        self.build_depth()
        self.build_range()
        self.build_setup()
        self.refresh()

    def text(self, parent, message, *, size=10, color=INK, bold=False, wrap=950):
        background = BG if isinstance(parent, ttk.Frame) else parent.cget("background")
        label = tk.Label(parent, text=message, bg=background, fg=color,
                         font=("Segoe UI", size, "bold" if bold else "normal"), justify="left", anchor="w", wraplength=wrap)
        label.pack(fill="x", pady=(0, 8))
        return label

    def build_overview(self):
        self.text(self.overview, "Your data comes first", size=19, bold=True)
        self.text(self.overview, "Explore saved history and follow each specialist as it is built. Forecasts stay unavailable until the data and models are validated.", color=MUTED)
        cards = tk.Frame(self.overview, bg=BG)
        cards.pack(fill="x", pady=(4, 12))
        self.summary_labels = {}
        for column, title in enumerate(("GROWW ACCESS", "SAVED CANDLES", "AGENT READINESS")):
            cards.columnconfigure(column, weight=1)
            panel = tk.Frame(cards, bg=PANEL, padx=16, pady=14)
            panel.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
            self.text(panel, title, size=9, color=MUTED, bold=True)
            self.summary_labels[title] = self.text(panel, "—", size=15, bold=True)
        toolbar = ttk.Frame(self.overview)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="Load sample history", command=self.load_demo).pack(side="left")
        ttk.Button(toolbar, text="Check Groww access", command=self.connect).pack(side="left", padx=8)
        ttk.Button(toolbar, text="Refresh status", command=self.refresh).pack(side="left")
        self.chart_mode = tk.StringVar(value="Candles")
        ttk.Combobox(toolbar, textvariable=self.chart_mode, values=("Candles", "Close line"),
                     state="readonly", width=12).pack(side="right")
        self.chart_interval = tk.StringVar(value="1 minute")
        ttk.Combobox(toolbar, textvariable=self.chart_interval,
                     values=("1 minute", "5 minutes", "15 minutes", "30 minutes", "60 minutes"),
                     state="readonly", width=12).pack(side="right", padx=8)
        self.chart_mode.trace_add("write", lambda *_: self.draw_chart())
        self.chart_interval.trace_add("write", lambda *_: self.draw_chart())
        self.chart_caption = self.text(self.overview, "No dataset selected", color=MUTED)
        self.pattern_caption = self.text(self.overview, "Patterns: unavailable until continuous candles are loaded", color=MUTED)
        self.chart = tk.Canvas(self.overview, height=210, bg=PANEL, highlightthickness=0)
        self.chart.pack(fill="both", expand=True)
        self.chart.bind("<Configure>", lambda event: self.draw_chart())
        horizons = tk.Frame(self.overview, bg=BG)
        horizons.pack(side="bottom", fill="x", pady=(12, 0), before=self.chart)
        for column, minutes in enumerate((15, 30, 60)):
            horizons.columnconfigure(column, weight=1)
            panel = tk.Frame(horizons, bg=PANEL, padx=16, pady=12)
            panel.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
            self.text(panel, f"NEXT {minutes} MINUTES", color=ACCENT, size=10, bold=True)
            self.text(panel, "Forecast unavailable", bold=True)
            self.text(panel, "Model validation pending", color=MUTED, size=9)
        horizons.columnconfigure(3, weight=1)
        panel = tk.Frame(horizons, bg=PANEL, padx=16, pady=12)
        panel.grid(row=0, column=3, sticky="nsew", padx=(8, 0))
        self.text(panel, "FULL-DAY RANGE", color=ACCENT, size=10, bold=True)
        self.text(panel, "Forecast unavailable", bold=True)
        self.text(panel, "Complete-day labels pending", color=MUTED, size=9)

    def build_agents(self):
        self.text(self.agents, "14 roles. One shared evidence snapshot.", size=19, bold=True)
        self.text(self.agents, "Select an agent to see its purpose and the work still needed. These are software roles; no AI model is running here.", color=MUTED)
        first_actions = ttk.Frame(self.agents)
        first_actions.pack(anchor="w", pady=(0, 5))
        ttk.Button(first_actions, text="Run Technical Agent on invented sample", command=self.run_technical_sample).pack(side="left")
        ttk.Button(first_actions, text="Check Technical Agent against recorded feed", command=self.run_technical_feed).pack(side="left", padx=8)
        ttk.Button(first_actions, text="Run Options Agent on invented chain", command=self.run_options_sample).pack(side="left")
        second_actions = ttk.Frame(self.agents)
        second_actions.pack(anchor="w", pady=(0, 8))
        ttk.Button(second_actions, text="Check later outcomes on invented sample", command=self.run_outcome_sample).pack(side="left")
        ttk.Button(second_actions, text="Run Verification and Risk on invented sample", command=self.run_guard_sample).pack(side="left", padx=8)
        ttk.Button(second_actions, text="Run Index Contribution on invented banks", command=self.run_index_sample).pack(side="left")
        ttk.Button(second_actions, text="View dated NSE bank weights", command=self.show_weight_reference).pack(side="left", padx=8)
        third_actions = ttk.Frame(self.agents)
        third_actions.pack(anchor="w", pady=(0, 8))
        ttk.Button(third_actions, text="Run Bull on invented sample", command=self.run_bull_sample).pack(side="left")
        ttk.Button(third_actions, text="Run Bear on invented sample", command=self.run_bear_sample).pack(side="left", padx=8)
        ttk.Button(third_actions, text="Check Bull, Bear & Master against recorded feed", command=self.run_cases_feed).pack(side="left")
        ttk.Button(third_actions, text="Run Master on invented sample", command=self.run_master_sample).pack(side="left", padx=8)
        ttk.Button(third_actions, text="Check training data", command=self.show_training_readiness).pack(side="left")
        ttk.Button(third_actions, text="View Institutional Flows", command=self.show_institutional_flows).pack(side="left", padx=8)
        indicator_actions = ttk.Frame(self.agents)
        indicator_actions.pack(anchor="w", pady=(0, 8))
        ttk.Button(indicator_actions, text="View indicators on invented sample",
                   command=self.show_indicator_sample).pack(side="left")
        ttk.Button(indicator_actions, text="View indicators from saved index feed",
                   command=self.show_recorded_indicators).pack(side="left", padx=8)
        work_area = ttk.Frame(self.agents)
        work_area.pack(fill="both", expand=True, pady=(0, 8))
        right_area = ttk.Frame(work_area)
        right_area.pack(side="right", fill="both", expand=True)
        self.agent_detail = self.text(right_area, "Select a row for details.", color=MUTED, wrap=760)
        self.agent_detail.pack_configure(side="bottom")
        self.results_tabs = ttk.Notebook(right_area)
        self.results_tabs.pack(side="top", fill="both", expand=True)
        def result_tab(title, message):
            page = ttk.Frame(self.results_tabs, padding=8)
            self.results_tabs.add(page, text=title)
            return page, self.text(page, message, color=MUTED, wrap=740)
        self.technical_tab, self.technical_sample_info = result_tab(
            "Technical", "No sample assessment run. This role is not yet validated on real Groww candles.")
        self.bull_tab, self.bull_info = result_tab("Bull", "Bull has not examined a feature snapshot.")
        self.bear_tab, self.bear_info = result_tab("Bear", "Bear has not examined a feature snapshot.")
        self.outcome_tab, self.outcome_sample_info = result_tab(
            "Outcomes", "No offline outcome check run. Invented outcomes cannot establish accuracy.")
        self.guard_tab, self.guard_info = result_tab(
            "Verification & Risk", "Verification and Risk have not been demonstrated yet.")
        self.index_tab, self.index_info = result_tab(
            "Index", "A dated 30 Sep NSE weight reference is saved. Real contributions need verified aligned prices and point-in-time weights.")
        self.options_tab, self.options_info = result_tab(
            "Options", "Options chain arithmetic has not been demonstrated. No exchange timestamp or real options evidence is available.")
        self.flows_tab, self.flows_info = result_tab(
            "Institutional Flows", "No manually imported report is saved. This is delayed market-wide cash context, not a live Bank Nifty forecast.")
        self.master_tab, self.master_info = result_tab(
            "Master", "Master has not reviewed a shared evidence bundle. No forecast model is available.")
        columns = ("agent", "state")
        table_frame = ttk.Frame(work_area)
        table_frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.agent_table = ttk.Treeview(table_frame, columns=columns, show="headings", height=12, selectmode="browse")
        for key, title, width in (("agent", "Agent", 190), ("state", "Build status", 105)):
            self.agent_table.heading(key, text=title)
            self.agent_table.column(key, width=width, minwidth=100, stretch=key == "purpose")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.agent_table.yview)
        self.agent_table.configure(yscrollcommand=scroll.set)
        self.agent_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for index, (name, state, purpose, _) in enumerate(AGENTS):
            self.agent_table.insert("", "end", iid=str(index), values=(name, state))
        self.agent_table.bind("<<TreeviewSelect>>", self.select_agent)
        self.agent_table.selection_set("0")

    def select_agent(self, event=None):
        selection = self.agent_table.selection()
        if selection:
            agent = AGENTS[int(selection[0])]
            self.agent_detail.config(text=f"{agent[0]}: {agent[3]}")

    def show_institutional_flows(self):
        try:
            reports = latest_staged(self.database, decision_at=datetime.now(timezone.utc))
        except (OSError, sqlite3.Error, ValueError):
            self.flows_info.config(text="Saved institutional-flow records could not be verified.")
            return
        if not reports:
            self.flows_info.config(text="No manually imported report is saved. This is delayed market-wide cash context, not a live Bank Nifty forecast.")
            return
        lines = ["USER-SUPPLIED REPORTS · SOURCE AUTHENTICITY UNVERIFIED", ""]
        for report in reports:
            label = ("NSE-only cash market" if report["scope"] == "nse_cash"
                     else "NSE/BSE/MSEI combined cash market")
            lines.append(f"{label} · report date {report['report_date']}")
            for row in report["rows"]:
                lines.append(f"  {row['category']}: buy ₹{row['buy_crore']:,.2f} cr; "
                             f"sell ₹{row['sell_crore']:,.2f} cr; "
                             f"net ₹{row['net_crore']:,.2f} cr")
            lines.append(f"  First locally observed: {report['rows'][0]['first_observed_utc']} UTC")
            lines.append("")
        lines.append("Published release time unknown. These totals are market-wide, delayed and excluded from Master, forecasts and trading.")
        self.flows_info.config(text="\n".join(lines))
        self.results_tabs.select(self.flows_tab)

    def show_training_readiness(self):
        try:
            report = assess_training_readiness(self.database)
            message = (f"Official NSE daily rows: {report['nse_daily_rows']}. "
                       f"Groww index updates: {report['groww_index_ticks']}. "
                       f"Complete sampled sessions: {report['complete_sampled_sessions']}. "
                       f"Trained forecast models: {report['trained_forecast_models']}. "
                       + report["reason"])
        except (OSError, ValueError, sqlite3.Error) as exc:
            message = "Training-data inventory could not be verified. No model is approved."
        self.results_tabs.select(self.master_tab)
        self.master_info.config(text=message)

    def run_technical_sample(self):
        self.results_tabs.select(self.technical_tab)
        session, candles = synthetic_session()
        as_of = session.open + timedelta(minutes=75)
        observed = [candle for candle in candles if candle.end <= as_of]
        try:
            snapshot = build_technical_snapshot(observed, as_of, generated_at=as_of)
            results = [assess(snapshot, minutes) for minutes in (15, 30, 60)]
        except (ValueError, TypeError):
            self.technical_sample_info.config(text="Sample calculation failed; no assessment is available.")
            return
        rows = [f"{item.horizon_minutes} min: {item.bias} rules score {item.score:+.1f} / 100" for item in results]
        self.technical_sample_info.config(text="INVENTED SAMPLE · offline rule interpretation · confidence unavailable\n" + "   |   ".join(rows))
        self.notice.set("Technical Agent ran on invented candles. These scores are not tested forecasts or trading signals.")

    def _show_indicator_window(self, report, label):
        window = tk.Toplevel(self.root)
        window.title("Bank Nifty indicator calculations")
        window.geometry("780x650")
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=label, wraplength=730).pack(anchor="w", pady=(0, 8))
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        viewer = tk.Text(body, wrap="none", font=("Consolas", 10), background="#ffffff")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=viewer.yview)
        viewer.configure(yscrollcommand=scrollbar.set)
        viewer.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        lines = [f"Information cutoff: {report['as_of']}",
                 f"Source: {report['source']}",
                 f"Formula: {report['formula_version']}",
                 f"Continuous completed minutes: {report['input_minutes']}", ""]
        for key, value in report["features"].items():
            shown = "unavailable from these inputs" if value is None else (
                f"{value:,.6f}" if isinstance(value, float) else str(value))
            lines.append(f"{key.replace('_', ' '):42} {shown}")
        lines.extend(("", "Descriptive measurements only. No trained forecast or alpha claim."))
        viewer.insert("1.0", "\n".join(lines))
        viewer.configure(state="disabled")
        return window

    def show_indicator_sample(self):
        session, candles = synthetic_session()
        as_of = session.open + timedelta(minutes=75)
        observed = [row for row in candles if row.end <= as_of]
        report = calculate_price_indicators(observed, as_of, session_open=session.open)
        self._show_indicator_window(report, "INVENTED DATA · Formula demonstration only")

    def show_recorded_indicators(self):
        try:
            ticks = load_index_ticks(self.database)
            timely = []
            for tick in ticks:
                minute_end = tick.exchange_at.astimezone(IST).replace(second=0, microsecond=0) + timedelta(minutes=1)
                if tick.exchange_at <= tick.received_at <= minute_end + timedelta(seconds=5):
                    timely.append(tick)
            _, accepted = inspect_sampled_minutes(timely)
            if len(accepted) < 61:
                raise ValueError("61 continuous accepted sampled minutes are required")
            observed = accepted[-61:]
            report = calculate_price_indicators(observed, observed[-1].end)
            report["features"].update(expiry_features(observed[-1].end,
                load_expiry_references(self.database)))
        except (OSError, sqlite3.Error, ValueError) as exc:
            messagebox.showinfo("Saved index indicators", f"No complete indicator report is available: {exc}", parent=self.root)
            return
        self._show_indicator_window(report,
            "SAVED GROWW FEED · Historical sampled candles, not official exchange OHLC or a live forecast")

    @staticmethod
    def _case_summary(case):
        if case.status != "descriptive":
            return f"{case.horizon_minutes}m: unavailable ({'; '.join(case.cautions)})"
        support = "; ".join(case.supporting_observations) or "none"
        opposing = "; ".join(case.opposing_observations) or "none"
        return f"{case.horizon_minutes}m: supporting [{support}] · opposing [{opposing}]"

    def _show_case(self, snapshot, role, label):
        make_case = build_bull_case if role == "Bull" else build_bear_case
        cases = [make_case(snapshot, minutes) for minutes in (15, 30, 60)]
        label.config(text=f"{role.upper()} · {snapshot.source.upper()} · past-only observations, no forecast\n" +
                     "\n".join(self._case_summary(case) for case in cases))
        self.results_tabs.select(self.bull_tab if role == "Bull" else self.bear_tab)

    def _run_case_sample(self, role):
        session, candles = synthetic_session()
        as_of = session.open + timedelta(minutes=75)
        try:
            snapshot = build_technical_snapshot(
                [candle for candle in candles if candle.end <= as_of], as_of,
                generated_at=as_of)
            self._show_case(snapshot, role, self.bull_info if role == "Bull" else self.bear_info)
        except (ValueError, TypeError):
            (self.bull_info if role == "Bull" else self.bear_info).config(
                text=f"{role} could not examine the invented sample.")
            return
        self.notice.set(f"{role} examined invented history only. It did not produce a prediction or trading signal.")

    def run_bull_sample(self):
        self._run_case_sample("Bull")

    def run_bear_sample(self):
        self._run_case_sample("Bear")

    def run_cases_feed(self):
        self.results_tabs.select(self.bull_tab)
        try:
            _, candles, snapshot, technical = build_feed_evidence(load_index_ticks(self.database))
            self._show_case(snapshot, "Bull", self.bull_info)
            self._show_case(snapshot, "Bear", self.bear_info)
            bull = [build_bull_case(snapshot, minutes) for minutes in (15, 30, 60)]
            bear = [build_bear_case(snapshot, minutes) for minutes in (15, 30, 60)]
            session = regular_session(snapshot.as_of.astimezone(IST).date())
            verification = verify_bundle(candles, snapshot, technical, session)
            risk = check_risk(snapshot, technical, verification, session)
            result = review_bundle(snapshot, technical, bull, bear, verification, risk)
            self._show_master_result(result, "RECORDED FEED")
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            message = f"RECORDED FEED · no specialist review: {exc}"
            self.bull_info.config(text=message)
            self.bear_info.config(text=message)
            self.master_info.config(text=message)
            return
        self.notice.set("Bull, Bear and Master examined one recorded-feed cutoff. No calibrated prediction is available.")

    def run_technical_feed(self):
        self.results_tabs.select(self.technical_tab)
        try:
            ticks = load_index_ticks(self.database)
            quality, candles, snapshot, results = build_feed_evidence(ticks)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.technical_sample_info.config(text=f"RECORDED FEED · no assessment: {exc}")
            self.notice.set("Recorded Groww updates do not yet meet the Technical Agent's freshness and coverage requirements.")
            return
        accepted = sum(row.status == "accepted" for row in quality)
        rows = [f"{item.horizon_minutes} min: {item.bias} rules score {item.score:+.1f} / 100"
                if item.score is not None else f"{item.horizon_minutes} min: unavailable (session window)"
                for item in results]
        try:
            session = regular_session(snapshot.as_of.date())
            verification = verify_bundle(candles, snapshot, results, session)
            gate = check_risk(snapshot, results, verification, session)
            eligible = [item for item, decision in zip(results, gate.horizons) if decision.paper_eligible]
            saved = record_paper_assessments(self.database, eligible)
            log_status = (f"Verification {'passed' if verification.passed else 'failed'} · "
                          f"Risk allowed {len(eligible)}/3 for experimental paper logging · "
                          f"{saved['inserted']} new, {saved['duplicates']} already fixed.")
        except (OSError, sqlite3.Error, ValueError):
            log_status = "Paper log could not be updated; no earlier assessment was replaced."
        self.technical_sample_info.config(text=f"GROWW FEED SAMPLES · {accepted} accepted minutes · approximate observed OHLC · no calibrated confidence\n" + "   |   ".join(rows) + "\n" + log_status)
        self.notice.set("Technical rules ran on recent Groww feed samples. Scores are experimental and cannot trigger trades.")

    def run_guard_sample(self):
        self.results_tabs.select(self.guard_tab)
        session, candles = synthetic_session()
        as_of = session.open + timedelta(minutes=75)
        observed = [candle for candle in candles if candle.end <= as_of]
        snapshot = build_technical_snapshot(observed, as_of, generated_at=as_of)
        assessments = [assess(snapshot, minutes) for minutes in (15, 30, 60)]
        verification = verify_bundle(observed, snapshot, assessments, session)
        gate = check_risk(snapshot, assessments, verification, session)
        allowed = sum(item.paper_eligible for item in gate.horizons)
        self.guard_info.config(text=f"INVENTED SAMPLE · Verification {'passed' if verification.passed else 'failed'} structural checks · "
                                    f"Risk allowed {allowed}/3 paper horizons · public forecast and trading blocked")
        self.notice.set("Verification checks internal consistency; it cannot certify provider data or predictive skill.")

    def run_master_sample(self):
        self.results_tabs.select(self.master_tab)
        session, candles = synthetic_session()
        as_of = session.open + timedelta(minutes=75)
        observed = [candle for candle in candles if candle.end <= as_of]
        try:
            snapshot = build_technical_snapshot(observed, as_of, generated_at=as_of)
            technical = [assess(snapshot, minutes) for minutes in (15, 30, 60)]
            bull = [build_bull_case(snapshot, minutes) for minutes in (15, 30, 60)]
            bear = [build_bear_case(snapshot, minutes) for minutes in (15, 30, 60)]
            verification = verify_bundle(observed, snapshot, technical, session)
            risk = check_risk(snapshot, technical, verification, session)
            result = review_bundle(snapshot, technical, bull, bear, verification, risk)
        except (ValueError, TypeError):
            self.master_info.config(text="Master rejected the sample bundle; no review is available.")
            return
        self._show_master_result(result, "INVENTED SAMPLE")
        self.notice.set("Master checked shared cutoffs and gates. All invented-sample horizons remain blocked.")

    def _show_master_result(self, result, label):
        lines = [f"{row.horizon_minutes}m: {row.status.replace('_', ' ')} · Technical {row.technical_bias} · "
                 f"Bull {row.bull_observations} observations · Bear {row.bear_observations} observations · "
                 f"{'; '.join(row.reasons)}" for row in result.horizons]
        self.master_info.config(text=f"MASTER · {label} · no forecast or trading authority\n" + "\n".join(lines))
        self.results_tabs.select(self.master_tab)

    def run_index_sample(self):
        self.results_tabs.select(self.index_tab)
        base = datetime(2026, 10, 1, 10, 0, tzinfo=IST)
        current = base + timedelta(minutes=15)
        symbols = tuple(f"DEMO-BANK-{number:02d}" for number in range(1, 15))
        percents = (18, 14, 10, 8, 8, 8, 7, 7, 6, 5, 4, 3, 1, 1)
        weights = WeightSnapshot("DEMO-BANKNIFTY", base - timedelta(days=1),
                                 base + timedelta(days=1), base - timedelta(hours=1),
                                 "synthetic", "synthetic:bank-contribution-demo",
                                 tuple(Weight(symbol, percent) for symbol, percent in zip(symbols, percents)))
        pairs = [PricePair(symbol, base, current, 100.0,
                           101.0 if index == 0 else 99.5 if index == 1 else 100.0,
                           "synthetic") for index, symbol in enumerate(symbols)]
        result = calculate_contributions(weights,
                                         PricePair("DEMO-BANKNIFTY", base, current,
                                                   50000.0, 50050.0, "synthetic"), pairs)
        self.index_info.config(
            text=f"INVENTED 14-BANK SAMPLE · top lift {result.rows[0].symbol} "
                 f"{result.rows[0].estimated_index_points:+.1f} estimated points · "
                 f"largest drag {result.rows[-1].symbol} {result.rows[-1].estimated_index_points:+.1f} points\n"
                 f"Weighted basket {result.estimated_index_point_change:+.1f} estimated points · "
                 f"invented index {result.observed_index_point_change:+.1f} points · "
                 f"unexplained residual {result.residual_percentage_points:+.3f} percentage points. "
                 "No official weights or real bank prices were used.")
        self.notice.set("Index Contribution arithmetic ran on invented banks. The dated NSE reference was not used; no real contribution is available.")

    def run_options_sample(self):
        self.results_tabs.select(self.options_tab)
        def contract(strike, side, oi, volume, iv):
            return {"trading_symbol": f"BANKNIFTYDEMO{strike}{side}",
                    "ltp": 100.0, "open_interest": oi, "volume": volume,
                    "greeks": {"iv": iv, "delta": 0.5 if side == "CE" else -0.5,
                               "gamma": 0.001, "theta": -5.0, "vega": 10.0,
                               "rho": 2.0 if side == "CE" else -2.0}}
        response = {"status": "SUCCESS", "payload": {"underlying_ltp": 55040.0,
                    "strikes": {"55000": {"CE": contract(55000, "CE", 100, 20, 18.0),
                                          "PE": contract(55000, "PE", 150, 30, 19.0)},
                                "55100": {"CE": contract(55100, "CE", 200, 40, 18.5),
                                          "PE": contract(55100, "PE", 250, 50, 19.5)}}}}
        result = analyze_option_chain(response, date(2026, 10, 27),
                                      datetime(2026, 10, 5, 10, 0, tzinfo=IST),
                                      source_kind="synthetic")
        structure = describe_option_structure(result)
        atm_call = next(row for row in result.contracts
                        if row.strike == result.atm_strike and row.side == "CE")
        self.options_info.config(text=f"INVENTED OPTION CHAIN · {result.complete_pairs}/{result.strikes} complete strike pairs · "
                                      f"OI put/call ratio {result.oi_pcr:.2f} · "
                                      f"volume put/call ratio {result.volume_pcr:.2f} · "
                                      f"nearest strike {result.atm_strike:,.0f} · "
                                      f"sample ATM call gamma {atm_call.gamma:.4f} · "
                                      f"hypothetical max-pain strike {structure['max_pain_strike']:,.0f} · "
                                      "no exchange timestamp, forecast or trading signal")
        self.notice.set("Options Agent ran on invented option-chain values. Its ratios are descriptive; no real market freshness or predictive skill is established.")

    def show_weight_reference(self):
        path = Path(__file__).resolve().parent / "reference" / "nse-bank-weights-2026-09-30.json"
        try:
            report = load_reference_weights(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            messagebox.showerror("NSE bank weight reference", f"Saved reference could not be loaded: {exc}", parent=self.root)
            return
        window = tk.Toplevel(self.root)
        window.title("NSE NIFTY Bank weights | dated reference")
        window.geometry("790x620")
        window.minsize(620, 480)
        window.configure(bg=BG)
        container = ttk.Frame(window, padding=20)
        container.pack(fill="both", expand=True)
        self.text(container, f"NSE NIFTY Bank weights as of {report['as_of_date']}", size=17, bold=True)
        self.text(container, "Official dated reference · not a live intraday snapshot · no prediction or trade", color=ACCENT, bold=True)
        retrieved = datetime.fromisoformat(report["retrieved_at_utc"]).strftime("%d %b %Y %H:%M UTC")
        self.text(container, f"Retrieved: {retrieved}   |   {len(report['constituents'])} banks   |   Total {sum(row['weight_percent'] for row in report['constituents']):.2f}%", color=MUTED)
        frame = ttk.Frame(container)
        frame.pack(fill="both", expand=True, pady=(5, 10))
        table = ttk.Treeview(frame, columns=("symbol", "company", "weight"), show="headings")
        for column, heading, width in (("symbol", "Symbol", 150), ("company", "Bank", 410), ("weight", "Weight %", 120)):
            table.heading(column, text=heading)
            table.column(column, width=width, anchor="e" if column == "weight" else "w")
        for row in sorted(report["constituents"], key=lambda item: -item["weight_percent"]):
            table.insert("", "end", values=(row["symbol"], row["company"], f"{row['weight_percent']:.2f}"))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scroll.set)
        table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.text(container, "Source: " + report["source_url"], color=MUTED, wrap=740)
        self.text(container, "PDF SHA-256: " + report["source_pdf_sha256"], color=MUTED, wrap=740)
        self.text(container, "These percentages describe the dated report. An as-of date does not establish its publication time or prove current intraday weights. Aligned constituent prices are not yet collected.", color=MUTED, wrap=740)
        tk.Button(container, text="Close", command=window.destroy, bg=PANEL, fg=INK,
                  padx=20, pady=5, font=("Segoe UI", 10)).pack(anchor="e")
        window.transient(self.root)
        window.grab_set()
        self.notice.set("Opened the 30 Sep NSE bank-weight reference; it is not a live contribution result.")

    def run_outcome_sample(self):
        self.results_tabs.select(self.outcome_tab)
        session, candles = synthetic_session()
        as_of = session.open + timedelta(minutes=75)
        observed = [candle for candle in candles if candle.end <= as_of]
        try:
            snapshot = build_technical_snapshot(observed, as_of, generated_at=as_of)
            assessments = [assess(snapshot, minutes) for minutes in (15, 30, 60)]
            outcomes = match_outcomes(assessments, candles, session)
        except (ValueError, TypeError):
            self.outcome_sample_info.config(text="Invented outcome check failed; no result is available.")
            return
        rows = [f"{row.horizon_minutes} min: {row.realized_return_pct:+.2f}% observed later"
                if row.status == "ready" else f"{row.horizon_minutes} min: {row.status}" for row in outcomes]
        self.outcome_sample_info.config(text="INVENTED SAMPLE · exact later endpoints · no accuracy claim\n" + "   |   ".join(rows))
        self.notice.set("Offline outcome matching used invented prices. Future outcomes were not inputs to the Technical Agent.")

    def build_history(self):
        self.text(self.history, "History you can keep and export", size=19, bold=True)
        self.text(self.history, "Records stay on this laptop across restarts. This version performs no automatic deletion. Demo data is identified by its source and dataset.", color=MUTED)
        bar = ttk.Frame(self.history)
        bar.pack(fill="x", pady=(0, 12))
        ttk.Button(bar, text="Load sample history", command=self.load_demo).pack(side="left")
        self.export_button = ttk.Button(bar, text="Export selected to CSV", command=self.export_selected)
        self.export_button.pack(side="left", padx=8)
        ttk.Button(bar, text="Back up market database", command=self.backup_history).pack(side="left", padx=(0, 8))
        ttk.Button(bar, text="Export daily research CSV", command=self.export_daily_research).pack(side="left", padx=(0, 8))
        ttk.Button(bar, text="Refresh", command=self.refresh).pack(side="left")
        paper_bar = ttk.Frame(self.history)
        paper_bar.pack(fill="x", pady=(0, 8))
        ttk.Button(paper_bar, text="Match later paper outcomes", command=self.match_paper_outcomes).pack(side="left")
        ttk.Button(paper_bar, text="Export experimental paper report", command=self.export_paper_report).pack(side="left", padx=8)
        ttk.Button(paper_bar, text="Check recorded history", command=self.check_recorded_history).pack(side="left")
        ttk.Button(paper_bar, text="Export history comparison CSV", command=self.export_recorded_comparison).pack(side="left", padx=8)
        research_bar = ttk.Frame(self.history)
        research_bar.pack(fill="x", pady=(0, 8))
        ttk.Button(research_bar, text="Export intraday research rows",
                   command=self.export_intraday_research).pack(side="left")
        ttk.Button(research_bar, text="Review expiry-day movement",
                   command=self.review_expiry_day_movement).pack(side="left", padx=8)
        ttk.Label(research_bar, text="Point-in-time 15/30/60 labels; empty until verified feed history exists.",
                  foreground=MUTED).pack(side="left", padx=10)
        backup_bar = ttk.Frame(self.history)
        backup_bar.pack(fill="x", pady=(0, 8))
        ttk.Button(backup_bar, text="Back up whole project (ZIP)",
                   command=self.backup_whole_project).pack(side="left")
        ttk.Button(backup_bar, text="Verify backup can restore",
                   command=self.verify_whole_project_backup).pack(side="left", padx=8)
        ttk.Label(backup_bar, text="Checks a selected ZIP in temporary storage without changing this project.",
                  foreground=MUTED).pack(side="left", padx=10)
        self.dataset_table = ttk.Treeview(self.history, columns=("dataset", "source", "count", "last"), show="headings", height=5, selectmode="browse")
        for key, title, width in (("dataset", "Dataset", 360), ("source", "Source", 150), ("count", "Candles", 100), ("last", "Last candle end (IST)", 245)):
            self.dataset_table.heading(key, text=title)
            self.dataset_table.column(key, width=width, minwidth=90, stretch=key == "dataset")
        self.dataset_table.pack(fill="x")
        self.dataset_table.bind("<<TreeviewSelect>>", self.select_dataset)
        self.text(self.history, "Latest saved candles  ·  One row per minute  ·  All displayed times are IST", color=MUTED)
        container = ttk.Frame(self.history)
        container.pack(fill="both", expand=True)
        cols = ("time", "open", "high", "low", "close")
        self.candle_table = ttk.Treeview(container, columns=cols, show="headings", height=7)
        for key in cols:
            self.candle_table.heading(key, text="Candle end (IST)" if key == "time" else key.title())
            self.candle_table.column(key, width=230 if key == "time" else 130, anchor="w" if key == "time" else "e")
        scroll = ttk.Scrollbar(container, orient="vertical", command=self.candle_table.yview)
        self.candle_table.configure(yscrollcommand=scroll.set)
        self.candle_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.storage_info = self.text(self.history, "", color=MUTED, size=9)
        self.storage_info.pack_configure(side="bottom", before=container)

    def build_activity(self):
        self.text(self.activity_page, "14-bank volume and order-book activity", size=19, bold=True)
        self.text(self.activity_page,
                  "Volume means cumulative traded shares. Buy and sell quantities are waiting orders, not executed trade direction. "
                  "All-bank sums are unweighted share counts, not Bank Nifty index volume. "
                  "15-second sweeps can miss briefer spikes.", color=MUTED)
        actions = ttk.Frame(self.activity_page)
        actions.pack(anchor="w", pady=(0, 8))
        ttk.Button(actions, text="Refresh bank activity", command=self.refresh_activity).pack(side="left")
        ttk.Button(actions, text="Export bank quotes CSV", command=self.export_bank_quotes).pack(side="left", padx=8)
        ttk.Button(actions, text="Export spike alerts CSV", command=self.export_activity_alerts).pack(side="left")
        ttk.Button(actions, text="Open Groww connection", command=self.connect).pack(side="left", padx=8)
        self.activity_status = self.text(self.activity_page, "No bank quote snapshots saved yet.", color=ACCENT)
        self.text(self.activity_page, "Recent exploratory spikes (trial thresholds; no trading signal)", bold=True)
        self.activity_alerts = self.text(self.activity_page, "No spikes recorded.", color=MUTED)
        columns = ("bank", "price", "volume", "buy", "sell", "received")
        table_frame = ttk.Frame(self.activity_page)
        table_frame.pack(fill="x")
        self.activity_table = ttk.Treeview(table_frame, columns=columns, show="headings", height=10)
        for key, title, width in (("bank", "Bank", 175), ("price", "Last price", 110),
                                  ("volume", "Traded shares today", 185),
                                  ("buy", "Waiting buy qty", 175), ("sell", "Waiting sell qty", 175),
                                  ("received", "Received (IST)", 180)):
            self.activity_table.heading(key, text=title)
            self.activity_table.column(key, width=width, minwidth=90)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.activity_table.yview)
        self.activity_table.configure(yscrollcommand=scroll.set)
        self.activity_table.pack(side="left", fill="x", expand=True)
        scroll.pack(side="right", fill="y")

    def refresh_activity(self):
        self.activity_table.delete(*self.activity_table.get_children())
        try:
            sweeps = load_bank_quote_sweeps(self.database, limit=1)
            alerts = load_activity_alerts(self.database, limit=3)
            count = count_bank_quotes(self.database)
            reference = load_reference_weights(Path(__file__).resolve().parent /
                                               "reference" / "nse-bank-weights-2026-09-30.json")
            expected = ["NSE-" + row["symbol"] for row in reference["constituents"]]
        except (OSError, sqlite3.Error, ValueError):
            self.activity_status.config(text="Bank activity archive could not be read.")
            return
        if not sweeps:
            self.activity_status.config(text="No bank quote snapshots saved yet. Open Groww connection during regular NSE hours to record them.")
            self.activity_alerts.config(text="No spikes recorded.")
            return
        latest = sweeps[-1]
        summary = summarize_bank_quotes(latest, expected)
        def show(value, decimals=0):
            return "—" if value is None else f"{value:,.{decimals}f}"
        for row in latest:
            self.activity_table.insert("", "end", values=(row.symbol.removeprefix("NSE-"),
                show(row.last_price, 2), show(row.volume), show(row.total_buy_quantity),
                show(row.total_sell_quantity), self.ist(row.received_at)))
        if summary["complete"]:
            self.activity_status.config(text=(
                f"Latest sweep: 14/14 banks · {show(summary['cumulative_traded_shares'])} cumulative traded shares · "
                f"{show(summary['outstanding_buy_shares'])} waiting buy · "
                f"{show(summary['outstanding_sell_shares'])} waiting sell · {count:,} saved quotes. "
                "Receipt times differ across banks; freshness unverified."))
        else:
            self.activity_status.config(text=f"Latest sweep: {summary['coverage']}/14 banks · "
                f"all-bank totals unavailable · {count:,} saved quotes. Receipt-time data only.")
        self.activity_alerts.config(text=("\n".join(
            f"{row['symbol'].removeprefix('NSE-')}: {row['kind'].replace('_', ' ')} "
            f"at {self.ist(row['at'])} IST, {row['multiple']:.1f}× prior median"
            for row in alerts) if alerts else "No spikes recorded. At least six earlier regular snapshots are needed."))

    def export_bank_quotes(self):
        self._export_activity_csv(export_bank_quotes_csv, "bank-activity-quotes.csv", "bank quotes")

    def export_activity_alerts(self):
        self._export_activity_csv(export_activity_alerts_csv, "bank-activity-spikes.csv", "spike alerts")

    def _export_activity_csv(self, exporter, filename, label):
        if not count_bank_quotes(self.database):
            self.notice.set("No bank activity has been recorded for export.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title=f"Export {label}",
            defaultextension=".csv", initialfile=filename, filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = exporter(self.database, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Export", f"Could not export {label}.", parent=self.root)
            return
        self.notice.set(f"Exported {count:,} {label} rows. Buy/sell quantities are outstanding orders, not executed trades.")

    def build_depth(self):
        self.text(self.depth_page, "Timestamped five-level order book", size=19, bold=True)
        self.text(self.depth_page,
                  "Each bank shows its latest saved five buy and sell levels. Times can differ between banks. "
                  "Visible orders can be changed or cancelled; they are not completed buy/sell trades.", color=MUTED)
        actions = ttk.Frame(self.depth_page)
        actions.pack(anchor="w", pady=(0, 8))
        ttk.Button(actions, text="Refresh order depth", command=self.refresh_depth).pack(side="left")
        ttk.Button(actions, text="Export depth history CSV", command=self.export_depth).pack(side="left", padx=8)
        ttk.Button(actions, text="Open Groww connection", command=self.connect).pack(side="left")
        self.depth_status = self.text(self.depth_page, "No timestamped depth updates recorded.", color=ACCENT)
        self.depth_total = self.text(self.depth_page, "All-bank top-five totals unavailable until 14 books align.", color=MUTED)
        self.depth_alerts = self.text(self.depth_page, "No trial depth spikes recorded.", color=MUTED)
        columns = ("bank", "bid", "buyqty", "ask", "sellqty", "exchange", "lag")
        frame = ttk.Frame(self.depth_page)
        frame.pack(fill="x")
        self.depth_table = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        for key, title, width in (("bank", "Bank", 150), ("bid", "Best bid", 115),
                                  ("buyqty", "Top-5 buy qty", 155), ("ask", "Best ask", 115),
                                  ("sellqty", "Top-5 sell qty", 155),
                                  ("exchange", "Exchange time (IST)", 210),
                                  ("lag", "Receipt lag", 120)):
            self.depth_table.heading(key, text=title)
            self.depth_table.column(key, width=width, minwidth=90)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.depth_table.yview)
        self.depth_table.configure(yscrollcommand=scroll.set)
        self.depth_table.pack(side="left", fill="x", expand=True)
        scroll.pack(side="right", fill="y")

    def refresh_depth(self):
        self.depth_table.delete(*self.depth_table.get_children())
        try:
            latest = latest_depth_by_symbol(self.database)
            count = count_depth_ticks(self.database)
            alerts = [row for row in load_activity_alerts(self.database, limit=100)
                      if row["kind"].startswith("top5_")][:3]
            reference = load_reference_weights(Path(__file__).resolve().parent /
                                               "reference" / "nse-bank-weights-2026-09-30.json")
            expected = ["NSE-" + row["symbol"] for row in reference["constituents"]]
            combined = align_latest_depth(latest, expected)
        except (OSError, sqlite3.Error, ValueError):
            self.depth_status.config(text="Order-depth archive could not be read.")
            return
        if not latest:
            self.depth_status.config(text="No timestamped depth updates recorded. Select the depth checkbox during a regular NSE feed capture.")
            self.depth_total.config(text="All-bank top-five totals unavailable until 14 books align.")
            self.depth_alerts.config(text="No trial depth spikes recorded.")
            return
        def show(value, decimals=0):
            return "—" if value is None else f"{value:,.{decimals}f}"
        for row in latest.values():
            lag = (row.received_at - row.exchange_at).total_seconds()
            self.depth_table.insert("", "end", values=(row.symbol.removeprefix("NSE-"),
                show(row.best_bid, 2), show(row.buy_quantity), show(row.best_ask, 2),
                show(row.sell_quantity), self.ist(row.exchange_at), f"{lag:.1f}s"))
        self.depth_status.config(text=f"{count:,} saved order-book updates across {len(latest)}/14 banks. "
            "These are latest per-bank books, not a simultaneous all-bank snapshot. Exchange timestamp units still require a live-feed check.")
        if combined["status"] == "aligned_trial":
            saved_at = datetime.fromisoformat(combined["anchor_exchange_at"]).astimezone(IST)
            self.depth_total.config(text=(
                f"Saved trial all-bank top-five books at {saved_at:%d %b %Y %H:%M:%S} IST: "
                f"buy {combined['buy_shares']:,.0f} shares "
                f"(₹{combined['buy_order_value_inr']:,.0f} visible order value); "
                f"sell {combined['sell_shares']:,.0f} shares "
                f"(₹{combined['sell_order_value_inr']:,.0f}). "
                f"Exchange span {combined['exchange_span_seconds']:.1f}s. "
                "Unweighted visible orders; no completed trade-side or index-volume claim."))
        else:
            self.depth_total.config(text=f"All-bank top-five totals unavailable: {combined['reason'].replace('_', ' ')} "
                f"({combined['coverage']}/14 banks). Trial limit: 2s exchange and 5s receipt span.")
        self.depth_alerts.config(text=("Recent trial spikes: " + "; ".join(
            f"{row['symbol'].removeprefix('NSE-')} {row['kind'].replace('_', ' ')} "
            f"{row['multiple']:.1f}× at {self.ist(row['at'])} IST" for row in alerts)
            if alerts else "No trial depth spikes recorded. Several nearby updates are required."))

    def export_depth(self):
        if not count_depth_ticks(self.database):
            self.notice.set("No timestamped order-depth updates are available to export.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="Export bank order depth",
            defaultextension=".csv", initialfile="bank-order-depth.csv",
            filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_depth_ticks_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Export", "Could not export order depth.", parent=self.root)
            return
        self.notice.set(f"Exported {count:,} five-level depth updates. These are visible orders, not executed trades.")

    def build_range(self):
        self.text(self.range_page, "Full-day Bank Nifty movement range", size=19, bold=True)
        self.text(self.range_page,
                  "Historical sampled high, low and high-to-low distance need every regular-session minute. "
                  "The app has no day-range forecast or accuracy claim yet. Sampled extremes may miss the exchange's official extremes.",
                  color=MUTED)
        actions = ttk.Frame(self.range_page)
        actions.pack(anchor="w", pady=(0, 9))
        self.official_fetch_button = ttk.Button(actions, text="Fetch 5 recent NSE days",
                                                command=self.fetch_recent_official_range)
        self.official_fetch_button.pack(side="left")
        ttk.Button(actions, text="Export NSE daily CSV", command=self.export_official_range).pack(side="left", padx=8)
        ttk.Button(actions, text="Inspect saved days", command=self.refresh_daily_range).pack(side="left")
        ttk.Button(actions, text="Export daily ranges CSV", command=self.export_daily_range).pack(side="left", padx=8)
        ttk.Button(actions, text="Open Groww connection", command=self.connect).pack(side="left")
        audit_actions = ttk.Frame(self.range_page)
        audit_actions.pack(anchor="w", pady=(0, 9))
        self.official_backfill_button = ttk.Button(audit_actions, text="Fetch 20 older NSE days",
                                                   command=self.backfill_official_range)
        self.official_backfill_button.pack(side="left")
        ttk.Button(audit_actions, text="Export NSE vs Groww audit CSV",
                   command=self.export_range_comparison).pack(side="left", padx=(8, 0))
        ttk.Button(audit_actions, text="Review past-only range reference",
                   command=self.refresh_daily_reference).pack(side="left", padx=(8, 0))
        ttk.Button(audit_actions, text="Export reference CSV",
                   command=self.export_daily_reference).pack(side="left", padx=(8, 0))
        paper_actions = ttk.Frame(self.range_page)
        paper_actions.pack(anchor="w", pady=(0, 9))
        ttk.Button(paper_actions, text="Record next-day paper reference",
                   command=self.record_daily_paper_reference).pack(side="left")
        ttk.Button(paper_actions, text="Refresh paper results",
                   command=self.refresh_daily_paper).pack(side="left", padx=(8, 0))
        ttk.Button(paper_actions, text="Export paper record CSV",
                   command=self.export_daily_paper).pack(side="left", padx=(8, 0))
        self.official_range_status = self.text(self.range_page,
                                               "Official NSE daily history has not been checked.", color=ACCENT)
        official_columns = ("day", "open", "high", "low", "close", "range", "source")
        self.official_range_table = ttk.Treeview(self.range_page, columns=official_columns,
                                                 show="headings", height=5)
        for key, title, width in (("day", "NSE session", 145), ("open", "Official open", 165),
                                  ("high", "Official high", 165), ("low", "Official low", 165),
                                  ("close", "Official close", 165),
                                  ("range", "Official range", 165),
                                  ("source", "Source", 200)):
            self.official_range_table.heading(key, text=title)
            self.official_range_table.column(key, width=width, minwidth=100)
        self.official_range_table.pack(fill="x", pady=(0, 8))
        self.daily_reference_status = self.text(self.range_page,
            "Past-only daily-range reference has not been evaluated. It is not a live forecast.", color=MUTED)
        self.daily_paper_status = self.text(self.range_page,
            "No pre-session paper range reference has been checked.", color=ACCENT)
        self.range_status = self.text(self.range_page,
                                      "Groww sampled-day coverage has not been checked.", color=ACCENT)
        self.text(self.range_page,
                  "NSE daily OHLC and Groww sampled extremes stay separate. Historical labels are not predictions; "
                  "remaining-day labels require complete later data.", color=MUTED)
        columns = ("day", "status", "coverage", "high", "low", "range", "percent")
        frame = ttk.Frame(self.range_page)
        frame.pack(fill="both", expand=True)
        self.range_table = ttk.Treeview(frame, columns=columns, show="headings", height=12)
        for key, title, width in (("day", "Session (IST)", 140), ("status", "Evidence status", 220),
                                  ("coverage", "Accepted minutes", 170),
                                  ("high", "Sampled high", 150), ("low", "Sampled low", 150),
                                  ("range", "Range points", 150), ("percent", "% of open", 130)):
            self.range_table.heading(key, text=title)
            self.range_table.column(key, width=width, minwidth=95)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.range_table.yview)
        self.range_table.configure(yscrollcommand=scroll.set)
        self.range_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.refresh_official_range()
        self.refresh_daily_paper()

    def refresh_official_range(self):
        self.official_range_table.delete(*self.official_range_table.get_children())
        try:
            rows = load_official_days(self.database)
        except (OSError, sqlite3.Error, ValueError):
            self.official_range_status.config(text="Official NSE daily archive could not be read.")
            return
        if not rows:
            self.official_range_status.config(text="No official NSE daily Bank Nifty rows saved yet. Fetch a small recent batch to begin.")
            return
        self.official_range_status.config(text=f"{len(rows)} official NSE daily Bank Nifty row(s) saved. "
            f"Latest: {rows[-1].day.isoformat()}. Source URL and SHA-256 are included in the NSE export. "
            "These are observed historical ranges, not forecasts.")
        for row in reversed(rows[-100:]):
            self.official_range_table.insert("", "end", values=(row.day.isoformat(),
                f"{row.open:,.2f}", f"{row.high:,.2f}", f"{row.low:,.2f}",
                f"{row.close:,.2f}", f"{row.range_points:,.2f}", "NSE daily CSV"))

    def fetch_recent_official_range(self):
        if self.offline:
            self.notice.set("Offline research mode. Live data requires a separately authorized market-hours check; no connection was opened.")
            return
        self.official_fetch_button.config(state="disabled")
        self.official_range_status.config(text="Fetching five completed daily CSVs from NSE's archive…")
        def work():
            try:
                outcome = sync_recent_official_days(self.database, 5)
            except (OSError, sqlite3.Error, ValueError) as exc:
                outcome = {"requested": 0, "inserted": 0, "duplicates": 0,
                           "failed": [("batch", type(exc).__name__)]}
            def finish():
                self.official_fetch_button.config(state="normal")
                self.refresh_official_range()
                self.notice.set(f"NSE daily fetch: {outcome['inserted']} added, "
                                f"{outcome['duplicates']} already saved, "
                                f"{len(outcome['failed'])} failed. No prediction was made.")
            try:
                self.root.after(0, finish)
            except RuntimeError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def backfill_official_range(self):
        if self.offline:
            self.notice.set("Offline research mode. Live data requires a separately authorized market-hours check; no connection was opened.")
            return
        self.official_backfill_button.config(state="disabled")
        self.official_range_status.config(text="Checking archive gaps and fetching up to 20 older NSE daily CSVs…")
        def work():
            try:
                outcome = sync_older_official_days(self.database, 20)
            except (OSError, sqlite3.Error, ValueError) as exc:
                outcome = {"requested": 0, "inserted": 0, "duplicates": 0,
                           "failed": [("batch", type(exc).__name__)]}
            def finish():
                self.official_backfill_button.config(state="normal")
                self.refresh_official_range()
                self.notice.set(f"Older NSE daily fetch: {outcome['inserted']} added, "
                                f"{len(outcome['failed'])} failed. Review gaps before evaluation.")
            try:
                self.root.after(0, finish)
            except RuntimeError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def export_official_range(self):
        try:
            if not load_official_days(self.database):
                self.notice.set("No NSE daily Bank Nifty rows are saved for export.")
                return
        except (OSError, sqlite3.Error, ValueError):
            self.notice.set("NSE daily archive could not be read. Nothing was exported.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root,
            title="Export official NSE daily Bank Nifty OHLC", defaultextension=".csv",
            initialfile="banknifty-nse-daily-ohlc.csv", filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_official_days_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Export", "Could not export NSE daily history. Choose another CSV destination.", parent=self.root)
            return
        self.notice.set(f"Exported {count} official NSE historical day(s) with source URLs and file hashes.")

    def refresh_daily_range(self):
        self.range_table.delete(*self.range_table.get_children())
        try:
            rows = build_daily_range_history(self.database)
            comparisons = compare_official_and_sampled(self.database)
        except (OSError, sqlite3.Error, ValueError):
            self.range_status.config(text="Daily range archive could not be checked; no range is reported.")
            return
        if not rows:
            self.range_status.config(text=f"No real Groww Bank Nifty session recorded. "
                f"0/{len(comparisons)} NSE days can be compared with a complete sampled session. "
                "No historical sampled range or forecast is available.")
            return
        complete = sum(row["status"] == "complete_sampled_session" for row in rows)
        compared = sum(row["status"] in ("sampled_within_official_range",
                           "sampled_outside_official_range") for row in comparisons)
        outside = sum(row["status"] == "sampled_outside_official_range" for row in comparisons)
        self.range_status.config(text=f"{len(rows)} observed session(s); {complete} complete sampled day-range label(s). "
            f"{compared}/{len(comparisons)} NSE days compared; {outside} sampled range(s) outside official extremes. "
            "Incomplete days have blank ranges. No day-range forecast is available.")
        for row in reversed(rows):
            def number(field, decimals=2):
                value = row[field]
                return "—" if value is None else f"{value:,.{decimals}f}"
            self.range_table.insert("", "end", values=(row["session_date_ist"],
                row["status"].replace("_", " "),
                f"{row['accepted_minutes']}/{row['expected_minutes']}",
                number("sampled_day_high"), number("sampled_day_low"),
                number("sampled_range_points"), number("sampled_range_pct_of_open", 3)))

    def export_daily_range(self):
        try:
            rows = build_daily_range_history(self.database)
        except (OSError, sqlite3.Error, ValueError):
            self.notice.set("Daily-range coverage could not be read. Nothing was exported.")
            return
        if not rows:
            self.notice.set("No recorded regular session is available for daily-range export.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root,
            title="Export historical sampled daily ranges", defaultextension=".csv",
            initialfile="banknifty-daily-range-coverage.csv",
            filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_daily_range_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Export", "Could not export daily range coverage. Choose another CSV destination.", parent=self.root)
            return
        self.notice.set(f"Exported {count} historical day-coverage rows. Blank ranges mean incomplete data; these are not predictions.")

    def export_range_comparison(self):
        try:
            if not load_official_days(self.database):
                self.notice.set("No NSE daily rows are saved for the source comparison.")
                return
        except (OSError, sqlite3.Error, ValueError):
            self.notice.set("NSE daily archive could not be read. Nothing was exported.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root,
            title="Export NSE versus Groww range audit", defaultextension=".csv",
            initialfile="banknifty-nse-groww-range-audit.csv",
            filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_range_comparison_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Export", "Could not export the range audit.", parent=self.root)
            return
        self.notice.set(f"Exported {count} NSE-to-sampled audit rows, including dates without complete Groww sessions.")

    def refresh_daily_reference(self):
        try:
            rows = evaluate_daily_reference(load_official_days(self.database))
            result = summarize_daily_reference(rows)
        except (OSError, sqlite3.Error, ValueError):
            self.daily_reference_status.config(text="Past-only reference could not be evaluated from the NSE archive.")
            return
        if not rows:
            self.daily_reference_status.config(text="Too few consecutive NSE days for the fixed 40-day reference. No performance result.")
            return
        self.daily_reference_status.config(text=(
            f"Exploratory reconstructed reference: {result['evaluated_days']} historical days; "
            f"{result['reconstructed_from_later_retrieval_days']} used NSE files archived after "
            "their target-day opening; "
            f"both extremes inside interval {result['both_extremes_contained_pct']:.1f}%; "
            f"mean interval width {result['mean_reference_width_points']:.0f} points "
            f"versus mean actual day range {result['mean_official_range_points']:.0f} points. "
            "Wide intervals can appear accurate. These are not 144 pre-session forecasts; "
            "no untouched test or live forecast."))

    def export_daily_reference(self):
        try:
            rows = evaluate_daily_reference(load_official_days(self.database))
        except (OSError, sqlite3.Error, ValueError):
            self.notice.set("Past-only reference could not be evaluated.")
            return
        if not rows:
            self.notice.set("Not enough consecutive NSE days to export reference results.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root,
            title="Export exploratory historical range reference", defaultextension=".csv",
            initialfile="banknifty-daily-reference-evaluation.csv",
            filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_daily_reference_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Export", "Could not export historical reference results.", parent=self.root)
            return
        self.notice.set(f"Exported {count} retrospectively reconstructed historical reference rows. These are not pre-session records or live forecasts.")

    def record_daily_paper_reference(self):
        try:
            outcome, payload = record_next_reference(self.database)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.notice.set(f"Could not record a pre-session reference: {exc}")
            return
        self.refresh_daily_paper()
        self.notice.set(f"{outcome.capitalize()} {payload['session_date_ist']} research reference before market open. "
                        "It is not a validated forecast or trading signal.")

    def refresh_daily_paper(self):
        try:
            rows = load_scored_references(self.database)
        except (OSError, sqlite3.Error, ValueError):
            self.daily_paper_status.config(text="Saved paper references could not be verified.")
            return
        if not rows:
            self.daily_paper_status.config(text="No pre-session research reference recorded. "
                "The next one can be locked only before its market opening, with a current official archive.")
            return
        latest = rows[-1]
        scored = sum(row["status"] == "scored_official_day" for row in rows)
        self.daily_paper_status.config(text=(
            f"Paper range reference {latest['session_date_ist']}: {latest['reference_lower']}–"
            f"{latest['reference_upper']} points; recorded {latest['recorded_at_utc']} UTC; "
            f"{latest['status'].replace('_', ' ')}. {scored}/{len(rows)} later NSE days scored. "
            "Research benchmark only; no trading signal."))

    def export_daily_paper(self):
        try:
            if not load_scored_references(self.database):
                self.notice.set("No pre-session paper reference exists to export.")
                return
        except (OSError, sqlite3.Error, ValueError):
            self.notice.set("Paper reference archive could not be verified.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root,
            title="Export pre-session daily-range paper records", defaultextension=".csv",
            initialfile="banknifty-daily-paper-range-reference.csv",
            filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_daily_paper_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Export", "Could not export paper range records.", parent=self.root)
            return
        self.notice.set(f"Exported {count} pre-session paper record(s).")

    def build_setup(self):
        self.text(self.setup, "Connect Groww. Validate before forecasting.", size=19, bold=True)
        ttk.Button(self.setup, text="Open visual project map",
                   command=self.open_project_map).pack(anchor="w", pady=(0, 8))
        ttk.Button(self.setup, text="What is needed from me?", command=self.show_readiness).pack(anchor="w", pady=(0, 8))
        self.connection_info = self.text(self.setup, "", color=MUTED)
        self.history_audit_info = self.text(self.setup, "", color=MUTED)
        self.tick_info = self.text(self.setup, "", color=MUTED)
        self.equity_info = self.text(self.setup, "", color=MUTED)
        self.option_info = self.text(self.setup, "", color=MUTED)
        self.future_info = self.text(self.setup, "", color=MUTED)
        self.capture_review_info = self.text(self.setup, "", color=MUTED)
        self.rest_info = self.text(self.setup, "", color=MUTED)
        self.paper_info = self.text(self.setup, "", color=MUTED)
        self.event_info = self.text(self.setup, "Bank/news and macro event records: none staged. Manual files remain unverified.", color=MUTED)
        feed_actions = ttk.Frame(self.setup)
        feed_actions.pack(anchor="w", pady=(0, 8))
        ttk.Button(feed_actions, text="Inspect recorded feed quality", command=self.inspect_feed_quality).pack(side="left")
        ttk.Button(feed_actions, text="Export feed quality CSV", command=self.export_feed_quality).pack(side="left", padx=8)
        ttk.Button(feed_actions, text="Daily feed summary", command=self.show_daily_feed_quality).pack(side="left")
        ttk.Button(feed_actions, text="Audit bank/index timing", command=self.show_alignment_audit).pack(side="left", padx=8)
        ttk.Button(feed_actions, text="Export option chain CSV", command=self.export_option_chain).pack(side="left")
        ttk.Button(feed_actions, text="Export futures CSV", command=self.export_futures).pack(side="left", padx=8)
        bank_move_actions = ttk.Frame(self.setup)
        bank_move_actions.pack(anchor="w", pady=(0, 8))
        ttk.Button(bank_move_actions, text="Review aligned bank moves",
                   command=self.show_bank_move_review).pack(side="left")
        ttk.Button(bank_move_actions, text="Export aligned bank moves CSV",
                   command=self.export_bank_move_review).pack(side="left", padx=8)
        ttk.Button(bank_move_actions, text="Review option changes",
                   command=self.show_option_changes).pack(side="left")
        ttk.Button(bank_move_actions, text="Export option changes CSV",
                   command=self.export_option_changes).pack(side="left", padx=8)
        review_actions = ttk.Frame(self.setup)
        review_actions.pack(anchor="w", pady=(0, 8))
        ttk.Button(review_actions, text="Review latest capture", command=self.review_latest_capture).pack(side="left")
        ttk.Button(review_actions, text="Export capture reviews CSV",
                   command=self.export_capture_reviews).pack(side="left", padx=8)
        ttk.Button(review_actions, text="Import bank/macro events CSV",
                   command=self.import_market_events).pack(side="left", padx=8)
        ttk.Button(review_actions, text="Review future changes",
                   command=self.show_future_changes).pack(side="left", padx=8)
        self.text(self.setup,
            "Event CSV columns: kind, subject, title, source_url, published_at, event_at. "
            "Use timezone-aware timestamps and a source link; manual imports remain unverified.",
            color=MUTED, wrap=800)
        connection_actions = ttk.Frame(self.setup)
        connection_actions.pack(anchor="w", pady=(0, 12))
        ttk.Button(connection_actions, text="Export raw feed updates for Excel", command=self.export_feed_ticks).pack(side="left")
        ttk.Button(connection_actions, text="Export bank prices CSV", command=self.export_bank_prices).pack(side="left", padx=8)
        ttk.Button(connection_actions, text="Inspect bank recording", command=self.inspect_bank_recording).pack(side="left")
        ttk.Button(connection_actions, text="Open Groww connection window", command=self.connect).pack(side="left", padx=8)
        rest_actions = ttk.Frame(self.setup)
        rest_actions.pack(anchor="w", pady=(0, 10))
        ttk.Button(rest_actions, text="Review REST price samples", command=self.review_rest_samples).pack(side="left")
        ttk.Button(rest_actions, text="Export REST price samples CSV",
                   command=self.export_rest_samples).pack(side="left", padx=8)
        self.text(self.setup, "Current stage", size=13, bold=True)
        self.text(self.setup, "Fourteen roles are defined: eleven prototypes and three planned. No live prediction is available before feed and model validation.", color=MUTED)
        self.text(self.setup, "Development setting: Sol / Medium. This Python app makes no OpenAI calls. Enter the Groww token only in the masked local window; its field clears after each action, but Python process memory cannot be securely erased.", color=ACCENT)

    def open_project_map(self):
        try:
            target = generate_project_map(Path(__file__).resolve().parent)
            if not webbrowser.open_new_tab(target.as_uri()):
                raise OSError("Browser could not be opened")
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            self.notice.set("Visual project map could not be opened. Check that this project folder is writable and a browser is available.")
            return
        self.notice.set("Visual project map opened with current archive counts and role status.")

    def show_readiness(self):
        report = check_local_readiness(self.data_dir)
        opening = (self.ist(report["next_reviewed_open_ist"]) + " IST"
                   if report["next_reviewed_open_ist"] else "No future session in the reviewed 2026 calendar")
        sdk = (f"Installed ({report['groww_sdk_version'] or 'version unavailable'})"
               if report["groww_sdk_installed"] else "Missing")
        socket_guard = ("Installed socket source reviewed; private-file ACL enforced when capture starts"
                        if report["groww_socket_guard_reviewed"] else
                        "SDK version or socket source not reviewed; live feed capture will be blocked")
        archive = "Writable" if report["archive_writable"] else "Cannot write; check folder access"
        free = (f"{report['disk_free_gb']:,.2f} GB" if report["disk_free_gb"] is not None
                else "Could not measure")
        ticks = (f"{report['recorded_ticks']:,}" if report["recorded_ticks"] is not None
                 else "Could not read")
        lines = ["WHAT IS NEEDED FROM YOU", "",
                 "Right now: no token or login is needed for offline development.",
                 "For the first live trial, during a regular NSE session:",
                 "1. Generate that day's Groww Trading API Access Token in Groww.",
                 "2. Open the Groww connection window in this app and enter it there.",
                 "3. Choose 5 minutes with all optional bank and depth checkboxes off for the first index trial.",
                 "4. Inspect saved exchange times, receipt lag and observed-minute quality. The first and last minutes may be partial.",
                 "5. If index capture works, run a separate short trial with bank prices and volume/waiting orders enabled.",
                 "6. Review 14-bank coverage and bank/index timing, then repeat for 90 minutes if sound.",
                 "7. Test order depth separately after the index and quote paths have passed.",
                 "The token field clears after each action; process memory cannot be securely wiped. Re-enter it for another check or capture.",
                 "The option-chain check is separate and read-only; it also needs the token re-entered.",
                 "Do not paste the token into chat. A prior browser login does not connect the Python feed.",
                 "", "LOCAL CHECKS", "",
                 f"Market status now: {report['market_status']}",
                 f"Next planned regular opening: {opening} (reviewed 2026 calendar)",
                 f"Groww Python feed package: {sdk}",
                 f"Socket credential safeguard: {socket_guard}",
                 f"Local archive: {archive}",
                 f"Free disk space: {free}",
                 f"Saved timestamped Groww feed updates: {ticks}",
                 "", "This screen does not contact Groww or test today's token.",
                 "An open-market feed capture and timestamp check are still required."]
        window = tk.Toplevel(self.root)
        window.title("Bank Nifty · next live step")
        window.geometry("850x650")
        window.transient(self.root)
        viewer = tk.Text(window, wrap="word", font=("Segoe UI", 11), padx=20, pady=16)
        viewer.insert("1.0", "\n".join(lines))
        viewer.config(state="disabled")
        viewer.pack(fill="both", expand=True)

    def request_close(self):
        """Parent closure must also wait for a live REST worker's final record."""
        self._closing = True
        if (self.connection_controller is not None and self.connection_window is not None
                and self.connection_window.winfo_exists()):
            self.connection_controller.request_close(on_closed=self.root.destroy)
        else:
            self.root.destroy()

    def connect(self):
        if self.offline:
            self.notice.set("Offline research mode. Live data requires a separately authorized market-hours check; no connection was opened.")
            return
        if self.connection_window is not None and self.connection_window.winfo_exists():
            self.connection_window.lift()
            return
        self.connection_window = tk.Toplevel(self.root)
        self.connection_window.transient(self.root)
        self.connection_controller = ConnectionWindow(self.connection_window, self.data_dir)
        def close():
            self.connection_window = None
            self.connection_controller = None
            if not self._closing:
                self.refresh()
        self.connection_window.protocol("WM_DELETE_WINDOW",
            lambda: self.connection_controller.request_close(on_closed=close))

    def inspect_feed_quality(self):
        try:
            quality, accepted = inspect_sampled_minutes(load_index_ticks(self.database))
        except (OSError, sqlite3.Error, ValueError):
            self.tick_info.config(text="Recorded feed could not be inspected.")
            return
        if not quality:
            self.tick_info.config(text="No timestamped Groww feed updates saved yet. No sampled minute bars are available.")
            return
        rejected = len(quality) - len(accepted)
        missing = missing_minutes_between_observations(quality)
        last = quality[-1]
        self.tick_info.config(text=f"Observed minutes: {len(quality)} · coverage accepted: {len(accepted)} · rejected: {rejected} · "
                                   f"entirely missing between observations: {missing}. "
                                   f"Latest observed minute: {self.ist(last.end)} IST ({last.status}). "
                                   "Accepted bars are sampled updates, not official exchange candles. "
                                   "Entire missing minutes are never filled.")

    def export_feed_ticks(self):
        if count_index_ticks(self.database) == 0:
            self.tick_info.config(text="No recorded Groww feed updates are available to export yet.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="Export raw Groww feed updates",
                                                    defaultextension=".csv", initialfile="banknifty-feed-updates.csv",
                                                    filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_index_ticks_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Export", "Could not export updates. Choose a writable CSV destination outside the database files.", parent=self.root)
            return
        self.notice.set(f"Exported {count:,} original timestamped Groww updates to {destination}")

    def review_rest_samples(self):
        attempt_note = rest_status_note(self.data_dir)
        try:
            summary = summarize_rest_observations(self.database)
            audit = audit_rest_receipt_integrity(self.database)
            sessions = summarize_rest_session_coverage(self.database)
        except (OSError, sqlite3.Error, ValueError):
            self.rest_info.config(text=attempt_note + " REST price samples could not be reviewed; inspect the archive.")
            return
        if summary["count"] == 0:
            self.rest_info.config(text=attempt_note + " No Groww REST price samples have been saved yet.")
            return
        latest = sessions[-1] if sessions else None
        coverage_note = (f" Latest regular session {latest['date_ist']}: "
                         f"{latest['minutes_with_receipts']}/375 minutes have a receipt, "
                         f"{latest['four_quadrant_minutes']} have receipts in all four 15-second quarters. "
                         if latest else " No regular-session coverage found. ")
        self.rest_info.config(text=(
            attempt_note + f" REST price samples: {summary['count']:,}; laptop receipt times "
            f"{summary['first']} to {summary['last']}; observed sample range "
            f"{summary['observed_low']:,.2f}–{summary['observed_high']:,.2f}; "
            f"{summary['repeated_prices']} repeated prices; "
            f"{audit['within_day_gaps_over_15_seconds']} within-day gaps over 15 seconds; "
            f"{audit['clock_regressions']} clock regressions; "
            f"{audit['outside_regular_session']} receipts outside regular hours; "
            f"{audit['large_move_review_flags']} adjacent moves above 1% requiring manual review. "
            + coverage_note +
            "Exchange freshness is unverified; these are not official candles or forecast evidence."))

    def export_rest_samples(self):
        try:
            available = summarize_rest_observations(self.database)["count"]
        except (OSError, sqlite3.Error, ValueError):
            self.rest_info.config(text="REST price samples could not be reviewed; no export was created.")
            return
        if not available:
            self.rest_info.config(text="No REST price samples have been saved yet; no export was created.")
            return
        destination = filedialog.asksaveasfilename(
            parent=self.root, title="Export Groww REST price samples", defaultextension=".csv",
            initialfile="banknifty-rest-price-samples.csv", filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_rest_observations_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Export", "Could not export REST samples. Choose another CSV destination outside the database.", parent=self.root)
            return
        self.notice.set(f"Exported {count:,} receipt-time REST samples to {destination}; exchange freshness remains unverified.")

    def export_bank_prices(self):
        if count_equity_ticks(self.database) == 0:
            self.equity_info.config(text="No timestamped bank-equity prices have been recorded yet.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="Export raw bank-equity prices",
                                                    defaultextension=".csv", initialfile="bank-equity-updates.csv",
                                                    filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_equity_ticks_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Export", "Could not export bank prices. Choose a writable CSV destination outside the database files.", parent=self.root)
            return
        self.notice.set(f"Exported {count:,} raw bank-equity prices to {destination}; timestamps remain separate from index updates.")

    def export_option_chain(self):
        try:
            available = count_option_chain_snapshots(self.database)
        except (OSError, sqlite3.Error, ValueError):
            self.option_info.config(text="Saved option-chain snapshots could not be read.")
            return
        if not available:
            self.option_info.config(text="No real Groww option-chain snapshots are saved yet. The invented workbench example is not archived.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="Export saved option-chain contracts",
                                                    defaultextension=".csv", initialfile="banknifty-option-chain.csv",
                                                    filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_option_chain_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            messagebox.showerror("Export", "Could not export option-chain records. Choose a writable CSV destination outside the database files.", parent=self.root)
            return
        self.notice.set(f"Exported {count:,} option contracts across {available} saved snapshots to {destination}. Exchange timestamp is unavailable.")

    def show_option_changes(self):
        try:
            review = review_latest_saved_option_pair(self.database)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            self.option_info.config(text="Saved option-chain changes could not be reviewed.")
            return
        if review["comparison"] is None:
            self.option_info.config(text=f"Option changes unavailable: {review['status']}. "
                                         "Two recent same-expiry observations are needed.")
            return
        result = review["comparison"]
        totals = (f"CE OI {result['call_oi_change_reported']:+,}; PE OI "
                  f"{result['put_oi_change_reported']:+,}; CE reported volume "
                  f"{result['call_volume_change_reported']:+,}; PE reported volume "
                  f"{result['put_volume_change_reported']:+,}"
                  if result["status"] == "comparable_contract_universe" else
                  "Whole-chain totals unavailable because the contract universe changed or is partial")
        self.option_info.config(text=f"Saved {result['expiry_date']} expiry: "
                                     f"{result['common_contracts']} common contracts, "
                                     f"{result['receipt_gap_seconds'] / 60:.1f}-minute receipt gap. "
                                     f"{totals}. Exchange freshness and trade direction unverified.")

    def export_option_changes(self):
        try:
            review = review_latest_saved_option_pair(self.database)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            self.option_info.config(text="Saved option-chain changes could not be reviewed.")
            return
        if review["comparison"] is None:
            self.option_info.config(text="No recent same-expiry option pair is available to export.")
            return
        destination = filedialog.asksaveasfilename(
            parent=self.root, title="Export reported option changes",
            defaultextension=".csv", initialfile="banknifty-option-changes.csv",
            filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_latest_option_changes_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            messagebox.showerror("Export", "Could not export option changes. Choose a writable CSV destination outside the market archive.", parent=self.root)
            return
        self.notice.set(f"Exported {count} option-contract comparisons. Exchange freshness and trade direction remain unverified.")

    def export_futures(self):
        try:
            available = len(load_future_snapshots(self.database))
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            self.future_info.config(text="Saved futures quotes could not be read.")
            return
        if not available:
            self.future_info.config(text="No Bank Nifty futures quotes have been saved yet.")
            return
        destination = filedialog.asksaveasfilename(
            parent=self.root, title="Export Bank Nifty futures quotes",
            defaultextension=".csv", initialfile="banknifty-futures-quotes.csv",
            filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_future_snapshots_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            messagebox.showerror("Export", "Could not export futures quotes. Choose a writable CSV destination outside the database.", parent=self.root)
            return
        self.notice.set(f"Exported {count:,} futures quote(s) to {destination}. Last-trade time does not establish snapshot freshness.")

    def show_future_changes(self):
        try:
            review = review_latest_saved_future_pair(self.database)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            self.future_info.config(text="Saved futures quotes could not be reviewed.")
            return
        result = review["comparison"]
        if result is None:
            self.future_info.config(text=f"Futures comparison unavailable: {review['status']}. "
                                         "A recent same-contract pair with distinct, recent trade times is required.")
            return
        self.future_info.config(text=(
            f"{result['trading_symbol']}: price {result['price_change_points']:+,.2f} points; "
            f"reported OI {result['open_interest_change_reported']:+,}; "
            f"{result['descriptive_quadrant'].replace('_', ' ')} over "
            f"{result['receipt_gap_seconds'] / 60:.1f} minutes. Last-trade ages "
            f"{result['previous_trade_age_seconds']:.1f}s and "
            f"{result['current_trade_age_seconds']:.1f}s. OI exchange time and trade side "
            "unverified; no forecast."))

    def review_latest_capture(self):
        try:
            saved = load_capture_trial_summaries(self.database)
            message = (format_capture_trial_summary(saved[-1]) if saved else
                       "No capture review has been saved yet. The next trial will create one if the archive is writable.")
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            message = "Saved capture reviews could not be read; inspect the archive before trusting a result."
        self.capture_review_info.config(text=message)

    def export_capture_reviews(self):
        try:
            available = len(load_capture_trial_summaries(self.database))
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            self.capture_review_info.config(text="Capture reviews could not be read; no export was created.")
            return
        if not available:
            self.capture_review_info.config(text="No capture reviews have been saved yet; no export was created.")
            return
        destination = filedialog.asksaveasfilename(
            parent=self.root, title="Export capture reviews", defaultextension=".csv",
            initialfile="banknifty-capture-reviews.csv", filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_capture_trial_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            messagebox.showerror("Export", "Could not export capture reviews. Choose a writable CSV destination outside the database.", parent=self.root)
            return
        self.notice.set(f"Exported {count:,} capture review(s) to {destination}. These are local quality checks, not forecast results.")

    def inspect_bank_recording(self):
        try:
            report = load_reference_weights(Path(__file__).resolve().parent /
                                            "reference" / "nse-bank-weights-2026-09-30.json")
            symbols = ["NSE-" + row["symbol"] for row in report["constituents"]]
            coverage = equity_coverage(self.database, symbols)
            index_count = count_index_ticks(self.database)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            self.equity_info.config(text="Bank recording could not be inspected; existing data was not changed.")
            return
        lines = ["RAW BANK FEED COVERAGE", "",
                 f"Bank list: NSE report dated {report['as_of_date']} (reference only)",
                 f"Saved index updates: {index_count:,}",
                 f"Saved bank updates: {sum(row['count'] for row in coverage.values()):,}",
                 f"Banks with any saved update: {sum(row['count'] > 0 for row in coverage.values())}/14",
                 "", "Bank                   Updates    First exchange time (UTC)       Last exchange time (UTC)"]
        for symbol, row in coverage.items():
            first = row["first_at_utc"] or "—"
            last = row["last_at_utc"] or "—"
            lines.append(f"{symbol:<22} {row['count']:>7}    {first:<30} {last}")
        lines.extend(("", "Counts and time spans do not prove complete market coverage,",
                      "same-time index/stock alignment, or live index weights.",
                      "No real constituent contribution is calculated here."))
        window = tk.Toplevel(self.root)
        window.title("Bank Nifty | raw bank recording")
        window.geometry("1100x650")
        window.transient(self.root)
        viewer = tk.Text(window, wrap="none", font=("Consolas", 10), padx=15, pady=15)
        viewer.insert("1.0", "\n".join(lines))
        viewer.config(state="disabled")
        viewer.pack(fill="both", expand=True)

    def show_alignment_audit(self):
        try:
            report = audit_latest_recorded_session(
                self.database, Path(__file__).resolve().parent /
                "reference" / "nse-bank-weights-2026-09-30.json")
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            self.equity_info.config(text="Bank/index timing could not be audited. Existing data was not changed.")
            return
        if report["status"] == "no_index_data":
            self.equity_info.config(text="Timing audit unavailable: no timestamped Bank Nifty index updates have been recorded.")
            return
        lines = ["BANK / INDEX TIMING AUDIT · DIAGNOSTIC ONLY", "",
                 f"Latest recorded session: {report['session_date']}",
                 f"Dated NSE symbol list: {report['reference_as_of_date']} (not a live weight snapshot)",
                 f"Observed index minutes: {report['observed_index_minutes']} / "
                 f"{report['expected_regular_minutes']} expected regular-session minutes",
                 f"Minutes with all 14 banks available: {report['all_banks_fresh_minutes']}",
                 f"Trial bank quote age limit: {report['trial_max_lag_seconds']} seconds",
                 "", "Bank                    Fresh   Stale   Late receipt   No prior quote"]
        for symbol, counts in report["per_bank"].items():
            lines.append(f"{symbol:<22} {counts['fresh']:>5}   {counts['stale']:>5}   "
                         f"{counts['not_received_yet']:>12}   {counts['no_prior_quote']:>14}")
        lines.extend(("", "Only the last observed index update in each minute is an anchor.",
                      "A bank quote must be no later than that exchange time and already",
                      "received by the laptop at the index update's receipt time.",
                      "This trial age limit has not been calibrated on a real session.",
                      "Passing this timing audit would not validate the data source,",
                      "index weights, a contribution estimate, or a forecast."))
        window = tk.Toplevel(self.root)
        window.title("Bank Nifty | bank/index timing audit")
        window.geometry("1000x650")
        window.transient(self.root)
        viewer = tk.Text(window, wrap="none", font=("Consolas", 10), padx=15, pady=15)
        viewer.insert("1.0", "\n".join(lines))
        viewer.config(state="disabled")
        viewer.pack(fill="both", expand=True)

    def show_bank_move_review(self):
        reference = Path(__file__).resolve().parent / "reference" / "nse-bank-weights-2026-09-30.json"
        try:
            report = review_latest_recorded_bank_moves(self.database, reference)
        except (OSError, sqlite3.Error, ValueError):
            self.equity_info.config(text="Aligned bank movement unavailable: local dated weights are missing or the archive could not be reviewed. No forecast was produced.")
            return
        if not report["results"]:
            self.equity_info.config(text="Aligned 15/30/60-minute bank movement unavailable: "
                "no complete accepted index origin and targets are recorded yet.")
            return
        lines = ["RECORDED BANK MOVEMENT · RETROSPECTIVE, UNWEIGHTED, NO FORECAST", "",
                 f"Session: {report['session_date_ist']} · dated bank list: {report['bank_list_as_of_date']}",
                 f"Accepted index minutes: {report['accepted_minutes']}; "
                 f"late index updates excluded: {report['excluded_late_index_updates']}",
                 "Bank quotes must be within five seconds of each index endpoint and received by its cutoff.", ""]
        for item in report["results"]:
            lines.append(f"{item['horizon_minutes']}m: index {item['index_move_points']:+.2f} points "
                         f"({item['index_return_pct']:+.3f}%); "
                         f"aligned banks {item['banks_with_aligned_pairs']}/{item['expected_banks']} "
                         f"· {item['status']}")
            for bank in item["bank_rows"]:
                lines.append(f"  {bank['symbol']:<20} {bank['change_pct']:+.3f}%")
            if item["missing_banks"]:
                lines.append("  Missing endpoint pairs: " + ", ".join(
                    bank["symbol"] for bank in item["missing_banks"]))
            else:
                lines.append(f"  Equal-weight bank average {item['unweighted_mean_bank_return_pct']:+.3f}% "
                             f"· advancing {item['advancing_banks']} · declining {item['declining_banks']}")
            lines.append("")
        lines.append("Dated weights were not used. These bank returns are not index contributions or a signal.")
        window = tk.Toplevel(self.root)
        window.title("Bank Nifty | aligned saved bank moves")
        window.geometry("1000x650")
        viewer = tk.Text(window, wrap="none", font=("Consolas", 10), padx=15, pady=15)
        viewer.insert("1.0", "\n".join(lines))
        viewer.config(state="disabled")
        viewer.pack(fill="both", expand=True)

    def export_bank_move_review(self):
        reference = Path(__file__).resolve().parent / "reference" / "nse-bank-weights-2026-09-30.json"
        try:
            review = review_latest_recorded_bank_moves(self.database, reference)
        except (OSError, sqlite3.Error, ValueError):
            self.notice.set("Aligned bank movement could not be verified; no bank-movement CSV was created.")
            return
        if not review["results"]:
            self.notice.set("No completed aligned index intervals exist; no bank-movement CSV was created.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root,
            title="Export aligned observed bank moves", defaultextension=".csv",
            initialfile="banknifty-aligned-bank-moves.csv", filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_latest_bank_moves_csv(self.database, reference, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Bank movement", "Could not save bank movement CSV.", parent=self.root)
            return
        self.notice.set(f"Exported {count} bank endpoint rows. Missing banks remain blank; "
                        "dated weights were not applied.")

    def export_feed_quality(self):
        if count_index_ticks(self.database) == 0:
            self.tick_info.config(text="No recorded Groww feed updates are available for a quality report yet.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="Export Groww feed quality",
                                                    defaultextension=".csv", initialfile="banknifty-feed-quality.csv",
                                                    filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            result = export_feed_quality_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Feed quality", "Could not save the feed quality CSV. The market archive was not changed.", parent=self.root)
            return
        self.notice.set(f"Exported {result['rows']:,} expected session minutes across "
                        f"{result['session_dates']} recorded dates. Accepted: {result['accepted_minutes']:,}; "
                        "missing and weak minutes stay visible. This is not official OHLC.")

    def show_daily_feed_quality(self):
        try:
            days = summarize_recorded_feed(self.database)
        except (OSError, sqlite3.Error, ValueError):
            self.tick_info.config(text="Daily feed quality could not be read. Existing data was not changed.")
            return
        if not days:
            self.tick_info.config(text="No regular-session Groww updates recorded yet. There is no daily quality result.")
            return
        lines = ["RECORDED FEED · DAILY QUALITY", "",
                 "Accepted bars use sampled updates; they are not official exchange candles.",
                 "Missing means no update was saved for that expected session minute.", ""]
        for row in days:
            lines.extend((
                f"{row['date_ist']} · {row['accepted_minutes']}/{row['expected_minutes']} minutes accepted",
                f"  Observed {row['observed_minutes']} · weak {row['rejected_minutes']} · "
                f"entirely missing {row['missing_whole_minutes']}",
                f"  First observed {self.ist(row['first_observed_start_ist'])} IST · "
                f"last observed {self.ist(row['last_observed_end_ist'])} IST", "",
            ))
        window = tk.Toplevel(self.root)
        window.title("Bank Nifty · daily feed quality")
        window.geometry("900x520")
        window.transient(self.root)
        viewer = tk.Text(window, wrap="word", font=("Segoe UI", 11), padx=18, pady=15)
        viewer.insert("1.0", "\n".join(lines))
        viewer.config(state="disabled")
        viewer.pack(fill="both", expand=True)

    def refresh(self):
        self.refresh_activity()
        self.refresh_depth()
        status = read_connection_status(self.data_dir)
        self.summary_labels["GROWW ACCESS"].config(text=status["title"])
        prototypes = sum(row[1] == "Prototype" for row in AGENTS)
        planned = sum(row[1] == "Planned" for row in AGENTS)
        self.summary_labels["AGENT READINESS"].config(text=f"{prototypes} prototypes / {planned} planned")
        stamp = f"Last check: {status['checked_at']}. " if status["checked_at"] else ""
        self.connection_info.config(text=stamp + status["detail"])
        self.history_audit_info.config(text=read_history_audit(self.data_dir))
        try:
            tick_count = count_index_ticks(self.database)
            self.tick_info.config(text=f"Timestamped Groww feed updates saved: {tick_count:,} (separate from candles).")
        except (OSError, sqlite3.Error):
            self.tick_info.config(text="Groww feed update count could not be read.")
        try:
            banks = equity_symbol_counts(self.database)
            self.equity_info.config(text=f"Timestamped bank-equity prices saved: {sum(banks.values()):,} across {len(banks)} banks. Raw observations; alignment unverified.")
        except (OSError, sqlite3.Error):
            self.equity_info.config(text="Bank-equity price count could not be read.")
        try:
            option_count = count_option_chain_snapshots(self.database)
            self.option_info.config(text=f"Saved Groww option-chain snapshots: {option_count:,}. Receipt time only; exchange freshness unverified.")
        except (OSError, sqlite3.Error, ValueError):
            self.option_info.config(text="Option-chain snapshot count could not be read.")
        try:
            futures = load_future_snapshots(self.database)
            real_count = sum(row["source"] == "Groww" for row in futures)
            self.future_info.config(text=f"Saved Groww Bank Nifty futures quotes: {real_count:,}. Single snapshots; freshness and predictive value unverified.")
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            self.future_info.config(text="Futures quote count could not be read.")
        self.review_latest_capture()
        try:
            paper_count = count_paper_assessments(self.database)
            self.paper_info.config(text=f"Experimental paper assessments fixed before outcomes: {paper_count:,}.")
        except (OSError, sqlite3.Error):
            self.paper_info.config(text="Paper assessment count could not be read.")
        try:
            datasets = list_datasets(self.database)
        except (OSError, sqlite3.Error, ValueError):
            self.summary_labels["SAVED CANDLES"].config(text="Read error")
            self.export_button.config(state="disabled")
            self.notice.set("Saved history could not be read. Check local folder access; existing records have not been changed.")
            return
        selected = self.dataset_table.selection()
        selected_name = self.dataset_table.item(selected[0], "values")[0] if selected else None
        self.dataset_table.delete(*self.dataset_table.get_children())
        count = sum(row["count"] for row in datasets)
        demo_count = sum(row["count"] for row in datasets if row["source"] == "synthetic")
        self.summary_labels["SAVED CANDLES"].config(text=f"{count:,} total · {demo_count:,} demo")
        for index, row in enumerate(datasets):
            self.dataset_table.insert("", "end", iid=str(index), values=(row["dataset"], row["source"], row["count"], self.ist(row["last_end"])))
            if row["dataset"] == selected_name:
                self.dataset_table.selection_set(str(index))
        if datasets and not self.dataset_table.selection():
            self.dataset_table.selection_set("0")
        if not datasets:
            self.candles = []
            self.candle_table.delete(*self.candle_table.get_children())
            self.draw_chart()
        self.export_button.config(state="normal" if datasets else "disabled")
        size = self.database.stat().st_size / 1024 if self.database.exists() else 0
        self.storage_info.config(text=f"Local database: {size:,.1f} KB  ·  No automatic deletion  ·  CSV opens in Excel\n{self.database}")
        if datasets:
            self.select_dataset()

    @staticmethod
    def ist(value):
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
        return stamp.astimezone(IST).strftime("%d %b %Y %H:%M")

    def load_demo(self):
        _, candles = synthetic_session()
        try:
            result = save_candles(self.database, candles, dataset=DEMO_DATASET, as_of=candles[-1].end)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Sample history", "Could not save the sample. Existing history has not been replaced.", parent=self.root)
            return
        self.refresh()
        for item in self.dataset_table.get_children():
            if self.dataset_table.item(item, "values")[0] == DEMO_DATASET:
                self.dataset_table.selection_set(item)
                break
        self.select_dataset()
        self.notice.set(f"Synthetic sample: {result['inserted']} candles added; {result['duplicates']} already saved. These are invented prices.")

    def select_dataset(self, event=None):
        selection = self.dataset_table.selection()
        if not selection:
            return
        dataset = self.dataset_table.item(selection[0], "values")[0]
        try:
            self.candles = load_candles(self.database, dataset=dataset)
        except (OSError, sqlite3.Error, ValueError):
            self.candles = []
            self.notice.set("Selected history could not be read. No records were changed.")
        self.candle_table.delete(*self.candle_table.get_children())
        for candle in reversed(self.candles[-300:]):
            self.candle_table.insert("", "end", values=(self.ist(candle.end), *(f"{getattr(candle, key):,.2f}" for key in ("open", "high", "low", "close"))))
        source = self.candles[0].source if self.candles else "unavailable"
        kind = "SYNTHETIC SAMPLE · invented prices" if source == "synthetic" else "SAVED HISTORY · not a live feed"
        self.chart_caption.config(text=f"{kind}  |  {dataset}")
        self.draw_chart()

    def draw_chart(self):
        self.chart.delete("all")
        width, height = max(self.chart.winfo_width(), 400), max(self.chart.winfo_height(), 70)
        if not self.candles:
            self.pattern_caption.config(text="Patterns: unavailable until continuous candles are loaded")
            self.chart.create_text(width / 2, height / 2, text="No history loaded\nUse Load sample history to explore the workspace", fill=MUTED, font=("Segoe UI", 12), justify="center")
            return
        candle_mode = self.chart_mode.get() == "Candles"
        minutes = int(self.chart_interval.get().split()[0])
        try:
            chart_rows = completed_chart_bars(self.candles, minutes)
        except ValueError:
            chart_rows = []
        if not chart_rows:
            self.chart.create_text(width / 2, height / 2,
                text=f"No complete {minutes}-minute bars in this saved history", fill=MUTED)
            self.pattern_caption.config(text="Patterns unavailable without complete source candles.")
            return
        candles = chart_rows[-120:] if candle_mode else chart_rows[-600:]
        low = min(c.low if candle_mode else c.close for c in candles)
        high = max(c.high if candle_mode else c.close for c in candles)
        spread = max(high - low, 1.0)
        left, right, top, bottom = 80, width - 25, 18, height - 34
        for line in range(4):
            y = top + (bottom - top) * line / 3
            self.chart.create_line(left, y, right, y, fill="#e9eef3")
            self.chart.create_text(left - 10, y, text=f"{high - spread * line / 3:,.0f}", anchor="e", fill=MUTED, font=("Segoe UI", 9))
        points = []
        previous = None
        for index, candle in enumerate(candles):
            x = left + (right - left) * index / max(len(candles) - 1, 1)
            y = bottom - (candle.close - low) / spread * (bottom - top)
            if candle_mode:
                y_high = bottom - (candle.high - low) / spread * (bottom - top)
                y_low = bottom - (candle.low - low) / spread * (bottom - top)
                color = "#087e83" if candle.close >= candle.open else "#c05b51"
                half = max(1.5, min(4, (right - left) / max(len(candles), 1) * .34))
                self.chart.create_line(x, y_high, x, y_low, fill=color)
                y_open = bottom - (candle.open - low) / spread * (bottom - top)
                self.chart.create_rectangle(x - half, min(y_open, y), x + half,
                                            max(y_open, y) + (1 if y_open == y else 0),
                                            fill=color, outline=color)
            else:
                if previous is not None and candle.start != previous.end:
                    if len(points) >= 4:
                        self.chart.create_line(*points, fill=ACCENT, width=2)
                    points = []
                points.extend((x, y))
                self.chart.create_oval(x - 1.5, y - 1.5, x + 1.5, y + 1.5, fill=ACCENT, outline="")
            previous = candle
        if not candle_mode and len(points) >= 4:
            self.chart.create_line(*points, fill=ACCENT, width=2)
        try:
            report = analyze_candle_patterns(chart_rows[-3:], chart_rows[-1].end,
                                             expected_minutes=minutes)
            matched = [name.replace("_", " ") for name, found in report["patterns"].items() if found]
            self.pattern_caption.config(text=f"Latest completed {minutes}-minute candle patterns: " +
                (", ".join(matched) if matched else "none under current definitions") +
                ". Descriptive only; sampled OHLC may differ from exchange candles.")
        except ValueError:
            self.pattern_caption.config(text="Patterns unavailable: last three saved candles are not continuous.")
        self.chart.create_text(left, height - 12, text=self.ist(candles[0].end) + " IST", anchor="w", fill=MUTED, font=("Segoe UI", 9))
        self.chart.create_text(right, height - 12, text=self.ist(candles[-1].end) + " IST", anchor="e", fill=MUTED, font=("Segoe UI", 9))

    def export_selected(self):
        selection = self.dataset_table.selection()
        if not selection:
            return
        dataset = self.dataset_table.item(selection[0], "values")[0]
        destination = filedialog.asksaveasfilename(parent=self.root, title="Export history for Excel", defaultextension=".csv", initialfile="banknifty-history.csv", filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_csv(self.database, destination, dataset=dataset)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Export", "Could not export. Choose a writable CSV destination outside the database files.", parent=self.root)
            return
        self.notice.set(f"Exported {count:,} saved candles to {destination}")

    def backup_history(self):
        if not self.database.exists():
            self.notice.set("There is no market database to back up yet.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="Save a new market database backup",
                                                    defaultextension=".sqlite3", initialfile="banknifty-backup.sqlite3",
                                                    filetypes=[("SQLite database", "*.sqlite3")])
        if not destination:
            return
        try:
            size = backup_database(self.database, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Backup", "Could not create a new database backup. Existing files were not changed.", parent=self.root)
            return
        self.notice.set(f"Backed up saved candles, index/bank feed updates and audit events: {size / 1024:,.1f} KB at {destination}")

    def backup_whole_project(self):
        project = Path(__file__).resolve().parent
        if not self.database.exists():
            self.notice.set("There is no market database to include in a whole-project backup yet.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root,
            title="Save a new whole-project backup outside the project folder",
            initialdir=str(project.parent),
            initialfile=f"banknifty-project-backup-{date.today().isoformat()}.zip",
            defaultextension=".zip", filetypes=[("ZIP archive", "*.zip")])
        if not destination:
            return
        try:
            result = backup_project(project, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Backup", "Could not verify the project backup. Choose a new ZIP outside the project folder; existing files were not changed.", parent=self.root)
            return
        self.notice.set(f"Verified whole-project backup: {result['files']} files, "
                        f"{result['bytes'] / 1024:,.1f} KB. Keep a second copy on another drive.")

    def verify_whole_project_backup(self):
        source = filedialog.askopenfilename(parent=self.root,
            title="Verify a Bank Nifty project backup ZIP",
            filetypes=[("ZIP archive", "*.zip")])
        if not source:
            return
        try:
            result = rehearse_project_restore(source)
        except (OSError, sqlite3.Error, BadZipFile, ValueError):
            messagebox.showerror("Restore rehearsal",
                "This ZIP did not pass the temporary restore check. The current project was not changed.",
                parent=self.root)
            return
        self.notice.set(f"Restore rehearsal passed: {result['files']} files, "
                        f"project version {result['project_version']}, SQLite integrity OK. "
                        "This checks readability, not the original data source's truth.")

    def export_daily_research(self):
        reference = Path(__file__).resolve().parent / "reference" / "nse-bank-weights-2026-09-30.json"
        try:
            available = build_session_diagnostics(self.database, reference)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            self.notice.set("Daily research coverage could not be checked. Existing data was not changed.")
            return
        if not available:
            self.notice.set("No regular-session index feed updates have been saved for a daily research report.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="Export daily research coverage",
                                                    defaultextension=".csv", initialfile="banknifty-daily-research.csv",
                                                    filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_session_diagnostics_csv(self.database, reference, destination)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            messagebox.showerror("Export", "Could not export daily research coverage. Choose a writable CSV destination outside the database files.", parent=self.root)
            return
        self.notice.set(f"Exported {count} recorded-session coverage rows to {destination}. This report contains no predictions.")

    def match_paper_outcomes(self):
        try:
            result = resolve_paper_outcomes(self.database)
            summary = paper_outcome_summary(self.database)
            total = count_paper_assessments(self.database)
        except (OSError, sqlite3.Error, ValueError):
            self.notice.set("Paper outcomes could not be checked. Existing assessments and outcomes were not changed.")
            return
        labelled = sum(row["labelled"] for row in summary.values())
        self.paper_info.config(text=f"Experimental paper assessments: {total:,}. "
                                    f"Later outcomes with complete data: {labelled:,}; missing data: "
                                    f"{sum(row['resolved'] - row['labelled'] for row in summary.values()):,}.")
        self.notice.set(f"Paper outcome check: {result['resolved']} newly matched, {result['pending']} await later data, "
                        f"{result['missing_data']} missing required minutes. No accuracy claim or trade.")

    def export_paper_report(self):
        try:
            available = count_paper_assessments(self.database)
        except (OSError, sqlite3.Error):
            self.notice.set("Paper assessment records could not be read for export.")
            return
        if available == 0:
            self.notice.set("There are no recorded Groww paper assessments to export yet.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="Export experimental paper report",
                                                    defaultextension=".csv", initialfile="banknifty-paper-report.csv",
                                                    filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_paper_report_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Paper report", "Could not export the paper report. Existing records were not changed.", parent=self.root)
            return
        self.notice.set(f"Exported {count:,} experimental paper assessments to {destination}. Scores are not probabilities.")

    def check_recorded_history(self):
        try:
            report = evaluate_recorded_database(self.database)
        except (OSError, sqlite3.Error, ValueError):
            self.notice.set("Recorded history could not be checked. No archive records were changed.")
            return
        if report["split_status"] != "ready":
            self.notice.set(f"Recorded feed: {report['observed_session_dates']} observed dates; "
                            f"{report['analysis_ready_session_dates']} have 61 continuous prior minutes. "
                            "At least 5 analysis-ready dates are needed before a chronological split is shown. "
                            "No historical performance result is available yet.")
            return
        lines = ["EXPERIMENTAL RECORDED-HISTORY CHECK", "",
                 "Source: observed Groww index updates; sampled bars are not official OHLC.",
                 "Rules are fixed and uncalibrated. Results do not prove predictive value.",
                 "Whole sessions are assigned chronologically; selected origins are 60 minutes apart.",
                 f"Late or clock-invalid updates excluded: {report['excluded_late_or_clock_invalid_ticks']}.",
                 "Only sampled prices within five seconds of each minute end count. Missing labels reduce coverage.",
                 "Wilson 95% reference intervals assume independent calls; market-day dependence can make them too narrow.",
                 "Mean day hit rate gives each session equal weight; it is descriptive, not an uncertainty bound.",
                 "Matched comparisons use exactly the origins where both Technical and the baseline made calls.",
                 "Small holdouts and these intervals cannot establish forecast accuracy.", ""]
        for name, item in report["partitions"].items():
            lines.append(f"{name.upper()}: {item['dates'][0]} to {item['dates'][-1]} ({len(item['dates'])} sessions)")
            for horizon, row in item["horizons"].items():
                def rate(method):
                    value = row[f"{method}_hit_rate"]
                    calls = row[f"{method}_calls"]
                    if value is None:
                        return "no calls"
                    lower = row[f"{method}_wilson95_lower"]
                    upper = row[f"{method}_wilson95_upper"]
                    day_count = row[f"{method}_days_with_calls"]
                    day_mean = row[f"{method}_mean_day_hit_rate"]
                    return (f"{value:.1%} of {calls} (95% reference {lower:.0%}–{upper:.0%}; "
                            f"mean day {day_mean:.1%} across {day_count} day(s))")
                lines.append(f"  {horizon} min · near-end sampled outcomes {row['near_end_labels']}/{row['eligible_origins']} · "
                             f"Technical {rate('technical')} · prior momentum {rate('momentum')} · "
                             f"always up {rate('always_up')}")
                for baseline, label in (("always_up", "always up"),
                                        ("momentum", "prior momentum")):
                    prefix = f"technical_vs_{baseline}"
                    calls = row[f"{prefix}_matched_calls"]
                    if calls:
                        lines.append(
                            f"    Matched Technical vs {label}: {calls} same calls across "
                            f"{row[f'{prefix}_days_with_matched_calls']} day(s); "
                            f"{row[f'{prefix}_matched_technical_hit_rate']:.1%} vs "
                            f"{row[f'{prefix}_matched_baseline_hit_rate']:.1%}; "
                            f"difference {row[f'{prefix}_matched_difference'] * 100:+.1f} pp")
                    else:
                        lines.append(f"    Matched Technical vs {label}: no shared calls")
            lines.append("")
        window = tk.Toplevel(self.root)
        window.title("Recorded history · experimental rule check")
        window.geometry("950x510")
        window.transient(self.root)
        viewer = tk.Text(window, wrap="word", font=("Consolas", 10), padx=14, pady=14)
        viewer.insert("1.0", "\n".join(lines))
        viewer.config(state="disabled")
        viewer.pack(fill="both", expand=True)
        self.notice.set("Recorded history check complete. This is an offline comparison, not a validated forecast.")

    def export_recorded_comparison(self):
        try:
            report = evaluate_recorded_database(self.database)
        except (OSError, sqlite3.Error, ValueError):
            self.notice.set("Recorded-feed comparison could not be calculated. No files were changed.")
            return
        if report["split_status"] != "ready":
            self.notice.set(f"Comparison export needs five dates with continuous prior minutes; "
                            f"only {report['analysis_ready_session_dates']} are available. No CSV was created.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="Export recorded-history comparison",
                                                    defaultextension=".csv", initialfile="banknifty-history-comparison.csv",
                                                    filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = export_recorded_comparison_csv(self.database, destination, report=report)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("History comparison", "Could not save the comparison CSV. The market archive was not changed.", parent=self.root)
            return
        self.notice.set(f"Exported {count} experimental comparison rows to {destination}. This is not a validated forecast.")

    def export_intraday_research(self):
        try:
            report = build_training_research_rows(load_index_ticks(self.database),
                expiry_references=load_expiry_references(self.database),
                events=load_known_events(self.database, datetime.now(timezone.utc)))
        except (OSError, sqlite3.Error, ValueError):
            self.notice.set("Intraday research rows could not be verified; no CSV was created.")
            return
        if not report["rows"]:
            self.notice.set("No complete point-in-time Groww intraday history is saved; no research CSV was created.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root,
            title="Export point-in-time Bank Nifty research rows",
            defaultextension=".csv", initialfile="banknifty-intraday-research.csv",
            filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            exported = export_training_research_csv(self.database, destination)
        except (OSError, sqlite3.Error, ValueError):
            messagebox.showerror("Research export", "Could not verify or save the research CSV. The market archive was not changed.", parent=self.root)
            return
        self.notice.set(f"Exported {len(exported['rows'])} research rows across "
                        f"{exported['analysis_ready_session_dates']} dates. "
                        f"{exported['weak_target_labels']} weak-endpoint labels stay blank. "
                        "Prices are sampled near minute ends; no model was trained.")

    def review_expiry_day_movement(self):
        try:
            report = build_training_research_rows(load_index_ticks(self.database),
                expiry_references=load_expiry_references(self.database),
                events=load_known_events(self.database, datetime.now(timezone.utc)))
            review = review_expiry_regimes(report)
        except (OSError, sqlite3.Error, ValueError):
            self.notice.set("Expiry-day research could not be checked. No forecast was produced.")
            return
        if not review["summary"]:
            self.notice.set("No labelled, expiry-classified index history is available yet. "
                            "Record and verify market sessions first.")
            return
        window = tk.Toplevel(self.root)
        window.title("Expiry-day movement · descriptive review")
        window.geometry("850x480")
        viewer = tk.Text(window, wrap="word", font=("Consolas", 10), padx=14, pady=14)
        lines = ["Saved Groww sampled-index outcomes only. This is not a forecast or expiry effect.",
                 f"Unknown expiry rows: {review['unknown_expiry_rows']}; "
                 f"unavailable outcome rows: {review['unavailable_label_rows']}.", ""]
        for item in review["summary"]:
            lines.append(f"{item['partition']} · {item['horizon_minutes']}m · {item['regime']}: "
                         f"{item['session_days']} day(s), {item['ready_observations']} observations; "
                         f"mean absolute move {item['mean_absolute_move_pct']:.3f}%, "
                         f"up fraction {item['up_fraction']:.1%}; {item['evidence_status']}.")
        lines.append("\nDifferent time-of-day, events and market regimes can explain differences. "
                     "No trading or accuracy claim follows from this summary.")
        viewer.insert("1.0", "\n".join(lines))
        viewer.config(state="disabled")
        viewer.pack(fill="both", expand=True)

    def import_market_events(self):
        source = filedialog.askopenfilename(parent=self.root,
            title="Choose a reviewed bank or macro event CSV", filetypes=[("CSV files", "*.csv")])
        if not source:
            return
        try:
            result = stage_event_csv(self.database, source)
        except (OSError, sqlite3.Error, ValueError):
            self.event_info.config(text="Event import failed its format or data checks. No event forecast was created.")
            return
        self.event_info.config(text=f"{result['inserted']} new event(s) staged from {result['parsed']} row(s). "
            "Source authenticity unverified; excluded from forecasts and trading.")


def main():
    parser = argparse.ArgumentParser(description="Bank Nifty local research workspace")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--options", action="store_true", help="Open the offline Options Research tab")
    args = parser.parse_args()
    from banknifty.offline_runtime import install_offline_guard
    install_offline_guard()
    root = tk.Tk()
    app = Dashboard(root, args.data_dir, offline=True)
    if args.options:
        app.tabs.select(app.options_page)
    root.protocol("WM_DELETE_WINDOW", app.request_close)
    root.mainloop()


if __name__ == "__main__":
    main()

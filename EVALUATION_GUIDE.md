# Offline evaluation

The evaluation runner reads archived timestamped index observations, dated option contracts and option bid/ask books. It calls the same pure closed-bar pipeline used by the desktop research view. It does not connect to Groww, authenticate a subscription, place orders, create manual fills or establish live feasibility. Synthetic results test software behavior only.

## Run without the desktop UI

From this application folder, use the existing Python environment:

```powershell
python -m banknifty.options_evaluation --archive .\data\options-research\market-archive.sqlite3 --config .\experiment.json --output .\evaluation-report.json --frozen-config .\experiment-frozen.json
```

The synthetic demonstration uses `synthetic-archive.sqlite3` in the same workspace directory. The archive must already exist. Both output filenames must be new; earlier artifacts are never overwritten. The archive is opened read-only. Cancellation returns partial diagnostics, which are not a completed experiment. No external packages are installed by the runner.

`experiment.json` is a `FrozenExperimentConfig` object. The following is a schema example with illustrative dates. Replace the date range with the archive's actual timestamps. Monetary assumptions are deliberately absent, so this example reports unavailable P&L until explicit assumptions are supplied:

```json
{
  "name": "Offline research experiment",
  "start_at": "2026-01-05T09:40:00+05:30",
  "end_at": "2026-01-10T15:00:00+05:30",
  "strategy": "LONG_OPTION",
  "archive_mode": "reconstructed",
  "research_policy": {"horizon_minutes": 30},
  "replay_policy": {
    "manual_latency_seconds": 5,
    "max_entry_wait_seconds": 30,
    "max_exit_wait_seconds": 30,
    "fees_total": null,
    "slippage_per_leg": null,
    "quantity": null
  },
  "decision_interval_minutes": 15,
  "min_train_sessions": 3,
  "validation_sessions": 1,
  "holdout_sessions": 1,
  "walkforward_test_sessions": 1,
  "min_calibration_samples": 100,
  "min_bin_samples": 20,
  "min_calibration_days": 20,
  "min_class_samples": 10,
  "allow_unverified_calibration": false,
  "session_timezone": "Asia/Kolkata",
  "max_decisions": 2000
}
```

Supported strategies are `LONG_OPTION`, `DEBIT_SPREAD` and `CREDIT_SPREAD`. Decimal amounts are serialized as strings. `fees_total` is the total round-trip cost for all legs and the chosen quantity, including every fee/tax the researcher intends to model. `slippage_per_leg` is adverse currency points for each leg on each opening and closing side, rounded adversely to the dated tick size. `quantity` is matched option units and must satisfy every leg's lot size; it is not a lot count. No fee schedule or suitable position size is inferred.

Research sizing separately requires explicit capital, maximum risk fraction, lot limits and cost assumptions. Credit-spread sizing additionally needs a separate margin input; bounded expiry loss is not broker margin. Supplying replay quantity creates a hypothetical execution scenario and does not certify that the account could fund it. Stops and targets are optional policy assumptions. They trigger on observable net per-unit prices, then incur manual latency; they are not guaranteed loss caps.

## Coverage and timestamps

`observed` mode respects the archive's actual first local capture, in addition to the original source, receipt, provenance and metadata-known timestamps. Passing older source dates to a newly imported file cannot make those records available to an earlier observed decision. Replay receives immutable per-record first-capture times and never rewrites receipt timestamps to hide a delay.

`reconstructed` mode evaluates the supplied historical chronology and explicitly labels it unverified replay. It cannot prove what was actually known in real time. Daily OHLC/index CSV data alone is insufficient: it cannot supply option fills, depth, spread costs or option P&L.

By default the decision grid covers only observed underlying spans. The report labels this `observed_spans_only`; entirely missing days and the edges of a requested period are unknown. Complete-period and complete-day P&L are withheld even if every observed candidate has an outcome.

For known expected decision windows, add `expected_session_intervals` as an ordered JSON list of `[first_cutoff, last_cutoff]` timestamp pairs. Each pair uses inclusive, timezone-aware minute boundaries within one session date and within the experiment range. The grid is anchored to the experiment's first complete minute and uses `decision_interval_minutes`. Windows must be disjoint. They are user-supplied expectations, not an exchange holiday calendar. Every scheduled cutoff is evaluated even when no observations exist. Missing data appears as abstention with `data_gate_passed: false`, and cannot become known zero P&L. Intentional neutral abstention remains distinct from missing-data abstention.

Each decision queries only the preceding point-in-time snapshot. The pipeline requires ten complete contiguous sampled minute bars under its frozen density, delay and freshness policy. All option legs must pass the shared metadata, freshness, spread and optional OI gates. Independent OI timestamps are required whenever the OI filter is requested.

An option path must remain fresh, synchronized and executable at the required size. Stop detection is independent of full-position depth: a shallow stop quote still triggers, and exit waits for an executable book after latency. Missing intervals or unresolved exits withhold P&L. A provider or evidence-type change from the frozen origin blocks entry or makes an open path incomplete, even if a later quote restores the original provider. Every consumed monitoring update contributes to evidence classification. Synthetic or mixed outcome data cannot inherit a verified-origin label.

## Frozen partitions and calibration

The report embeds the complete configuration, its SHA-256 fingerprint and a semantic archive fingerprint. `freeze_experiment(archive, config)` binds a configuration before a run. Reusing a bound config rejects changed corpus records. The corpus is checked again before a completed run returns. This detects accidental historical changes; it is not protection against a researcher choosing a favorable period after seeing results.

Session dates use `session_timezone`. Expanding walk-forward training and validation precede each test block; the final session block is the untouched holdout. Only training labels fit probabilities. Validation and test labels never select rules, thresholds, bins or policies. There is no optimizer. Purging covers at least the full signal horizon plus exit latency and maximum exit wait, and also checks each actual label-availability time. A later outcome is excluded from training when it could overlap the next partition.

Calibration uses four fixed heuristic-strength bins, not a machine-learning dependency. The pipeline's strength feature is normalized movement, not a probability. An eligible bin needs completed, costed training outcomes with minimum total samples, both outcome classes, distinct training sessions and enough samples and sessions in that bin. Duplicate decisions cannot inflate counts; conflicting labels are rejected. Synthetic, mixed, receipt-only and unknown evidence never fit probabilities. Historical replay requires explicit `allow_unverified_calibration` opt-in and remains experimental/unverified. A `live_verified` label is an upstream assertion; this module does not authenticate the provider.

The target is **positive net quoted-replay P&L under the frozen costs**, not directional accuracy or guaranteed profit. Training Brier/reliability scores are labelled `in_sample`. Later validation/test scoring is `out_of_sample`, uses the frozen training bins and training base rate, and rejects earlier training cutoffs. Unsupported or sparse bins return unknown. Every model/report keeps `validated: false`; six synthetic sessions cannot establish calibrated confidence.

## Reading the report

`schema_version: 1` reports include:

- `status`, configuration/corpus fingerprints, frozen `config`, and evidence/assumption notes.
- `rows`: cutoff, session date, model, candidate/abstention, evidence kind, signal fingerprint, data-gate result, replay status, P&L/outcome when available, and label-availability timestamp.
- `summary.schedule_coverage`: expected-versus-observed scope and evaluated cutoff count.
- Per-model and per-day candidate counts, missing outcomes, data-unavailable decisions, scored P&L and descriptive positive rate. Missing outcomes are never counted as losses or zeros.
- `folds`: separated train/validation/test dates, purge counts, frozen calibration model, scoring roles, Brier/reliability diagnostics and flags confirming no fitting on tests.
- Always-bullish, always-bearish and zero-position comparisons. Directional baselines use the same underlying data gate, exact option snapshot and policies. Comparisons use only pairs with both outcomes available and show unmatched coverage.

Aggregate P&L sums hypothetical scenarios. It is not a funded portfolio: signals can overlap, and the runner does not reserve capital or margin across signals. Observations and days can be dependent; no independent sample size, significance interval, validated accuracy or live profitability is claimed. Repeatedly inspecting or tuning against a holdout compromises its independence; a new future period is needed for a new unbiased test.

## Verification

Deterministic tests cover delayed first capture, strict point-in-time inputs, future-price mutation, frozen corpus mismatch, actual label-end purging, depth-starved stops, adverse costs/slippage, absent option outcomes, observed-span/explicit-window coverage, synthetic monitoring contamination, origin continuity, calibration sample limits and read-only CLI output. These fixtures verify implementation behavior and are not market-performance evidence.

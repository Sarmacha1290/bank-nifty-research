# Using the offline options workspace

Open `Start Bank Nifty.pyw` or `Start Bank Nifty.cmd`, then select **Options Research**. This is the existing Windows desktop app with a new research tab. The default launcher blocks network calls and credential-file access. Existing historical charts, daily NSE data, exports and research roles remain available.

## Data-driven research and evaluation

Open **Data & evaluation** in Options Research. Load the six-session synthetic corpus to exercise the workflow, or import a normalized version-2 archive containing timestamped underlying observations, contract metadata and option quotes. The demo and imported market corpus use separate database files.

Select a historical cutoff and run the data-driven analysis. Ten complete sampled minutes, sample density, edge/internal gaps, source/receipt freshness and sequence checks must pass. A fixed momentum/trend rule derives the direction; neutral or insufficient evidence abstains. Each candidate still passes the existing options quality, listing, depth, sizing and risk filters. This is an explained research rule with no calibrated confidence.

Choose explicit fees, latency, slippage and experiment dates before evaluation. Frozen experiment configuration, input hashes, actual outcome availability, purged chronological partitions and missing outcomes remain in the saved run. Calibration tools suppress unsupported or synthetic results; reading a successful software test does not establish market performance. See `PIPELINE_SPEC.md` and `EVALUATION_GUIDE.md` for exact semantics and coverage limits.

The **Explain latest** action uses deterministic local facts. The optional AI interface is disabled by default and cannot create prices, change direction or execute orders; see `OPTIONAL_AI.md`. **Start Browser Companion.cmd** starts a read-only view at the printed loopback URL. Stop it with Ctrl+C. `WORKER_GUIDE.md` describes explicit bounded background archive analysis with pause and restart deduplication.

## Manual hypothesis workflow

1. Load the explicitly labelled synthetic example. Its prices, exchange, contracts, expiry, lot sizes and future path are invented for demonstration. They are not current NSE contracts.
2. Choose a bullish, bearish or neutral **hypothesis**, a long option, debit spread or hedged credit spread, and a horizon. The direction is a research input, not a trained model prediction.
3. Leave capital, risk fraction, fees and quantity cap blank until you can supply them. A candidate can be inspected while sizing stays unavailable. Credit-spread sizing additionally requires a separate margin estimate. The app does not fetch broker margin.
4. Evaluate. Read the source label, selected listed-contract claims, quote sides, reasons and rejected-data messages. A `no_trade` result is a valid outcome. Confidence is unavailable.
5. Run replay only after explicitly entering round-trip fees and adverse slippage. Replay estimates possible executions after manual latency; it never adds manual fills. The invented demo includes a dense future path for testing; it demonstrates arithmetic and workflow only. Imported paths with gaps remain unscored.
6. Use the manual journal only to record actions you actually report having completed elsewhere. Each opening/closing leg is a separate event with units, price, time and known or unknown fees. Signals never create these events automatically. Corrections preserve the original event.

## Data and timestamps

Import the normalized JSON/CSV schema in `OPTIONS_IMPORT_SCHEMA.md`; the UI can save an example. Files are bounded to 2 MiB, 200 contract versions and 2,000 quotes. Imports are reconstructed, unverified replay evidence even if the file claims current prices. Daily NSE or underlying OHLC files cannot become option quotes, Greeks or fills.

Each option contract supplies expiry, strike, option right, dated lot/tick values, effective interval, first-known time and an explicit catalogue listing claim. No current contract names or lot sizes are hard-coded. Quote source time, laptop receipt time and OI source time are distinct. A missing source time means unknown freshness. OI zero is an observation; missing OI is not zero. Fresh quote time does not establish OI freshness.

The operational defaults reject quote/receipt ages above five seconds, leg skew above one second, and OI age above thirty seconds when OI freshness is required. The one-second future-clock tolerance in structural validation does not permit future data in a decision: strategy and replay separately enforce the information cutoff. These are conservative research rejection parameters, not recommended trading latency. Change policy objects deliberately and record the assumptions; they are included in signal/replay evidence.

In manual hypothesis mode the underlying spot is caller-supplied and unverified. Data-driven mode requires typed, timestamped underlying observations and closed sampled bars. Reconstructed first-known times remain historical claims, and importing a file cannot authenticate them. File hashes and actual import times remain in archive lineage; only already-visible records enter the frozen decision fingerprint.

## Prices, losses and manual execution

Long legs use the ask and short legs use the bid for research entries. Reverse quote sides and adverse slippage are used for hypothetical exits. Available quoted size is a filter, not a fill guarantee. Total fees must cover the chosen quantity and all relevant opening/closing legs; the app does not know your tax, brokerage or exchange charges.

For matched quantity `q`, a width-100 debit spread costing 40 has contractual expiry loss `40q + fees` and profit `60q - fees`. A width-100 credit spread receiving 25 has loss `75q + fees` and profit `25q - fees`. These arithmetic bounds require the complete matched structure and the stated entry prices. Margin is separate. A stop is an intended exit trigger, not a guaranteed maximum loss.

If the short leg fills before its hedge, or the hedge closes first, the journal displays unhedged exposure and suppresses the planned spread bound. A recorded manual fill is a user report, not a broker-verified execution. Fully closed manual cash-flow P&L is separate from replay P&L; unknown fees leave net results unavailable. Open positions do not get invented realized P&L.

**Pause new entries** prevents new research evaluations but keeps the manual journal, exposure and exit warnings available. It does not close positions or cancel anything at a broker. Restart restores the pause state and validates local inputs. There are no order endpoints, execution buttons, webhooks or credential-bearing objects in the options layer.

## Replay and evaluation

Only timestamped option books can support hypothetical option fills. A decision at a bar close cannot fill earlier inside that bar. Entry/exit quotes must arrive after the respective manual latency and pass the same quality and liquidity gates used by the decision function. Missing synchronized legs, missing size/costs, stale data or gaps that might conceal a stop suppress a scored result. A stop can fill at a worse price after latency.

Underlying OHLC diagnostics are explicitly approximate. If stop and target both occur in one unresolved bar, the diagnostic assumes the stop occurred first. Missing intervals make the path unavailable. It never generates option fills or option P&L.

The Python evaluation API provides whole-date chronological holdout/walk-forward folds with an embargo at least as long as the outcome horizon. Parameters must be frozen before examining the held-out periods. No real option-history sample, calibrated model, win-rate claim or validated strategy is included. The existing official daily archive is useful historical context, not live intraday evidence.

## Storage, exports and operating limits

Options state, imported source files, immutable market archives, saved research runs and the separate append-only ledger live under `data/options-research`. Existing market history remains in `data/market-data.sqlite3`. Replay runs retain selected inputs, policy, costs and fingerprints across restart and new evaluations. Validated backups include these artifacts and worker checkpoints. Signal hashes and event chains detect accidental modification; they are not externally signed proof against someone who controls the database.

Use the local JSON/CSV exports for detailed review and the responsive HTML report for a browser-readable snapshot. HTML reports contain no remote resources and do not refresh themselves. They can be viewed on another device after a deliberate private file transfer. No public site, mobile account service or cloud background process has been deployed.

The program runs on this laptop while it is awake. Offline mode does not schedule live capture or renew Groww access. Do not expect it to run when the laptop is off. The existing connection utility is preserved for a separately authorized test, but the main research launcher does not open it in offline mode.

## Pending live-data check

During an appropriate NSE session, the next separately authorized step is a bounded, read-only Groww test: index first, then one listed option contract and a small chain snapshot. Use the masked local token workflow, never chat. Record source and receipt times, monotonic elapsed time, update counts, duplicate/out-of-order events, source/receipt age distributions and stale transitions. Test shutdown and reconnect deduplication. Review whether each bid/ask, OI and catalogue field has a documented timestamp; do not borrow last-trade time as the timestamp for unrelated fields.

A successful subscription or HTTP response alone does not establish fresh options data. No authenticated feed was exercised during this implementation. Availability, daily access renewal, option-book/OI freshness and source authenticity remain live-test gates.


## Run the checks again

Double-click `Run Offline Tests.cmd` or run `python -I -B run_tests.py`. The runner copies approved source and synthetic fixtures into a new temporary test project, excludes your working data, blocks external networking and credential files outside synthetic fixtures, and retains a test log and JSON result. It permits Windows loopback socket pairs and the existing `whoami`/`icacls` fixture checks. The runner installs nothing. See README for the public preview verification results.

# Bounded local archive worker

The worker analyzes the application's saved archive while this laptop and the worker process are running. It never collects broker data, accesses an account, places orders, records fills, registers a service or starts automatically. It cannot run while the laptop is off. A stale archive produces no-trade outcomes, not fabricated prices.

Run manually from this project folder:

```powershell
python -m banknifty.research_worker --enable --archive-kind market --duration-seconds 300 --decision-delay-seconds 1 --horizon-minutes 30
```

Omitting `--enable` is inert. Choose `--archive-kind synthetic` only for the clearly labelled synthetic corpus. `--storage-dir` defaults to `data/options-research`. The duration is positive and at most eight hours. Polling defaults to one second. Press Ctrl+C to stop. There is no service, scheduler or startup installation. Only one worker process can hold the local file lease; its OS lock is released when the process ends.

The desktop's **Pause new entries** state applies to the worker. Missing, duplicate-key, malformed or unsupported pause state blocks analysis. Pausing and cancellation leave manual fills unchanged. Guarded publication acquires an exclusive SQLite transaction before checking eligibility, so a rollback-journal reader waits before these checks rather than delaying COMMIT afterward. Pause, cancellation, clocks and decision/duration deadlines are checked before INSERT and again after INSERT immediately before COMMIT; rejected transactions roll back. Clock samples follow the control-state reads so a slow read cannot leave the deadline check using an earlier time.

SQLite lock acquisition can wait up to its existing ten-second timeout. Stop therefore may not respond immediately during contention, but release of the lock triggers fresh eligibility checks before publication. `recorded_at` is sampled after the lock wait and denotes transaction recording time, not exact fsync completion. Frozen source, receipt and decision cutoffs remain unchanged.

Each minute's cutoff is fixed at the closed minute plus the configured decision delay. The default one-second delay is within the pipeline's five-second decision gate. Processing after that gate is recorded as no-trade. Missed intervening minutes are not replayed as current entries. Archive queries use `observed` mode and require data to have been locally available by the cutoff. A newly imported historical file cannot become a current live signal.

Qualified results and no-trade attempts are immutable signals in the existing `options-ledger.sqlite3`, with worker configuration, cutoff, evidence and input fingerprints. Worker signal IDs identify the boundary/configuration attempt; original pipeline signal IDs are retained separately. The worker never calls a fill-writing method. Risk sizing remains unavailable until explicit capital, limits, fees and any needed margin are supplied through a typed `WorkerConfig`; the CLI makes no financial assumptions.

`worker-state.json` is a hash-checked **last status and restart checkpoint**, not a complete event log or an authenticated signature. Immutable attempts remain in the ledger. Restart checks that ledger before recomputing a boundary, including recovery if the process ended after journaling but before saving its checkpoint. A malformed checkpoint is preserved and blocks startup; inspect or restore it locally before trying again. A backward wall or monotonic clock stops publication. Changed configuration cannot reopen an already journaled boundary.

`read_worker_status(storage_dir)` is read-only and exposes the last status plus heartbeat freshness. A fresh heartbeat does not prove the process is still alive, and never establishes market-data freshness. `validate_worker_state(envelope)` is available to backup validation. `worker.lock` and `worker-state.pending` are transient implementation files and should not be restored as active state.

No worker was started against the user's real archive during implementation. Tests use synthetic archives, fake clocks, controlled waits and real SQLite reader contention; they do not contact a broker.

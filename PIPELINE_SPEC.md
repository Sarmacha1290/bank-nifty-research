# Timestamped underlying to options research

The 0.98 pipeline uses `banknifty/options_pipeline.py` for the same deterministic computation in historical replay and a future approved live adapter. It accepts data records, never a broker client. It does not connect, create an order, infer a fill, or estimate a calibrated probability.

`run_pipeline(underlying, contracts, quotes, as_of=..., strategy=...)` returns an immutable `PipelineDecision`. The existing `generate_signal(... direction=..., underlying_price=...)` remains available as explicitly labelled manual-hypothesis research.

## Underlying records and completed bars

An `UnderlyingObservation` contains an explicit symbol, positive Decimal price, source timestamp (or missing), original receipt timestamp, raw provenance, and optional sequence. It has no index volume, trade-direction, or open-interest field. Receipt-only REST prices cannot enter the derived-direction pipeline. Historical candles cannot masquerade as observed ticks or option quotes.

The decision boundary is the UTC minute containing `as_of`. The rule uses exactly ten consecutive one-minute intervals immediately before this boundary. Each interval includes its start and excludes its end: a tick at 10:00:00 belongs to the next minute and cannot change the 10:00 assessment. A record must have been sourced, received, and captured by the decision cutoff. Later rows cannot alter an earlier decision or its fingerprint.

These are **sampled one-minute OHLC bars**, not official exchange candles. Each bar preserves its exact input observations and input hash. No missing interval or edge is forward-filled.

Default operational rejection limits are configurable through `PipelinePolicy`:

| Limit | Default | Purpose |
|---|---:|---|
| Distinct source samples per minute | 12 | Reject sparse sampled intervals |
| First and last sample edge gaps | 10 seconds | Require coverage near both minute edges |
| Largest internal sample gap | 15 seconds | Reject discontinuous intervals |
| Original receipt minus source timestamp | 0–5 seconds | Reject negative clocks and late arrivals |
| Latest completed-bar source age at decision | 5 seconds | Reject a stale reference price |
| Decision after the minute boundary | 5 seconds | Keep the processing window bounded |

These limits mirror the earlier sampled-minute safeguards. They are engineering limits, not optimized trading parameters or proof of sufficient execution speed. Live clock behavior still needs observed validation. Duplicate observations retain the first receipt. Conflicting values, sequence regressions, missing sequences after a sequenced stream, and source/evidence switches block the bounded history.

## Fixed explanatory rule

For ten accepted closes `c[0]` through `c[9]`:

- Three-minute momentum is `c[9] / c[6] - 1`.
- The fast mean is the arithmetic mean of `c[7:10]`.
- The slow mean is the arithmetic mean of `c[0:10]`.
- Positive momentum with fast mean above slow mean yields `BULLISH`.
- Negative momentum with fast mean below slow mean yields `BEARISH`.
- All other cases yield `NEUTRAL` and a no-trade result.

The formula version is `closed-minute-momentum-trend-v1`. Its reported heuristic strength is `min(1, abs(momentum) / 0.01)`, a normalized movement magnitude. **It is not a probability, forecast accuracy, or confidence estimate.** Any statistical calibration belongs to a separately frozen evaluation experiment.

The derived direction and last closed-bar price then enter the existing listed-contract strategy function. Symbol identity must match the option catalogue exactly. Effective metadata, stale or receipt-only quotes, option spread/OI filters, synchronization, sizing, fees, and credit-margin requirements remain enforced. Mixed synthetic/replay/live evidence is blocked. No contract, expiry, lot size, or tick size is manufactured.

## Frozen decisions and restart handling

The decision hash covers the visible underlying records, sampled bars, rejected-data diagnostics, visible option inputs, policies, cutoff, and formula. The resulting option signal receives that full lineage, replacing the manual-price warning only after the underlying checks pass. Every emitted signal still has `trading_allowed=False` and `confidence=None`.

`PipelineSession.process(...)` permits one decision attempt per closed boundary, fixed caller namespace, strategy, and frozen policies. Use a separate fixed namespace per instrument stream; the default is `default`. The namespace does not depend on arriving rows, so even an empty first attempt is frozen. The first result is retained even if it abstains; late or reconnected data cannot rewrite it. A duplicate call returns `emitted=False` and no new decision. Changing a policy creates a separate explicitly different experiment.

The wrapper has no filesystem access. Its `checkpoint()` returns immutable key/fingerprint pairs; the caller must persist and reload those pairs with the same namespace for restart durability. A UI must display the original decision time and evaluate current data staleness separately rather than treating an earlier stored candidate as new.

Always-bullish and always-bearish comparison baselines use `generate_baseline(...)` only after the same underlying data gates passed, and retain separate identifiers and labels. They cannot rescue an incomplete input history.

## Limits still requiring real evidence

Source/evidence classifications are explicit upstream claims; local validation cannot authenticate a provider. Synthetic and reconstructed replay data remain visibly distinct. There is no claim that this rule predicts profits or has been calibrated. Source-clock reliability, actual listed-contract completeness, live freshness, and an approved market-session calendar still require separate review before any live release. The pipeline itself does not infer a trading calendar or turn a synthetic date into a verified exchange session.

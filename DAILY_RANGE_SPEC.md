# Full-day movement range: target and validation contract

The intended output is a forecast interval for **Nifty Bank spot index levels**,
with a lower bound, upper bound, time made, and the portion of the regular NSE
session it covers. It is separate from the 15-, 30- and 60-minute return targets.
There is no trained or calibrated day-range forecaster in this version.

## Exact targets

1. **Full-session high and low:** highest and lowest index level from the regular
   session open through close. The daily distance is high minus low, in index
   points; dividing by the first sampled level yields a percentage of open.
2. **Remaining-session high and low:** highest and lowest level strictly after
   an exact, completed-minute information cutoff through close. An offline label
   may be calculated only after that whole session is recorded. A future model
   must receive only inputs available at or before its cutoff.

The present label source is locally captured, quality-gated Groww index-feed
updates. Every one of the 375 regular-session minutes must have an accepted
sampled minute bar; no missing minute is filled. These highs and lows are
**sampled extremes**, not official NSE high/low values, and can miss movements
between updates. When official timestamped OHLC becomes available from an
authorized source, it must be stored under separate provenance and compared,
not silently substituted. Partial sessions have coverage rows and blank labels.

[NSE's historical index-data page](https://www.nseindia.com/reports-indices-historical-index-data)
offers downloadable daily index OHLC. The application now fetches a small batch
of completed-day, all-index CSVs from NSE's archive, requires exactly one
Nifty Bank row and valid date/OHLC fields, and saves source URL, retrieval time
and SHA-256. These daily records stay separate from sampled bars and do not
provide the intraday timing of extreme values. This preview ships no official
or provider market records; access and use authorized data at your own cost
and under applicable provider terms.

The application can export an audit to distinguish missing or incomplete
sampled sessions from complete comparisons with independently sourced daily
extremes. The audit shows point differences where both sources qualify; a
rounding tolerance does not turn missing data into agreement.

An exploratory fixed reference uses the previous official close and ranked
low/high offsets from the preceding 40 consecutive sessions. The target day's
OHLC only scores the reference after it was formed. Missing sessions invalidate
a window. A retrospective benchmark reconstructed from later downloads does
not establish what was available before the session, live performance or a
calibrated probability interval.

## Forward paper record

The software can now lock one next-session reference before 09:15 IST. It
requires 41 consecutive official NSE days through the preceding regular
session, rejects any source retrieved after the recording time, and stores a
hash of the 41 source rows. The record is append-only for that date. After the
official target-day OHLC is archived, the app scores whether both extremes
fell inside the already stored interval; it cannot use the target-day row to
form the interval.

Paper reference records and retrospective reports are generated locally
and not included. A timestamp or hash is local provenance, not independent
attestation or a trading signal.

[Groww's historical-data documentation](https://groww.in/trade-api/docs/python-sdk/historical-data)
also describes day candles, but its availability and exact timestamp convention
for Bank Nifty must be checked on actual authenticated responses. The two
sources should be compared on overlapping dates before the range model is
trained. Official daily highs/lows are preferable as final full-day targets;
intraday sampled data is still needed to build cutoff-time features and
remaining-session labels.

## What a future forecast must prove

- Fix forecast times in advance, such as shortly after the open and at later
  completed-minute cutoffs. Never use the final daily high/low, later option
  chain, later bank prices, or revised source data as an input at those cutoffs.
- Keep whole trading dates together in chronological development, validation
  and untouched test periods. Report the number of complete eligible days and
  excluded days. Evaluate separately by forecast time and market regime.
- Compare high and low errors in points and percent with simple prior-day and
  recent-range baselines. For forecast intervals, report empirical containment
  versus the stated target coverage, average interval width, and misses on both
  sides. An extremely wide interval can contain most outcomes yet be useless.
- Report changes in performance after fees/slippage only if a separate,
  executable-instrument trading strategy is explicitly specified and tested.
  Spot-index range accuracy alone says nothing about option profit.

The release gate is evidence-based: collect complete real sessions, verify the
sampled extremes against an authoritative range source where possible, freeze
the forecasting method, and demonstrate stable out-of-sample coverage and
error against baselines. No fixed accuracy percentage can be promised before
those tests. The desktop app therefore displays **Forecast unavailable**.

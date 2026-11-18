# Groww mapping and connection readiness

Reviewed against official documentation on 18 October 2026. These adapters run offline and accept data objects; they contain no connection, authentication or order capability. All mapped records remain `live_unverified`. The tests use documented schemas and dated illustrative values, not captured live market evidence.

| Input | Mapping and limitation |
| --- | --- |
| Index feed | Catalogue exchange/segment/token selects `value`; `tsInMillis` supplies its own source time. Missing time stays missing. |
| Option depth feed | `buyBook`/`sellBook` prices and quantities form the top book. Its own `tsInMillis` dates that book. |
| Option LTP feed | `ltp` and its timestamp populate a separate last-price observation. They cannot populate absent bid/ask data. |

Feed milliseconds follow the documented unit. The depth example contains a seconds-sized value despite that schema; the adapter deliberately does not guess a different unit. That example remains stale under literal milliseconds and cannot pass quality gates. This discrepancy needs an authorized live-session check. [Groww feed documentation](https://groww.in/trade-api/docs/python-sdk/feed).

REST quote maps `bid_price`, `offer_price`, their quantities and OI. `last_trade_time` dates only the last-price field. Neither a quote timestamp nor an OI timestamp is documented, so both remain unknown. Option chains map catalogue-matched trading symbols, LTP, OI, volume and Greeks; they lack documented bid/ask and field timestamps. IV in chains is retained in the documented percentage units. No REST timestamp alias, candle or unknown extra field can upgrade freshness. [Groww live-data documentation](https://groww.in/trade-api/docs/curl/live-data).

Catalogue parsing preserves the observed Bank Nifty index and option rows, including provider identifiers, strike, lot, tick and expiry **date**. `instrument_contract` requires an explicit timezone-aware expiry time whose Indian date matches the row; no expiry clock is inferred. Metadata becomes known/effective at local observation, never earlier. Retain successive versions without rewriting old input fingerprints; an ambiguous version must block a decision. [Groww instruments documentation](https://groww.in/trade-api/docs/curl/instruments).

`CatalogueSnapshot.raw` preserves the original CSV with its hash. Per-record provenance preserves selected raw rows, including unrecognized fields. JSON callers should use `decode_provider_json` to preserve decimal lexemes; already-decoded Python floats cannot recover lost precision. Store original response bytes and transport receipt metadata in the archive separately. Credential-labelled payload fields are rejected with safe errors.

The inert `plan_index_ltp`, `plan_option_quote` and `plan_option_chain` helpers produce GET request plans for paths already allowed by the preserved `GrowwDataClient`. They do not execute requests. The explicit `GrowwDataClient.option_quote(ProviderInstrument)` wrapper preserves exact decimals and uses that existing allowlist. Its client blocks redirects, bounds response sizes and uses a 25-second request timeout. No SDK upgrade or new endpoint is required. Keep credentials out of snapshots, plans, logs and saved files.

A future authorized smoke test should record monotonic and wall-clock receipt times, compare each independent source timestamp, test missing/stale/crossed/duplicate/out-of-order data, inspect catalogue identity and quantity units, and verify bounded shutdown. Require synchronized index and option books before research eligibility. OI/Greeks remain excluded from freshness-dependent rules until their independent timing is established. No connection or subscription test was performed in this phase.

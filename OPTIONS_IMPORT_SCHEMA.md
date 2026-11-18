# Normalized option import schema, version 1

This is an application-owned format, not a Groww response format. It accepts no broker credentials, execution commands, Greeks inferred from candles, or `live_verified` declaration. All imported records become `replay_quote` with reconstructed, unverified chronology. The original file hash and actual import observation time are retained separately from individual decision records.

Save this invented example as UTF-8 JSON. The catalogue values and dates illustrate fields only; they are not current contract specifications. The UI **Save example** action also generates a labelled example.

```json
{
  "schema_version": 1,
  "underlying_price": "50000",
  "contracts": [{
    "contract_id": "EXAMPLE-CALL",
    "underlying": "BANKNIFTY",
    "exchange": "NSE",
    "option_type": "CALL",
    "strike": "50000",
    "expiry_at": "2026-01-29T15:30:00+05:30",
    "lot_size": 10,
    "tick_size": "0.05",
    "effective_from": "2026-01-01T00:00:00+05:30",
    "effective_until": null,
    "known_at": "2026-01-01T00:00:00+05:30",
    "listed": true
  }],
  "quotes": [{
    "contract_id": "EXAMPLE-CALL",
    "source_at": "2026-01-05T10:00:00+05:30",
    "received_at": "2026-01-05T10:00:01+05:30",
    "bid": "100.00",
    "ask": "101.00",
    "bid_size": 100,
    "ask_size": 100,
    "last": null,
    "open_interest": 0,
    "oi_source_at": null,
    "sequence": 1
  }]
}
```

Identifiers use 1–100 ASCII letters/digits or `-_.:`. Prices are finite nonnegative decimal values; string decimals preserve precision. Sizes, OI, sequence and lots are nonnegative integer values, with positive lot size required. Timestamps require an explicit timezone offset. `option_type` is `CALL` or `PUT`; `listed` is a Boolean catalogue claim. The quality layer separately rejects invalid, expired, off-tick, crossed, stale or unavailable quotes.

JSON requires `schema_version`, `contracts`, and `quotes`. `underlying_price` is optional. Contract fields are all required except `effective_until`. A quote requires `contract_id`, `source_at`, `received_at`, `bid`, and `ask`; optional fields default to unknown. Null means unknown, never zero. Unknown fields and duplicate JSON keys are rejected. Every quote must have supplied metadata. Multiple explicitly dated metadata versions are supported; overlapping conflicting versions are blocked at evaluation.

CSV contains one quote per row with repeated contract metadata. It must use every column below exactly once (order is irrelevant). Optional/unknown cells are blank; `listed` is `true` or `false`. Repeated metadata for the same version must agree. The bundle has one reference underlying price, not a time series of underlying observations.

```csv
ask,ask_size,bid,bid_size,contract_id,effective_from,effective_until,exchange,expiry_at,known_at,last,listed,lot_size,oi_source_at,open_interest,option_type,received_at,sequence,source_at,strike,tick_size,underlying,underlying_price
```

Limits: 2 MiB per file, 200 contract versions, 2,000 quotes. This bounded desktop importer is for small research slices. It is not a streaming adapter or large-history ingestion service. A successful import proves schema consistency only, not that an exchange listed the contracts or published the values.

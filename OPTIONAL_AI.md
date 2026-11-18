# Optional explanation service

The application explains a saved research result locally without a key, network connection or language model. `local_explanation(signal)` returns deterministic facts tied to the immutable input fingerprint. The optional API feature is **disabled by default**. No API request, account connection, credential access or billing test was performed during implementation.

The optional model can only select approved fact identifiers. It cannot write arbitrary prose, add or change prices, quantities, confidence, direction, trade recommendations, fills or execution claims. The application supplies every displayed sentence. Mandatory data-quality and risk limitations remain visible even if the model omits them. This constrained interface provides optional emphasis of existing explanations; it is not another forecasting engine or a general financial chatbot.

## Local use and UI integration

```python
from banknifty.explanation_service import local_explanation

result = local_explanation(saved_research_signal)
display_text = result.text
public_snapshot = result.to_dict()
```

The input must be the existing immutable `ResearchSignal`, with trading disabled and no claimed calibration. The result cannot mutate the signal. Exact diagnostics are displayed locally after best-effort redaction and are never transmitted. Arbitrary hidden secrets cannot be reliably identified in free text; keep credentials out of all research inputs.

## Ready-to-connect interface, currently unconnected

`ExplanationService(config).explain(signal, api_key=ephemeral_key, allow_network=True)` is the explicit connection boundary. Do not call it automatically from timers, imports or signal creation. The default dashboard uses only the local helper.

Before a later authorized connection, supply all of the following in `ExplanationConfig`: `enabled=True`, an explicit supported model identifier, `max_estimated_usd`, `input_usd_per_million` and `output_usd_per_million` as positive `Decimal` values. Verify the model's availability, structured-output support and current prices in the user's API account. There is deliberately no selected model, assumed price, stored key or environment-variable lookup.

The caller supplies the credential only for the current method call. No key appears in configuration, results, logs or files. Python cannot guarantee erasure of all process-memory copies. No persistent account access, SDK installation or model discovery is performed.

Operational limits include request count, request-body bytes, maximum output tokens, session reserved tokens, response size and socket timeout. The default session allows at most one request; the timeout is ten seconds and output is capped at 512 tokens. Budgets reserve failed and cancelled attempts, with no automatic retry. Concurrent and repeated requests for the same fixed record are suppressed or served from the in-memory result cache. Limits reset with a new service instance and do not replace account-level controls.

The USD limit uses caller-supplied rates and a conservative byte-based input-token estimate plus a protocol allowance. **It is an estimated local budget, not a guaranteed billing cap.** A wrong price, provider billing rules or another process can invalidate the estimate. Set appropriate API account spending controls before connecting. No billing settings are changed by this application.

Cancellation before sending avoids a request; cancellation after sending discards output but cannot recall the request or undo possible charges. A socket timeout is not a guaranteed whole-operation deadline. UI callers should run explicitly enabled requests outside the UI thread and ignore late results after cancellation.

## Provider payload and validation

The standard-library HTTPS transport uses only `POST https://api.openai.com/v1/responses`, normal TLS validation, no environment proxy configuration, no redirects and no retries. It sets `store:false`, sends no tools or conversation history, and applies a strict `text.format` JSON schema. The payload contains only the fingerprint and a small catalogue of application-owned facts. Raw quotes, catalogue payloads, exact source diagnostics, account information, capital, sizing limits, numerical entry prices and quantities are excluded.

The response must be completed, contain exactly one assistant JSON output, echo the input fingerprint and choose unique known fact identifiers. Refusals, incomplete responses, tool items, duplicate JSON keys, unsupported keys, free prose and unknown facts are rejected. Provider errors and raw responses are never logged or included in UI exceptions. A failure returns a clearly labeled local explanation.

Transport injection does not grant network permission. The shipped HTTPS transport, its subclasses and unknown custom transports require `allow_network=True`; passing one explicitly cannot bypass this gate. Only a reviewed pure test transport declaring the exact boolean `network_capable=False` is treated as a fixture and labelled **FIXTURE ONLY**. Arbitrary caller-supplied Python is trusted code, not sandboxed by this declaration. Network-capable attempts are reported as remote attempts once sending begins, including failure or cancellation afterward. Cancellation before sending remains a local result with no remote attempt. Tests replace the HTTPS connection completely; they do not call the real API.

The API format follows official [Structured Outputs documentation](https://developers.openai.com/api/docs/guides/structured-outputs) and the [Responses API migration guide](https://developers.openai.com/api/docs/guides/migrate-to-responses), reviewed on 18 October 2026. `store:false` disables response application-state storage; it does not establish zero retention of all provider data. See official [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data) before authorizing transmission.

## Verification and remaining boundary

`tests/test_explanation_service.py` uses synthetic research records and mock transports only. It covers privacy minimization, request schema, preserved research, malformed output, fabricated numbers/directions, credential injection, response limits, budgets, repeated/concurrent clicks, cancellation, sanitized failures and the fixed HTTPS boundary. A real model/account connection and its availability, billing and response behavior remain untested.

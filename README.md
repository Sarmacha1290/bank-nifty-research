# Bank Nifty Research

An experimental Python toolkit for exploring NIFTY Bank market and options signals, reviewing capture quality, and evaluating historical assumptions offline. This is the v0.99 research snapshot, not a live-market-validated product. It does not place broker orders.

## Start locally

Use Python 3 with Tkinter. On Windows, run `Start Bank Nifty.cmd` for the desktop dashboard. Run `python run_tests.py` for the offline test suite. See [Options Guide](OPTIONS_GUIDE.md) and [Evaluation Guide](EVALUATION_GUIDE.md) for the research workflows.

This source-only copy intentionally does not include any collected market data, broker credentials, private test reports, or local state. The project shows how to import your own authorized data; follow the provider's terms. The Groww and AI integrations are optional, and credentials are neither included nor needed for offline tests. Dated weights, prices, and other external reference files are not shipped; workflows that need them must remain unavailable until an authorized local input is supplied. Test-only weights use invented symbols and values and must not be imported for market use.

This project is not affiliated with NSE or Groww and is not investment advice.

The offline suite includes optional SDK integration checks (skipped when the SDK is absent) and local loopback tests for the browser companion. A restricted environment that blocks localhost cannot validate those integration tests. The immutable-run and restore checks also require an ordinary writable filesystem with same-volume hard-link support; there is no unsafe overwrite fallback. On Windows, use a short writable TEMP directory if long fixture paths fail.

## Validation

The Windows preview check recorded 802 passing tests, 26 blocked by environment permissions (6 localhost and 20 same-volume hard-link checks), and 11 skipped because the optional Groww SDK was not installed. No assertion failures were recorded. These results do not establish a clean full-suite pass, live market-data validation, or forecast accuracy.

Source code is distributed under the [MIT License](LICENSE). The license does not grant rights to third-party data or services.

## Contributing

Bug reports and improvements are welcome. Open a GitHub issue with your Python and operating system versions, steps to reproduce, and what you expected versus what happened. Include a minimal example using synthetic data if you can. Pull requests are welcome, too.

Do not include broker credentials, personal data, or downloaded market datasets in issues, pull requests, or attachments.

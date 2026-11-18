"""Option-chain arithmetic must never infer a missing exchange timestamp."""
from datetime import date, datetime, timedelta
from dataclasses import replace
from contextlib import closing
import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import sqlite3

from banknifty.agents.options_futures import (analyze_option_chain,
    compare_option_chains, describe_iv_term_structure, describe_option_structure,
    summarize_known_option_positions)
from banknifty.derivative_catalog import nearest_banknifty_option_expiry
from banknifty.groww import GrowwDataClient, IST
from banknifty.option_history import (count_option_chain_snapshots, export_option_chain_csv,
                                      save_option_chain_analysis)


NOW = datetime(2026, 10, 5, 10, 0, tzinfo=IST)
EXPIRY = date(2026, 10, 27)


def chain():
    return {"status": "SUCCESS", "payload": {"underlying_ltp": 55050,
        "strikes": {"55000": {
            "CE": {"trading_symbol": "BANKNIFTY26O2755000CE", "ltp": 500,
                   "open_interest": 100, "volume": 20, "greeks": {"iv": 18}},
            "PE": {"trading_symbol": "BANKNIFTY26O2755000PE", "ltp": 450,
                   "open_interest": 150, "volume": 30, "greeks": {"iv": 19}}},
            "55100": {
            "CE": {"trading_symbol": "BANKNIFTY26O2755100CE", "ltp": 450,
                   "open_interest": 200, "volume": 40, "greeks": {"iv": 18.5}},
            "PE": {"trading_symbol": "BANKNIFTY26O2755100PE", "ltp": 500,
                   "open_interest": 250, "volume": 50, "greeks": {"iv": 19.5}}}}}}


class OptionsFuturesTests(unittest.TestCase):
    def test_complete_chain_pcr_and_missing_exchange_time(self):
        report = analyze_option_chain(chain(), EXPIRY, NOW)
        self.assertEqual(report.status, "receipt_time_only_complete")
        self.assertIsNone(report.exchange_at)
        self.assertEqual((report.strikes, report.complete_pairs), (2, 2))
        self.assertAlmostEqual(report.oi_pcr, 400 / 300)
        self.assertAlmostEqual(report.volume_pcr, 80 / 60)
        self.assertEqual(report.top_call_oi_strike, 55100)
        self.assertEqual(report.top_put_oi_strike, 55100)
        self.assertEqual(report.atm_strike, 55000)

    def test_partial_chain_blocks_whole_chain_pcr(self):
        response = chain()
        del response["payload"]["strikes"]["55100"]["PE"]
        report = analyze_option_chain(response, EXPIRY, NOW)
        self.assertEqual(report.status, "receipt_time_only_partial")
        self.assertEqual(report.complete_pairs, 1)
        self.assertIsNone(report.oi_pcr)
        self.assertIsNone(report.volume_pcr)

    def test_greeks_are_preserved_but_open_interest_is_not_a_signed_position(self):
        response = chain()
        call = response["payload"]["strikes"]["55000"]["CE"]
        call["greeks"].update(delta=0.5, gamma=0.002, theta=-5,
                               vega=10, rho=2)
        report = analyze_option_chain(response, EXPIRY, NOW)
        parsed = next(row for row in report.contracts if row.trading_symbol == call["trading_symbol"])
        self.assertEqual((parsed.delta, parsed.gamma, parsed.theta,
                          parsed.vega, parsed.rho), (0.5, 0.002, -5, 10, 2))
        with self.assertRaisesRegex(ValueError, "position"):
            summarize_known_option_positions(report, {}, 30)
        totals = summarize_known_option_positions(report, {parsed.trading_symbol: -2}, 30)
        self.assertAlmostEqual(totals["sensitivities"]["gamma"], -0.12)
        self.assertAlmostEqual(totals["sensitivities"]["delta"], -30)
        self.assertIn("not_market_dealer_gamma", totals["interpretation"])
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            save_option_chain_analysis(database, report)
            destination = Path(folder) / "chain.csv"
            export_option_chain_csv(database, destination)
            with destination.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            saved = next(row for row in rows if row["trading_symbol"] == parsed.trading_symbol)
            self.assertEqual(saved["gamma"], "0.002")
        response = chain()
        response["payload"]["strikes"]["55000"]["CE"]["greeks"]["gamma"] = -1
        with self.assertRaisesRegex(ValueError, "gamma"):
            analyze_option_chain(response, EXPIRY, NOW)

    def test_option_structure_and_snapshot_change_are_descriptive(self):
        prior = analyze_option_chain(chain(), EXPIRY, NOW)
        structure = describe_option_structure(prior)
        self.assertEqual(structure["max_pain_strike"], 55100)
        self.assertAlmostEqual(structure["atm_put_minus_call_iv_points"], 1)
        response = chain()
        response["payload"]["strikes"]["55000"]["CE"]["open_interest"] += 10
        later = analyze_option_chain(response, EXPIRY,
                                     NOW + timedelta(minutes=1))
        changes = compare_option_chains(prior, later)
        self.assertEqual(changes["oi_change_by_symbol"]["BANKNIFTY26O2755000CE"], 10)
        with self.assertRaisesRegex(ValueError, "increase"):
            compare_option_chains(later, prior)
        response = chain()
        del response["payload"]["strikes"]["55100"]["PE"]
        self.assertIsNone(describe_option_structure(
            analyze_option_chain(response, EXPIRY, NOW))["max_pain_strike"])

    def test_iv_term_structure_requires_near_simultaneous_expiries(self):
        near = analyze_option_chain(chain(), EXPIRY, NOW)
        far_response = chain()
        far_response["payload"]["strikes"]["55000"]["CE"]["greeks"]["iv"] = 20
        far = analyze_option_chain(far_response, date(2026, 11, 24),
                                   NOW + timedelta(seconds=2))
        curve = describe_iv_term_structure((near, far))
        self.assertEqual(curve["far_minus_near_call_iv_points"], 2)
        self.assertIn("freshness_unverified", curve["interpretation"])
        stale = analyze_option_chain(far_response, date(2026, 11, 24),
                                     NOW + timedelta(minutes=1))
        with self.assertRaisesRegex(ValueError, "close enough"):
            describe_iv_term_structure((near, stale))

    def test_synthetic_input_is_not_labelled_groww(self):
        report = analyze_option_chain(chain(), EXPIRY, NOW, source_kind="synthetic")
        self.assertEqual(report.status, "synthetic_demo_complete")
        self.assertIn("synthetic", report.source)

    def test_rejects_wrong_underlying_and_invalid_oi(self):
        response = chain()
        response["payload"]["strikes"]["55000"]["CE"]["trading_symbol"] = "NIFTY26O2755000CE"
        with self.assertRaisesRegex(ValueError, "does not match"):
            analyze_option_chain(response, EXPIRY, NOW)
        response = chain()
        response["payload"]["strikes"]["55000"]["CE"]["open_interest"] = -1
        with self.assertRaisesRegex(ValueError, "open interest"):
            analyze_option_chain(response, EXPIRY, NOW)

    def test_rejects_formula_or_multiline_symbol_before_archive_or_excel(self):
        for prefix in ("=1+1", "BANKNIFTY\n=1+1", "BANKNIFTY\t+1"):
            response = chain()
            response["payload"]["strikes"]["55000"]["CE"]["trading_symbol"] = prefix + "55000CE"
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, "does not match"):
                analyze_option_chain(response, EXPIRY, NOW)

    def test_read_only_option_endpoint_requires_explicit_date(self):
        client = GrowwDataClient("fixture-token")
        with self.assertRaises(ValueError):
            client.banknifty_option_chain("2026-10-27")
        with patch.object(client, "_get", return_value=chain()) as request:
            client.banknifty_option_chain(EXPIRY)
            request.assert_called_once_with(
                "/option-chain/exchange/NSE/underlying/BANKNIFTY",
                {"expiry_date": "2026-10-27"})

    def test_nearest_expiry_requires_both_sides(self):
        catalog = ("exchange,segment,underlying_symbol,instrument_type,expiry_date\n"
                   "NSE,FNO,BANKNIFTY,CE,2026-10-06\n"
                   "NSE,FNO,BANKNIFTY,CE,2026-10-27\n"
                   "NSE,FNO,BANKNIFTY,PE,2026-10-27\n")
        self.assertEqual(nearest_banknifty_option_expiry(catalog, date(2026, 10, 5)), EXPIRY)

    def test_archive_keeps_only_sanitized_receipt_time_observation(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            with self.assertRaisesRegex(ValueError, "Only receipt-time"):
                save_option_chain_analysis(database,
                    analyze_option_chain(chain(), EXPIRY, NOW, source_kind="synthetic"))
            save_option_chain_analysis(database, analyze_option_chain(chain(), EXPIRY, NOW))
            with closing(sqlite3.connect(database)) as connection:
                event_type, payload = connection.execute(
                    "SELECT event_type, payload FROM events").fetchone()
            self.assertEqual(event_type, "option_chain_snapshot")
            self.assertIn('"exchange_at":null', payload)
            self.assertIn('"freshness":"unverified_exchange_time_absent"', payload)
            self.assertNotIn("fixture-token", payload)
            self.assertEqual(count_option_chain_snapshots(database), 1)
            destination = Path(folder) / "chain.csv"
            self.assertEqual(export_option_chain_csv(database, destination), 4)
            self.assertIn("BANKNIFTY26O2755000CE", destination.read_text(encoding="utf-8-sig"))
            with self.assertRaises(ValueError):
                export_option_chain_csv(database, database)

    def test_archive_rechecks_direct_option_analysis_before_excel_export(self):
        good = analyze_option_chain(chain(), EXPIRY, NOW)
        forged_contract = replace(good.contracts[0], trading_symbol="=HYPERLINK(\"bad\")")
        invalid = (
            replace(good, contracts=(forged_contract, *good.contracts[1:])),
            replace(good, call_oi=good.call_oi + 1),
            replace(good, received_at_utc=NOW.replace(tzinfo=None)),
        )
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            for analysis in invalid:
                with self.subTest(analysis=analysis), self.assertRaises(ValueError):
                    save_option_chain_analysis(database, analysis)
                self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()

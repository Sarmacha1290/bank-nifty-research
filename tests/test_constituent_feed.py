"""Bank feed tests cover source mapping, timestamp safety and durable separation."""
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.constituents import resolve_bank_equities
from banknifty.alignment import audit_latest_recorded_session
from banknifty.equity_feed import parse_equity_ticks
from banknifty.equity_history import (count_equity_ticks, equity_symbol_counts,
                                      equity_coverage, export_equity_ticks_csv,
                                      save_equity_ticks)
from banknifty.groww import DataAccessError, IST
from collect_groww import collect


NOW = datetime(2026, 10, 5, 10, 0, tzinfo=IST)
INDEX = {"exchange": "NSE", "segment": "CASH", "exchange_token": "BANKNIFTY",
         "groww_symbol": "NSE-BANKNIFTY"}
HEADER = "exchange,segment,instrument_type,series,trading_symbol,groww_symbol,exchange_token\n"
CATALOG = HEADER + "NSE,CASH,EQ,EQ,HDFCBANK,NSE-HDFCBANK,1333\nNSE,CASH,EQ,EQ,ICICIBANK,NSE-ICICIBANK,4963\n"


def equity_payload(at=NOW, price=100.0):
    return {"NSE": {"CASH": {
        "1333": {"tsInMillis": int(at.timestamp() * 1000), "ltp": price}}}}


def index_payload(at=NOW):
    return {"NSE": {"CASH": {"BANKNIFTY": {
        "tsInMillis": int(at.timestamp() * 1000), "value": 55000.0}}}}


class FakeBothFeeds:
    def __init__(self):
        self.index_calls = 0
        self.stock_subscribed = False
        self.stock_unsubscribed = False

    def subscribe_index_value(self, instruments):
        pass

    def unsubscribe_index_value(self, instruments):
        pass

    def subscribe_ltp(self, instruments):
        self.stock_subscribed = True
        self.stock_instruments = instruments

    def unsubscribe_ltp(self, instruments):
        self.stock_unsubscribed = True

    def get_index_value(self):
        self.index_calls += 1
        return index_payload()

    def get_ltp(self):
        return equity_payload()


class ConstituentFeedTests(unittest.TestCase):
    def test_installed_sdk_ltp_protobuf_shape_reaches_bank_parser(self):
        try:
            from growwapi import GrowwFeed
            from growwapi.groww.constants import FeedConstants
            from growwapi.groww.proto.stocks_socket_response_pb2 import StocksSocketResponseProtoDto
        except ImportError as exc:
            self.skipTest(f"Groww SDK unavailable: {exc}")
        encoded = StocksSocketResponseProtoDto()
        encoded.stockLivePrice.tsInMillis = float(NOW.timestamp() * 1000)
        encoded.stockLivePrice.ltp = 100.0
        feed = object.__new__(GrowwFeed)  # protobuf conversion only; no token or socket
        envelope = {}
        feed._generate_feed_response(encoded.SerializeToString(), {
            FeedConstants.FEED_TYPE: FeedConstants.LIVE_DATA,
            FeedConstants.EXCHANGE: "NSE", FeedConstants.SEGMENT: "CASH",
            FeedConstants.FEED_KEY: "1333",
        }, envelope)
        ticks, rejected = parse_equity_ticks(envelope[FeedConstants.LIVE_DATA],
            resolve_bank_equities(CATALOG, ("HDFCBANK",)), NOW)
        self.assertEqual((len(ticks), rejected), (1, 0))
        self.assertEqual(ticks[0].ltp, 100.0)

    def test_exact_equity_mapping_rejects_missing_or_ambiguous_catalog_rows(self):
        rows = resolve_bank_equities(CATALOG, ("HDFCBANK", "ICICIBANK"))
        self.assertEqual([row["exchange_token"] for row in rows], ["1333", "4963"])
        with self.assertRaises(DataAccessError):
            resolve_bank_equities(CATALOG, ("HDFCBANK", "SBIN"))
        with self.assertRaises(DataAccessError):
            resolve_bank_equities(CATALOG + CATALOG.splitlines()[1] + "\n", ("HDFCBANK",))
        with self.assertRaises(DataAccessError):
            resolve_bank_equities(CATALOG.replace("NSE-HDFCBANK", "NSE-WRONG"), ("HDFCBANK",))

    def test_timestamp_validation_and_missing_rows(self):
        bank = resolve_bank_equities(CATALOG, ("HDFCBANK", "ICICIBANK"))
        ticks, rejected = parse_equity_ticks(equity_payload(), bank, NOW + timedelta(seconds=2))
        self.assertEqual((len(ticks), rejected), (1, 0))
        self.assertEqual(ticks[0].symbol, "NSE-HDFCBANK")
        self.assertEqual(ticks[0].exchange_at, NOW)
        self.assertEqual(parse_equity_ticks({}, bank, NOW), ((), 0))
        self.assertEqual(parse_equity_ticks(equity_payload(NOW - timedelta(minutes=3)), bank, NOW)[1], 1)
        self.assertEqual(parse_equity_ticks(equity_payload(NOW + timedelta(seconds=6)), bank, NOW)[1], 1)
        self.assertEqual(parse_equity_ticks(equity_payload(price=True), bank, NOW)[1], 1)

    def test_equity_archive_is_separate_deduplicated_and_exportable(self):
        bank = resolve_bank_equities(CATALOG, ("HDFCBANK",))
        tick = parse_equity_ticks(equity_payload(), bank, NOW)[0][0]
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            self.assertEqual(save_equity_ticks(database, (tick,))["inserted"], 1)
            self.assertEqual(save_equity_ticks(database, (replace(tick, received_at=NOW + timedelta(seconds=1)),))["duplicates"], 1)
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                save_equity_ticks(database, (replace(tick, ltp=101.0),))
            self.assertEqual(count_equity_ticks(database), 1)
            self.assertEqual(equity_symbol_counts(database), {"NSE-HDFCBANK": 1})
            coverage = equity_coverage(database, ("NSE-HDFCBANK", "NSE-ICICIBANK"))
            self.assertEqual(coverage["NSE-HDFCBANK"]["count"], 1)
            self.assertEqual(coverage["NSE-ICICIBANK"]["count"], 0)
            destination = Path(folder) / "equities.csv"
            self.assertEqual(export_equity_ticks_csv(database, destination), 1)
            self.assertIn("NSE-HDFCBANK", destination.read_text(encoding="utf-8-sig"))
            with self.assertRaises(ValueError):
                export_equity_ticks_csv(database, database)

    def test_optional_joint_capture_preserves_raw_separate_observations(self):
        bank = resolve_bank_equities(CATALOG, ("HDFCBANK", "ICICIBANK"))
        feed = FakeBothFeeds()
        elapsed = [0.0]
        with TemporaryDirectory() as folder:
            result = collect(feed, INDEX, Path(folder) / "market.sqlite3", duration_seconds=2,
                             equity_instruments=bank,
                             now=lambda: NOW + timedelta(seconds=elapsed[0]),
                             sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                             monotonic=lambda: elapsed[0])
            self.assertEqual((result["inserted"], result["equity_inserted"]), (1, 1))
            self.assertEqual(result["equity_duplicates"], 1)
            self.assertTrue(feed.stock_subscribed and feed.stock_unsubscribed)
            self.assertEqual(result["equity_status"], "partial_coverage")
            self.assertEqual((result["equity_symbols_with_updates"], result["equity_expected_symbols"]), (1, 2))
            self.assertEqual(count_equity_ticks(Path(folder) / "market.sqlite3"), 1)

    def test_closed_market_does_not_subscribe_banks(self):
        feed = FakeBothFeeds()
        bank = resolve_bank_equities(CATALOG, ("HDFCBANK",))
        with TemporaryDirectory() as folder:
            result = collect(feed, INDEX, Path(folder) / "market.sqlite3", duration_seconds=60,
                             equity_instruments=bank,
                             now=lambda: datetime(2026, 10, 4, 10, 0, tzinfo=IST))
            self.assertEqual(result["status"], "weekend")
            self.assertFalse(feed.stock_subscribed)

    def test_full_fourteen_bank_simulation_reaches_alignment_audit(self):
        from banknifty.nse_weights import load_reference_weights
        reference = Path(__file__).resolve().parent / "fixtures" / "synthetic-bank-weights.json"
        symbols = [row["symbol"] for row in load_reference_weights(reference)["constituents"]]
        catalog = HEADER + "".join(
            f"NSE,CASH,EQ,EQ,{symbol},NSE-{symbol},{10000 + number}\n"
            for number, symbol in enumerate(symbols))
        banks = resolve_bank_equities(catalog, symbols)

        class FullFeed(FakeBothFeeds):
            def get_ltp(self):
                return {"NSE": {"CASH": {
                    row["exchange_token"]: {"tsInMillis": int(NOW.timestamp() * 1000), "ltp": 100.0}
                    for row in banks}}}

        elapsed = [0.0]
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            result = collect(FullFeed(), INDEX, database, duration_seconds=2,
                             equity_instruments=banks,
                             now=lambda: NOW + timedelta(seconds=elapsed[0]),
                             sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                             monotonic=lambda: elapsed[0])
            alignment = audit_latest_recorded_session(database, reference)
            self.assertEqual(result["equity_inserted"], 14)
            self.assertEqual(result["equity_status"], "completed")
            self.assertEqual(result["equity_symbols_with_updates"], 14)
            self.assertEqual(alignment["observed_index_minutes"], 1)
            self.assertEqual(alignment["all_banks_fresh_minutes"], 1)


if __name__ == "__main__":
    unittest.main()

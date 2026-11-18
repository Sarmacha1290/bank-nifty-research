"""Only time-checked, top-five order books may reach the archive."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import nan
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.depth_feed import (DepthTick, describe_depth,
                                   estimate_visible_book_walk, parse_depth_ticks)
from banknifty.depth_history import (count_depth_ticks, export_depth_ticks_csv,
                                     latest_depth_by_symbol, load_recent_depth_ticks,
                                     save_depth_ticks)
from banknifty.depth_spikes import detect_depth_spikes
from banknifty.groww import IST
from collect_groww import collect


NOW = datetime(2026, 10, 5, 10, 0, tzinfo=IST)
BANKS = ({"exchange": "NSE", "segment": "CASH", "exchange_token": "123",
          "groww_symbol": "NSE-A", "trading_symbol": "A"},)
INDEX = {"exchange": "NSE", "segment": "CASH", "exchange_token": "BANKNIFTY",
         "groww_symbol": "NSE-BANKNIFTY"}


def payload(at=NOW, buy=100, sell=50):
    return {"NSE": {"CASH": {"123": {
        "tsInMillis": int(at.timestamp() * 1000),
        "buyBook": {"2": {"price": 99, "qty": 1}, "1": {"price": 100, "qty": buy}},
        "sellBook": {"1": {"price": 101, "qty": sell}}
    }}}}


class DepthTests(unittest.TestCase):
    def test_microprice_and_imbalance_describe_waiting_orders(self):
        row = parse_depth_ticks(payload(buy=100, sell=50), BANKS, NOW)[0][0]
        report = describe_depth(row)
        self.assertEqual(report["spread"], 1)
        self.assertAlmostEqual(report["microprice"], (101 * 100 + 100 * 50) / 150)
        self.assertAlmostEqual(report["top_level_imbalance"], 1 / 3)
        self.assertIn("not_executed", report["interpretation"])

    def test_visible_book_walk_never_claims_unseen_liquidity(self):
        row = parse_depth_ticks(payload(), BANKS, NOW)[0][0]
        buy = estimate_visible_book_walk(row, side="buy", shares=20)
        self.assertEqual(buy["average_price"], 101)
        self.assertEqual(buy["unfilled_shares"], 0)
        too_large = estimate_visible_book_walk(row, side="buy", shares=100)
        self.assertEqual(too_large["unfilled_shares"], 50)
        self.assertIsNone(too_large["average_price"])
        with self.assertRaisesRegex(ValueError, "ordered"):
            estimate_visible_book_walk(
                replace(row, sell_levels=((101.0, 10.0), (100.0, 10.0))),
                side="buy", shares=20)

    def test_depth_archive_rejects_invalid_direct_rows(self):
        valid = parse_depth_ticks(payload(), BANKS, NOW)[0][0]
        invalid = (
            replace(valid, symbol="=FORMULA"),
            replace(valid, received_at=NOW.replace(tzinfo=None)),
            replace(valid, buy_levels=((nan, 1.0),)),
            replace(valid, sell_levels=((101.0, -1.0),)),
            replace(valid, buy_levels=(), sell_levels=()),
            replace(valid, buy_levels=((100.0, 1.0),) * 6),
        )
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            for row in invalid:
                with self.subTest(row=row), self.assertRaises(ValueError):
                    save_depth_ticks(database, (row,))
                self.assertFalse(database.exists())

    def test_installed_sdk_depth_protobuf_shape_reaches_parser(self):
        try:
            from growwapi import GrowwFeed
            from growwapi.groww.constants import FeedConstants
            from growwapi.groww.proto.stocks_socket_response_pb2 import StocksSocketResponseProtoDto
        except ImportError as exc:
            self.skipTest(f"Groww SDK unavailable: {exc}")
        encoded = StocksSocketResponseProtoDto()
        encoded.stocksMarketDepth.tsInMillis = float(NOW.timestamp() * 1000)
        encoded.stocksMarketDepth.buyBook[1].price = 100.0
        encoded.stocksMarketDepth.buyBook[1].qty = 10.0
        encoded.stocksMarketDepth.sellBook[1].price = 101.0
        encoded.stocksMarketDepth.sellBook[1].qty = 20.0
        feed = object.__new__(GrowwFeed)  # conversion only; no token or socket
        envelope = {}
        feed._generate_feed_response(encoded.SerializeToString(), {
            FeedConstants.FEED_TYPE: FeedConstants.MARKET_DEPTH,
            FeedConstants.EXCHANGE: "NSE", FeedConstants.SEGMENT: "CASH",
            FeedConstants.FEED_KEY: "123",
        }, envelope)
        ticks, rejected = parse_depth_ticks(envelope[FeedConstants.MARKET_DEPTH], BANKS, NOW)
        self.assertEqual((len(ticks), rejected), (1, 0))
        self.assertEqual((ticks[0].best_bid, ticks[0].best_ask), (100.0, 101.0))

    def test_parse_checks_units_age_and_rank(self):
        ticks, rejected = parse_depth_ticks(payload(), BANKS, NOW)
        self.assertEqual((len(ticks), rejected), (1, 0))
        self.assertEqual((ticks[0].best_bid, ticks[0].buy_quantity), (100, 101))
        seconds = payload()
        seconds["NSE"]["CASH"]["123"]["tsInMillis"] = int(NOW.timestamp())
        self.assertEqual(parse_depth_ticks(seconds, BANKS, NOW)[1], 1)
        self.assertEqual(parse_depth_ticks(payload(NOW - timedelta(seconds=30)), BANKS, NOW)[1], 1)
        self.assertEqual(parse_depth_ticks(payload(NOW + timedelta(seconds=5)), BANKS, NOW)[1], 1)

    def test_depth_archive_and_export(self):
        tick = parse_depth_ticks(payload(), BANKS, NOW)[0][0]
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            self.assertEqual(save_depth_ticks(database, [tick])["inserted"], 1)
            self.assertEqual(save_depth_ticks(database, [replace(tick, received_at=NOW + timedelta(seconds=1))])["duplicates"], 1)
            with self.assertRaises(ValueError):
                save_depth_ticks(database, [replace(tick, buy_levels=((100.0, 999.0),))])
            self.assertEqual(count_depth_ticks(database), 1)
            self.assertEqual(latest_depth_by_symbol(database)["NSE-A"].sell_quantity, 50)
            self.assertEqual(len(load_recent_depth_ticks(database)), 1)
            target = Path(folder) / "depth.csv"
            self.assertEqual(export_depth_ticks_csv(database, target), 1)
            self.assertIn("Visible top five", target.read_text(encoding="utf-8-sig"))

    def test_spike_requires_prior_nearby_books(self):
        at = NOW.astimezone(timezone.utc)
        rows = [DepthTick("NSE-A", at + timedelta(seconds=i),
                          at + timedelta(seconds=i), ((100.0, 100.0),),
                          ((101.0, 50.0),)) for i in range(5)]
        rows.append(DepthTick("NSE-A", at + timedelta(seconds=5),
                              at + timedelta(seconds=5), ((100.0, 400.0),),
                              ((101.0, 50.0),)))
        self.assertEqual(detect_depth_spikes(rows)[0]["kind"], "top5_buy_depth_surge")
        self.assertEqual(detect_depth_spikes(rows[:-1]), [])
        self.assertEqual(detect_depth_spikes(rows[:-1] + [replace(rows[-1], exchange_at=at + timedelta(minutes=2))]), [])

    def test_joint_index_and_depth_capture(self):
        class Feed:
            subscribed = unsubscribed = False
            def subscribe_index_value(self, rows): pass
            def unsubscribe_index_value(self, rows): pass
            def subscribe_market_depth(self, rows): self.subscribed = True
            def unsubscribe_market_depth(self, rows): self.unsubscribed = True
            def get_index_value(self):
                return {"NSE": {"CASH": {"BANKNIFTY": {
                    "tsInMillis": int(NOW.timestamp() * 1000), "value": 55000}}}}
            def get_market_depth(self): return payload()
        feed = Feed()
        elapsed = [0.0]
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            result = collect(feed, INDEX, database, duration_seconds=2,
                depth_instruments=BANKS,
                now=lambda: NOW + timedelta(seconds=elapsed[0]),
                sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                monotonic=lambda: elapsed[0])
            self.assertEqual(result["depth_inserted"], 1)
            self.assertEqual(result["depth_status"], "completed")
            self.assertEqual(count_depth_ticks(database), 1)
            self.assertTrue(feed.subscribed and feed.unsubscribed)

    def test_all_fourteen_bank_books_are_separate_from_index(self):
        banks = tuple({"exchange": "NSE", "segment": "CASH", "exchange_token": str(1000 + i),
                       "groww_symbol": f"NSE-B{i}", "trading_symbol": f"B{i}"}
                      for i in range(14))
        class Feed:
            def subscribe_index_value(self, rows): pass
            def unsubscribe_index_value(self, rows): pass
            def subscribe_market_depth(self, rows): self.subscribed = len(rows)
            def unsubscribe_market_depth(self, rows): pass
            def get_index_value(self):
                return {"NSE": {"CASH": {"BANKNIFTY": {
                    "tsInMillis": int(NOW.timestamp() * 1000), "value": 55000}}}}
            def get_market_depth(self):
                return {"NSE": {"CASH": {row["exchange_token"]: {
                    "tsInMillis": int(NOW.timestamp() * 1000),
                    "buyBook": {"1": {"price": 100, "qty": 10}},
                    "sellBook": {"1": {"price": 101, "qty": 5}}}
                    for row in banks}}}
        feed, elapsed = Feed(), [0.0]
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            result = collect(feed, INDEX, database, duration_seconds=2,
                depth_instruments=banks,
                now=lambda: NOW + timedelta(seconds=elapsed[0]),
                sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                monotonic=lambda: elapsed[0])
            self.assertEqual((result["depth_inserted"], result["depth_symbols_with_updates"]), (14, 14))
            self.assertEqual(feed.subscribed, 14)
            self.assertEqual(len(latest_depth_by_symbol(database)), 14)

    def test_long_capture_reports_progress_without_token(self):
        class Feed:
            def subscribe_index_value(self, rows): pass
            def unsubscribe_index_value(self, rows): pass
            def get_index_value(self):
                return {"NSE": {"CASH": {"BANKNIFTY": {
                    "tsInMillis": int(NOW.timestamp() * 1000), "value": 55000}}}}
        elapsed, updates = [0.0], []
        with TemporaryDirectory() as folder:
            collect(Feed(), INDEX, Path(folder) / "market.sqlite3", duration_seconds=12,
                now=lambda: NOW + timedelta(seconds=elapsed[0]),
                sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                monotonic=lambda: elapsed[0], on_progress=updates.append)
        self.assertTrue(updates)
        self.assertEqual(updates[0]["inserted"], 1)


if __name__ == "__main__":
    unittest.main()

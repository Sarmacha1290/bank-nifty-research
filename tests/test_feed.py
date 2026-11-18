"""Groww feed timestamps must remain distinguishable from receipt times."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import nan
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.feed import IndexTick, parse_index_tick
from banknifty.groww_feed_security import GrowwFeedSecurityError
from banknifty.groww import IST
from banknifty.market_calendar import regular_market_status, regular_session_date
from banknifty.tick_history import count_index_ticks, save_index_tick
from collect_groww import collect


INSTRUMENT = {"exchange": "NSE", "segment": "CASH", "exchange_token": "BANKNIFTY",
              "groww_symbol": "NSE-BANKNIFTY"}
NOW = datetime(2026, 10, 5, 10, 0, tzinfo=IST)


def payload(at=NOW, value=55000.0):
    return {"NSE": {"CASH": {"BANKNIFTY": {
        "tsInMillis": int(at.timestamp() * 1000), "value": value}}}}


class FakeFeed:
    def __init__(self, values):
        self.values = iter(values)
        self.subscribed = False
        self.unsubscribed = False

    def subscribe_index_value(self, instruments):
        self.subscribed = True
        self.instruments = instruments

    def get_index_value(self):
        return next(self.values)

    def unsubscribe_index_value(self, instruments):
        self.unsubscribed = True


class FeedTests(unittest.TestCase):
    def test_archive_rejects_invalid_direct_writes_before_creating_database(self):
        good = IndexTick("NSE-BANKNIFTY", NOW, NOW + timedelta(seconds=1), 55000.0)
        invalid = (
            replace(good, symbol="DEMO-BANKNIFTY"),
            replace(good, exchange_at=NOW.replace(tzinfo=None)),
            replace(good, received_at=NOW.replace(tzinfo=None)),
            replace(good, value=nan),
            replace(good, value=-1),
            replace(good, value=True),
        )
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            for tick in invalid:
                with self.subTest(tick=tick), self.assertRaises(ValueError):
                    save_index_tick(database, tick)
                self.assertFalse(database.exists())

    def test_security_rejection_stops_before_polling(self):
        class RejectedFeed(FakeFeed):
            def subscribe_index_value(self, instruments):
                raise GrowwFeedSecurityError("unreviewed SDK")

            def get_index_value(self):
                raise AssertionError("Security rejection must prevent polling")

        with TemporaryDirectory() as folder:
            feed = RejectedFeed([])
            result = collect(feed, INSTRUMENT, Path(folder) / "market.sqlite3",
                             duration_seconds=1, now=lambda: NOW)
            self.assertEqual(result["status"], "security_blocked")
            self.assertFalse((Path(folder) / "market.sqlite3").exists())

    def test_installed_sdk_index_protobuf_shape_reaches_parser(self):
        try:
            from growwapi import GrowwFeed
            from growwapi.groww.constants import FeedConstants
            from growwapi.groww.proto.stocks_socket_response_pb2 import StocksSocketResponseProtoDto
        except ImportError as exc:
            self.skipTest(f"Groww SDK unavailable: {exc}")
        encoded = StocksSocketResponseProtoDto()
        encoded.stocksLiveIndices.tsInMillis = float(NOW.timestamp() * 1000)
        encoded.stocksLiveIndices.value = 55000.0
        feed = object.__new__(GrowwFeed)  # conversion only; no token or socket
        envelope = {}
        feed._generate_feed_response(encoded.SerializeToString(), {
            FeedConstants.FEED_TYPE: FeedConstants.LIVE_INDEX,
            FeedConstants.EXCHANGE: "NSE", FeedConstants.SEGMENT: "CASH",
            FeedConstants.FEED_KEY: "BANKNIFTY",
        }, envelope)
        tick = parse_index_tick(envelope[FeedConstants.LIVE_INDEX], INSTRUMENT, NOW)
        self.assertEqual(tick.value, 55000.0)
        self.assertEqual(tick.exchange_at, NOW.astimezone(timezone.utc))

    def test_sdk_subscribed_topic_without_first_message_is_missing_not_rejected(self):
        try:
            from growwapi import GrowwFeed
            from growwapi.groww.constants import FeedConstants
        except ImportError as exc:
            self.skipTest(f"Groww SDK unavailable: {exc}")
        feed = object.__new__(GrowwFeed)  # conversion only; no token or socket
        envelope = {}
        feed._generate_feed_response(None, {
            FeedConstants.FEED_TYPE: FeedConstants.LIVE_INDEX,
            FeedConstants.EXCHANGE: "NSE", FeedConstants.SEGMENT: "CASH",
            FeedConstants.FEED_KEY: "BANKNIFTY",
        }, envelope)
        self.assertIsNone(parse_index_tick(envelope[FeedConstants.LIVE_INDEX], INSTRUMENT, NOW))

        class WaitingFeed(FakeFeed):
            pass

        elapsed = [0.0]
        with TemporaryDirectory() as folder:
            result = collect(WaitingFeed([envelope[FeedConstants.LIVE_INDEX]] * 3),
                             INSTRUMENT, Path(folder) / "market.sqlite3",
                             duration_seconds=3,
                             now=lambda: NOW + timedelta(seconds=elapsed[0]),
                             sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                             monotonic=lambda: elapsed[0])
        self.assertEqual((result["status"], result["rejected"]), ("no_fresh_updates", 0))

    def test_timestamped_tick_and_stale_rejection(self):
        tick = parse_index_tick(payload(), INSTRUMENT, NOW + timedelta(seconds=2))
        self.assertEqual(tick.exchange_at, NOW.astimezone(timezone.utc))
        self.assertEqual(tick.received_at, (NOW + timedelta(seconds=2)).astimezone(timezone.utc))
        self.assertEqual(tick.value, 55000)
        self.assertIsNone(parse_index_tick({}, INSTRUMENT, NOW))
        with self.assertRaisesRegex(ValueError, "stale"):
            parse_index_tick(payload(NOW - timedelta(minutes=3)), INSTRUMENT, NOW)
        with self.assertRaisesRegex(ValueError, "future"):
            parse_index_tick(payload(NOW + timedelta(seconds=6)), INSTRUMENT, NOW)

    def test_archive_deduplicates_without_rewriting_or_mixing_candles(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            tick = parse_index_tick(payload(), INSTRUMENT, NOW)
            self.assertEqual(save_index_tick(database, tick), "inserted")
            self.assertEqual(save_index_tick(database, replace(tick, received_at=NOW + timedelta(seconds=1))), "duplicate")
            self.assertEqual(count_index_ticks(database), 1)
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                save_index_tick(database, replace(tick, value=55001))
            self.assertEqual(count_index_ticks(database), 1)

    def test_capture_only_stores_fresh_updates_during_market_hours(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            fake = FakeFeed([payload(), payload()])
            elapsed = [0.0]
            def sleep(seconds):
                elapsed[0] += seconds
            result = collect(fake, INSTRUMENT, database, duration_seconds=2,
                             now=lambda: NOW + timedelta(seconds=elapsed[0]),
                             sleep=sleep, monotonic=lambda: elapsed[0])
            self.assertEqual((result["inserted"], result["duplicates"]), (1, 1))
            self.assertEqual(count_index_ticks(database), 1)
            self.assertTrue(fake.subscribed and fake.unsubscribed)
            self.assertEqual(fake.instruments[0]["exchange_token"], "BANKNIFTY")

    def test_closed_market_never_subscribes(self):
        with TemporaryDirectory() as folder:
            fake = FakeFeed([])
            closed = datetime(2026, 10, 4, 10, 0, tzinfo=IST)
            result = collect(fake, INSTRUMENT, Path(folder) / "market.sqlite3",
                             duration_seconds=60, now=lambda: closed)
            self.assertEqual(result["status"], "weekend")
            self.assertFalse(fake.subscribed)

    def test_session_close_with_no_index_update_is_not_reported_complete(self):
        with TemporaryDirectory() as folder:
            elapsed = [0.0]
            closing = datetime(2026, 10, 5, 15, 29, 59, tzinfo=IST)
            result = collect(FakeFeed([{}]), INSTRUMENT,
                             Path(folder) / "market.sqlite3", duration_seconds=5,
                             now=lambda: closing + timedelta(seconds=elapsed[0]),
                             sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                             monotonic=lambda: elapsed[0])
            self.assertEqual(result["status"], "no_fresh_updates")
            self.assertEqual(result["inserted"], 0)

    def test_official_2026_holidays_and_unknown_year_fail_closed(self):
        self.assertEqual(regular_market_status(datetime(2026, 10, 5, 10, tzinfo=IST)), "regular_session")
        self.assertEqual(regular_market_status(datetime(2026, 10, 2, 10, tzinfo=IST)), "nse_holiday")
        self.assertEqual(regular_market_status(datetime(2026, 10, 20, 10, tzinfo=IST)), "nse_holiday")
        self.assertEqual(regular_market_status(datetime(2027, 10, 5, 10, tzinfo=IST)), "calendar_year_unverified")
        self.assertEqual(regular_session_date(datetime(2026, 1, 15).date())[1], "nse_holiday")

    def test_interruption_and_silence_preserve_partial_updates(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            elapsed = [0.0]
            def sleep(seconds):
                elapsed[0] += seconds
            fake = FakeFeed([payload()])
            result = collect(fake, INSTRUMENT, database, duration_seconds=5,
                             now=lambda: NOW + timedelta(seconds=elapsed[0]),
                             sleep=sleep, monotonic=lambda: elapsed[0])
            self.assertEqual(result["status"], "feed_interrupted")
            self.assertEqual(result["inserted"], 1)
            self.assertTrue(fake.unsubscribed)
            self.assertEqual(count_index_ticks(database), 1)
            elapsed[0] = 0
            fake = FakeFeed([payload(), payload(), payload()])
            result = collect(fake, INSTRUMENT, database, duration_seconds=10,
                             max_silence_seconds=2,
                             now=lambda: NOW + timedelta(seconds=elapsed[0]),
                             sleep=sleep, monotonic=lambda: elapsed[0])
            self.assertEqual(result["status"], "no_fresh_updates")
            self.assertEqual(result["duplicates"], 3)

    def test_user_can_stop_long_capture_without_losing_saved_tick(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            elapsed = [0.0]
            def sleep(seconds):
                elapsed[0] += seconds
            fake = FakeFeed([payload()])
            result = collect(fake, INSTRUMENT, database, duration_seconds=90 * 60,
                             now=lambda: NOW + timedelta(seconds=elapsed[0]),
                             sleep=sleep, monotonic=lambda: elapsed[0],
                             stop_requested=lambda: elapsed[0] >= 1)
            self.assertEqual(result["status"], "stopped_by_user")
            self.assertEqual(result["inserted"], 1)
            self.assertEqual(count_index_ticks(database), 1)
            self.assertTrue(fake.unsubscribed)

    def test_new_exchange_minute_triggers_paper_callback_once(self):
        with TemporaryDirectory() as folder:
            elapsed = [0.0]
            boundaries = []
            def sleep(seconds):
                elapsed[0] += seconds
            values = [payload()] * 60 + [payload(NOW + timedelta(minutes=1))]
            result = collect(FakeFeed(values), INSTRUMENT, Path(folder) / "market.sqlite3",
                             duration_seconds=61, now=lambda: NOW + timedelta(seconds=elapsed[0]),
                             sleep=sleep, monotonic=lambda: elapsed[0],
                             on_completed_minute=lambda boundary, generated_at: boundaries.append(boundary) or 0)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(boundaries), 2)
            self.assertEqual(boundaries[1] - boundaries[0], timedelta(minutes=1))


if __name__ == "__main__":
    unittest.main()

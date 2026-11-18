"""A Nifty fallback check must use only the current read-only index feed."""

from datetime import datetime, timedelta
import unittest

from banknifty.groww import IST, resolve_nifty50
from banknifty.nifty50_probe import probe_nifty50_feed


CATALOG = ("exchange,segment,instrument_type,trading_symbol,groww_symbol,exchange_token\n"
           "NSE,CASH,IDX,NIFTY,NSE-NIFTY,NIFTY\n"
           "NSE,CASH,IDX,BANKNIFTY,NSE-BANKNIFTY,BANKNIFTY\n")


class Feed:
    def __init__(self, payload=None, *, fail_subscribe=False):
        self.payload = payload
        self.fail_subscribe = fail_subscribe
        self.subscribed = []
        self.unsubscribed = []

    def subscribe_index_value(self, items):
        if self.fail_subscribe:
            raise RuntimeError("private provider detail")
        self.subscribed.append(items)

    def get_index_value(self):
        return self.payload

    def unsubscribe_index_value(self, items):
        self.unsubscribed.append(items)


class Nifty50ProbeTests(unittest.TestCase):
    def setUp(self):
        self.instrument = resolve_nifty50(CATALOG)
        self.at = datetime(2026, 10, 5, 9, 15, 2, tzinfo=IST)
        self.elapsed = [0.0]

    def run_probe(self, feed):
        return probe_nifty50_feed(feed, self.instrument, duration_seconds=1,
            now=lambda: self.at,
            monotonic=lambda: self.elapsed[0],
            sleep=lambda seconds: self.elapsed.__setitem__(0, self.elapsed[0] + seconds))

    def test_valid_repeated_update_is_only_one_unique_observation(self):
        exchange_at = self.at - timedelta(seconds=1)
        feed = Feed({"NSE": {"CASH": {"NIFTY": {
            "tsInMillis": exchange_at.timestamp() * 1000, "value": 25000.0}}}})
        result = self.run_probe(feed)
        self.assertEqual((result["status"], result["unique"], result["repeats"]),
                         ("completed", 1, 1))
        self.assertEqual(result["first_exchange_ist"], exchange_at.isoformat())
        self.assertEqual(feed.subscribed, feed.unsubscribed)
        self.assertEqual(feed.subscribed[0][0]["exchange_token"], "NIFTY")

    def test_empty_and_failed_subscriptions_do_not_claim_access(self):
        empty = self.run_probe(Feed({"NSE": {"CASH": {"NIFTY": None}}}))
        self.assertEqual((empty["status"], empty["unique"]), ("no_fresh_updates", 0))
        failure = self.run_probe(Feed(fail_subscribe=True))
        self.assertEqual((failure["status"], failure["unique"]), ("subscribe_failed", 0))

    def test_other_index_or_closed_market_is_rejected(self):
        wrong = dict(self.instrument, groww_symbol="NSE-BANKNIFTY")
        with self.assertRaises(ValueError):
            probe_nifty50_feed(Feed(), wrong)
        result = probe_nifty50_feed(Feed(), self.instrument,
            now=lambda: datetime(2026, 10, 4, 9, 15, tzinfo=IST))
        self.assertEqual(result["status"], "weekend")


if __name__ == "__main__":
    unittest.main()

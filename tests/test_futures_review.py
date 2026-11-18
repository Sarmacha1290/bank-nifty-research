"""Price/OI comparison requires recent, distinct last-trade timestamps."""

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.futures import FutureInstrument, parse_future_quote
from banknifty.futures_history import save_future_snapshot
from banknifty.futures_review import (compare_saved_future_quotes,
                                      review_latest_saved_future_pair)


INSTRUMENT = FutureInstrument("BANKNIFTY26OCTFUT", "NSE-BANKNIFTY-27Oct26-FUT",
                              "48699", date(2026, 10, 27))
WHEN = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)


def saved(when, *, price, oi, trade_age=2):
    snap = parse_future_quote({"status": "SUCCESS", "payload": {
        "last_price": price, "open_interest": oi, "volume": 100,
        "last_trade_time": int((when - timedelta(seconds=trade_age)).timestamp() * 1000)}},
        INSTRUMENT, when)
    row = asdict(snap)
    row["expiry_date"] = snap.expiry_date.isoformat()
    row["received_at_utc"] = snap.received_at_utc.isoformat()
    row["last_trade_at_utc"] = snap.last_trade_at_utc.isoformat()
    return row


class FuturesReviewTests(unittest.TestCase):
    def test_recent_distinct_trades_show_descriptive_quadrant(self):
        result = compare_saved_future_quotes(saved(WHEN, price=55000, oi=1000),
            saved(WHEN + timedelta(minutes=5), price=55040, oi=1100))
        self.assertEqual(result["status"], "recent_distinct_last_trades")
        self.assertEqual(result["comparison"]["descriptive_quadrant"], "price_up_oi_up")
        self.assertEqual(result["comparison"]["open_interest_change_reported"], 100)
        self.assertIn("oi_exchange_time", result["comparison"]["interpretation"])

    def test_stale_or_repeated_trade_suppresses_change(self):
        result = compare_saved_future_quotes(saved(WHEN, price=55000, oi=1000),
            saved(WHEN + timedelta(minutes=5), price=55040, oi=1100, trade_age=302))
        self.assertEqual(result["status"], "stale_or_repeated_last_trade")
        self.assertIsNone(result["comparison"])

    def test_actual_archive_path_still_reports_zero_without_two_quotes(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            self.assertEqual(review_latest_saved_future_pair(database)["status"],
                             "need_two_saved_quotes")
            for when, price, oi in ((WHEN, 55000, 1000),
                                    (WHEN + timedelta(minutes=5), 55040, 1100)):
                snap = parse_future_quote({"status": "SUCCESS", "payload": {
                    "last_price": price, "open_interest": oi,
                    "last_trade_time": int((when - timedelta(seconds=2)).timestamp() * 1000)}},
                    INSTRUMENT, when)
                save_future_snapshot(database, snap)
            review = review_latest_saved_future_pair(database)
            self.assertEqual(review["status"], "recent_distinct_last_trades")
            self.assertEqual(review["comparison"]["price_change_points"], 40)


if __name__ == "__main__":
    unittest.main()

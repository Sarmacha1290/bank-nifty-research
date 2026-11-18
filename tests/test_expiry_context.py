from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.expiry_context import (SOURCE, expiry_features,
    load_expiry_references, record_expiry_reference)
from banknifty.groww import IST


CATALOG = ("exchange,segment,underlying_symbol,instrument_type,expiry_date\n"
           "NSE,FNO,BANKNIFTY,CE,2026-10-27\n"
           "NSE,FNO,BANKNIFTY,PE,2026-10-27\n")


class ExpiryContextTests(unittest.TestCase):
    def test_reference_saved_with_provenance_and_cutoff(self):
        observed = datetime(2026, 10, 27, 9, 5, tzinfo=IST)
        with TemporaryDirectory() as folder:
            database = Path(folder) / "archive.sqlite3"
            saved = record_expiry_reference(database, CATALOG, observed)
            self.assertEqual(saved["source"], SOURCE)
            self.assertEqual(len(saved["catalog_sha256"]), 64)
            loaded = load_expiry_references(database)
            self.assertEqual(loaded, [saved])
            before = expiry_features(observed - timedelta(minutes=1), loaded)
            self.assertIsNone(before["is_expiry_day"])
            on_day = expiry_features(observed + timedelta(minutes=11), loaded)
            self.assertTrue(on_day["is_expiry_day"])
            self.assertEqual(on_day["calendar_days_to_expiry"], 0)
            next_day = expiry_features(observed + timedelta(days=1), loaded)
            self.assertIsNone(next_day["expiry_date"])

    def test_previous_trading_day_and_unobserved_history(self):
        observed = datetime(2026, 10, 26, 9, 5, tzinfo=IST)
        with TemporaryDirectory() as folder:
            reference = record_expiry_reference(Path(folder) / "a.sqlite3", CATALOG, observed)
        prior = expiry_features(observed + timedelta(minutes=70), (reference,))
        self.assertTrue(prior["is_pre_expiry_session"])
        self.assertFalse(prior["is_expiry_day"])
        self.assertEqual(prior["calendar_days_to_expiry"], 1)
        self.assertIsNone(expiry_features(datetime(2026, 10, 5, 10, 0, tzinfo=IST),
                                          (reference,))["expiry_date"])

    def test_catalogue_requires_both_option_sides(self):
        with TemporaryDirectory() as folder:
            with self.assertRaises(Exception):
                record_expiry_reference(Path(folder) / "a.sqlite3",
                    CATALOG.splitlines()[0] + "\n" + CATALOG.splitlines()[1],
                    datetime(2026, 10, 5, 9, 0, tzinfo=IST))


if __name__ == "__main__":
    unittest.main()

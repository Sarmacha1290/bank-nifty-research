import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from banknifty.institutional_flows import (
    parse_normalized_csv, parse_nse_download_csv, read_staged, stage_file,
)


CSV = (b"date,category,buy_crore,sell_crore,net_crore\n"
       b"2026-10-01,FII/FPI,100.00,120.00,-20.00\n"
       b"2026-10-01,DII,130.00,110.00,20.00\n")
OBSERVED = datetime(2026, 10, 4, 8, tzinfo=timezone.utc)
NSE_CSV = ('"CATEGORY \n","DATE \n","BUY VALUE \n(₹ Crores)",'
           '"SELL VALUE \n(₹ Crores)","NET VALUE \n(₹ Crores)"\n'
           '"DII","01-Oct-2026","24,077.65","14,444.02","9,633.63"\n'
           '"FII/FPI","01-Oct-2026","11,210.36","20,370.35","-9,159.99"\n').encode("utf-8")


class InstitutionalFlowsTests(unittest.TestCase):
    def test_download_layout_and_cash_scope(self):
        rows = parse_nse_download_csv(NSE_CSV, scope="nse_cash", observed_at=OBSERVED)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].day, date(2026, 10, 1))
        self.assertEqual(str(rows[1].net_crore), "-9159.99")
        with self.assertRaisesRegex(ValueError, "NSE flow headers"):
            parse_nse_download_csv(CSV, scope="nse_cash", observed_at=OBSERVED)
        with self.assertRaisesRegex(ValueError, "net must equal"):
            parse_nse_download_csv(NSE_CSV.replace(b"-9,159.99", b"-9,159.89"),
                                   scope="nse_cash", observed_at=OBSERVED)

    def test_nse_download_stages_without_forecast_release(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "download.csv"
            database = Path(folder) / "market.sqlite3"
            source.write_bytes(NSE_CSV)
            result = stage_file(database, source, scope="nse_cash",
                                observed_at=OBSERVED, file_format="nse_download")
            self.assertEqual(result["file_format"], "nse_download")
            rows = read_staged(database, scope="nse_cash",
                               report_date=date(2026, 10, 1), decision_at=OBSERVED)
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(not row["forecast_eligible"] for row in rows))

    def test_scope_and_math_are_not_guessed(self):
        with self.assertRaisesRegex(ValueError, "scope"):
            parse_normalized_csv(CSV, scope="bank_nifty", observed_at=OBSERVED)
        with self.assertRaisesRegex(ValueError, "net must equal"):
            parse_normalized_csv(CSV.replace(b"-20.00", b"-19.00"),
                                 scope="nse_cash", observed_at=OBSERVED)
        with self.assertRaisesRegex(ValueError, "one FII/FPI"):
            parse_normalized_csv(CSV.replace(b"DII", b"FII/FPI"),
                                 scope="nse_cash", observed_at=OBSERVED)
        with self.assertRaisesRegex(ValueError, "headers"):
            parse_normalized_csv(CSV.replace(b"buy_crore", b"buy_value"),
                                 scope="nse_cash", observed_at=OBSERVED)

    def test_import_is_unverified_and_cutoff_gated(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "manual.csv"
            database = Path(folder) / "market.sqlite3"
            source.write_bytes(CSV)
            result = stage_file(database, source, scope="nse_cash", observed_at=OBSERVED)
            self.assertEqual(result["authenticity"], "unverified_manual")
            self.assertEqual(read_staged(database, scope="nse_cash",
                                         report_date=date(2026, 10, 1),
                                         decision_at=OBSERVED - timedelta(seconds=1)), ())
            self.assertEqual(read_staged(database, scope="nse_bse_msei_cash",
                                         report_date=date(2026, 10, 1),
                                         decision_at=OBSERVED + timedelta(seconds=1)), ())
            rows = read_staged(database, scope="nse_cash",
                               report_date=date(2026, 10, 1),
                               decision_at=OBSERVED)
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(not row["forecast_eligible"] for row in rows))
            stage_file(database, source, scope="nse_cash",
                       observed_at=OBSERVED + timedelta(hours=1))
            self.assertEqual(read_staged(database, scope="nse_cash",
                                         report_date=date(2026, 10, 1),
                                         decision_at=OBSERVED), rows)

    def test_conflict_rolls_back_entire_report(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "manual.csv"
            database = Path(folder) / "market.sqlite3"
            source.write_bytes(CSV)
            stage_file(database, source, scope="nse_cash", observed_at=OBSERVED)
            source.write_bytes(CSV.replace(b"130.00,110.00,20.00",
                                           b"131.00,110.00,21.00"))
            with self.assertRaises(ValueError):
                stage_file(database, source, scope="nse_cash", observed_at=OBSERVED)
            rows = read_staged(database, scope="nse_cash",
                               report_date=date(2026, 10, 1), decision_at=OBSERVED)
            self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()

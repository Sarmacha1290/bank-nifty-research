import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from banknifty.nse_weights import load_reference_weights, parse_nifty_bank_report


SOURCE = "https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataSep2026.zip"
ROOT = Path(__file__).resolve().parents[1]


class NSEWeightTests(unittest.TestCase):
    def test_saved_synthetic_fixture_is_full_and_explicitly_not_live(self):
        report = load_reference_weights(Path(__file__).resolve().parent / "fixtures" / "synthetic-bank-weights.json")
        self.assertEqual(report["as_of_date"], "2026-09-30")
        self.assertTrue(report["reference_only"])
        self.assertEqual(len(report["constituents"]), 14)
        self.assertAlmostEqual(sum(row["weight_percent"] for row in report["constituents"]), 100.0)
        self.assertEqual(next(row["weight_percent"] for row in report["constituents"]
                              if row["symbol"] == "MOCKBANK01"), 9.0)
        self.assertEqual(report["source_pdf_sha256"],
                         "f" * 64)

    def test_parser_rejects_partial_weight_table(self):
        text = "\n".join(("Constituents of NIFTY Bank", "September 30, 2026",
                          "BANKA Bank A Financial Services 100.00 12345 60.00",
                          "BANKB Bank B Financial Services 100.00 23456 20.00"))
        with self.assertRaisesRegex(ValueError, "sum to 100"):
            parse_nifty_bank_report(text, source_url=SOURCE, source_pdf_sha256="a" * 64,
                                    retrieved_at=datetime.now(timezone.utc))

    def test_saved_reference_cannot_be_relabelled_as_live(self):
        original = load_reference_weights(Path(__file__).resolve().parent / "fixtures" / "synthetic-bank-weights.json")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid.json"
            original["reference_only"] = False
            path.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "reference-only"):
                load_reference_weights(path)

    def test_saved_reference_rejects_unsafe_symbol(self):
        original = load_reference_weights(Path(__file__).resolve().parent / "fixtures" / "synthetic-bank-weights.json")
        original["constituents"][0]["symbol"] = "=1+1"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid constituent symbol"):
                load_reference_weights(path)


if __name__ == "__main__":
    unittest.main()

import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from banknifty.groww import IST

from banknifty.agents.index_contribution import (
    PricePair, Weight, WeightSnapshot, calculate_contributions,
    describe_constituent_breadth, describe_weighted_ma_breadth,
)
from banknifty.core import Candle


BASE = datetime(2026, 10, 5, 10, 0, tzinfo=IST)
CURRENT = BASE + timedelta(minutes=15)


def example():
    weights = WeightSnapshot("DEMO-BANKNIFTY", BASE - timedelta(days=1),
                             BASE + timedelta(days=1), BASE - timedelta(hours=1),
                             "synthetic", "synthetic:two-bank-test",
                             (Weight("DEMO-A", 60), Weight("DEMO-B", 40)))
    index = PricePair("DEMO-BANKNIFTY", BASE, CURRENT, 50000, 52050, "synthetic")
    prices = [PricePair("DEMO-A", BASE, CURRENT, 100, 110, "synthetic"),
              PricePair("DEMO-B", BASE, CURRENT, 100, 95, "synthetic")]
    return weights, index, prices


class IndexContributionTests(unittest.TestCase):
    def test_weighted_ma_breadth_requires_every_bank_and_effective_weights(self):
        weights, _, _ = example()
        histories = {}
        for symbol, rising in (("DEMO-A", True), ("DEMO-B", False)):
            rows = []
            for minute in range(20):
                start = BASE + timedelta(minutes=minute)
                price = 100 + (minute if rising else -minute)
                rows.append(Candle(symbol, "equity", start,
                                   start + timedelta(minutes=1),
                                   price, price + 1, price - 1, price,
                                   "synthetic"))
            histories[symbol] = rows
        cutoff = BASE + timedelta(minutes=20)
        report = describe_weighted_ma_breadth(weights, histories, cutoff)
        self.assertEqual(report["banks_above_ma"], 1)
        self.assertEqual(report["weight_above_ma_percent"], 60)
        with self.assertRaisesRegex(ValueError, "Every weighted bank"):
            describe_weighted_ma_breadth(weights, {"DEMO-A": histories["DEMO-A"]}, cutoff)
        with self.assertRaisesRegex(ValueError, "not known"):
            describe_weighted_ma_breadth(
                replace(weights, known_at=cutoff + timedelta(seconds=1)),
                histories, cutoff)

    def test_weighted_returns_and_residual_are_explicit(self):
        result = calculate_contributions(*example())
        self.assertAlmostEqual(result.weighted_return_percent, 4.0)
        self.assertAlmostEqual(result.estimated_index_point_change, 2000.0)
        self.assertAlmostEqual(result.observed_index_point_change, 2050.0)
        self.assertAlmostEqual(result.residual_percentage_points, 0.1)
        self.assertEqual(result.rows[0].symbol, "DEMO-A")
        self.assertEqual(result.rows[-1].symbol, "DEMO-B")
        self.assertEqual(result.status, "synthetic_demo")
        breadth = describe_constituent_breadth(result)
        self.assertEqual((breadth["advancing_banks"], breadth["declining_banks"]), (1, 1))
        self.assertEqual(breadth["advancing_weight_percent"], 60)
        self.assertAlmostEqual(breadth["weight_concentration_hhi"], 0.52)

    def test_partial_factsheet_weights_are_refused(self):
        weights, _, _ = example()
        with self.assertRaisesRegex(ValueError, "sum to 100"):
            replace(weights, weights=(Weight("DEMO-A", 60), Weight("DEMO-B", 20)))

    def test_future_or_expired_weights_are_refused(self):
        weights, index, prices = example()
        with self.assertRaisesRegex(ValueError, "not known"):
            calculate_contributions(replace(weights, known_at=BASE + timedelta(seconds=1)), index, prices)
        with self.assertRaisesRegex(ValueError, "not effective"):
            calculate_contributions(replace(weights, valid_until=CURRENT), index, prices)

    def test_missing_or_misaligned_constituents_are_refused(self):
        weights, index, prices = example()
        with self.assertRaisesRegex(ValueError, "Every weighted constituent"):
            calculate_contributions(weights, index, prices[:1])
        with self.assertRaisesRegex(ValueError, "timestamps must match"):
            calculate_contributions(weights, index,
                                    [prices[0], replace(prices[1], current_at=CURRENT + timedelta(seconds=1))])

    def test_official_source_claim_is_structural_only_and_requires_https(self):
        weights, index, prices = example()
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            replace(weights, source_kind="official_nse_indices", source_uri="http://example.com/weights")
        claimed = replace(weights, source_kind="official_nse_indices",
                          source_uri="https://www.niftyindices.com/Factsheet/example.csv")
        with self.assertRaisesRegex(ValueError, "cannot use synthetic prices"):
            calculate_contributions(claimed, index, prices)


if __name__ == "__main__":
    unittest.main()

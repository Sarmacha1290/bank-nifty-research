import tempfile
from pathlib import Path
import unittest

from banknifty.training_readiness import assess_training_readiness


class TrainingReadinessTests(unittest.TestCase):
    def test_empty_archive_never_claims_a_trained_model(self):
        with tempfile.TemporaryDirectory() as folder:
            report = assess_training_readiness(Path(folder) / "empty.sqlite3")
        self.assertEqual(report["nse_daily_rows"], 0)
        self.assertEqual(report["complete_sampled_sessions"], 0)
        self.assertEqual(report["trained_forecast_models"], 0)
        self.assertFalse(report["intraday_model_ready"])


if __name__ == "__main__":
    unittest.main()

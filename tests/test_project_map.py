"""The visual map must report real role states and missing data honestly."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.project_map import build_project_map_data, generate_project_map


ROOT = Path(__file__).resolve().parents[1]


class ProjectMapTests(unittest.TestCase):
    def test_current_map_has_fourteen_roles_and_no_training_claim(self):
        data = build_project_map_data(ROOT)
        self.assertEqual(len(data["roles"]), 14)
        self.assertEqual((data["prototype_roles"], data["planned_roles"]), (11, 3))
        self.assertFalse(data["forecast_models_implemented"])
        self.assertIsInstance(data["archive"]["index_updates"], int)
        self.assertIsInstance(data["archive"]["rest_price_samples"], int)
        self.assertEqual(len(data["phases"]), 6)

    def test_missing_archive_is_zero_but_corrupted_archive_is_unavailable(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "project.json").write_text(json.dumps({"version": "fixture",
                "forecast_models_implemented": False}), encoding="utf-8")
            (root / "AGENT_ROLES.md").write_text(
                (ROOT / "AGENT_ROLES.md").read_text(encoding="utf-8"), encoding="utf-8")
            empty = build_project_map_data(root)
            self.assertEqual(empty["archive"]["index_updates"], 0)
            self.assertEqual(empty["archive"]["rest_price_samples"], 0)
            (root / "data").mkdir()
            (root / "data" / "market-data.sqlite3").write_bytes(b"not a SQLite database")
            damaged = build_project_map_data(root)
            self.assertIsNone(damaged["archive"]["index_updates"])
            self.assertIsNone(damaged["archive"]["rest_price_samples"])

    def test_standalone_map_embeds_data_without_external_scripts(self):
        with TemporaryDirectory() as folder:
            target = generate_project_map(ROOT, Path(folder) / "map.html")
            html = target.read_text(encoding="utf-8")
        self.assertIn("Bank Nifty project map", html)
        version = json.loads((ROOT / "project.json").read_text(encoding="utf-8"))["version"]
        self.assertIn(f'"version": "{version}"', html)
        self.assertIn("Fourteen software roles", html)
        self.assertIn("REST receipt-time Bank Nifty price samples", html)
        self.assertNotIn("<script src=", html)
        self.assertNotIn("ACCESS_TOKEN", html)


if __name__ == "__main__":
    unittest.main()

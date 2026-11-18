from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from banknifty.core import validate_series
from banknifty.workspace import AGENTS, read_connection_status, synthetic_session


class WorkspaceTests(unittest.TestCase):
    def test_synthetic_prices_are_repeatable_complete_and_labelled(self):
        session, candles = synthetic_session()
        self.assertEqual(candles, synthetic_session()[1])
        self.assertEqual(len(validate_series(candles, candles[-1].end)), 180)
        self.assertEqual(candles[0].start, session.open)
        self.assertLess(candles[-1].end, session.close)
        self.assertEqual({c.source for c in candles}, {"synthetic"})
        self.assertTrue(all(c.volume is None and c.open_interest is None for c in candles))

    def test_all_roles_are_visible_without_claiming_they_are_ready(self):
        self.assertEqual(len(AGENTS), 14)
        self.assertEqual(sum(a[1] == "Planned" for a in AGENTS), 3)
        self.assertEqual(sum(a[1] == "Prototype" for a in AGENTS), 11)
        self.assertEqual(AGENTS[0][0], "Data Quality")

    def test_saved_access_check_does_not_imply_live_connectivity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "connection-status.json"
            self.assertEqual(read_connection_status(folder)["state"], "not_checked")
            path.write_text(json.dumps({"connected": True, "checked_at": "2026-01-01T10:00:00+00:00", "token": "DO-NOT-DISPLAY"}), encoding="utf-8")
            result = read_connection_status(folder)
            self.assertEqual(result["state"], "passed")
            self.assertIn("unverified", result["detail"])
            self.assertNotIn("DO-NOT-DISPLAY", json.dumps(result))
            for content in ("{broken", "[]", json.dumps({"connected": True, "checked_at": "2026-01-01T10:00:00"}), json.dumps({"connected": "yes", "checked_at": "2026-01-01T10:00:00Z"}), json.dumps({"connected": True, "checked_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    self.assertEqual(read_connection_status(folder)["state"], "unreadable")


if __name__ == "__main__":
    unittest.main()

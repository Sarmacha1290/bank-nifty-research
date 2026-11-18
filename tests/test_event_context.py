from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from banknifty.event_context import (describe_event_window, load_known_events,
                                      parse_event_csv, stage_event_csv)


IST = timezone(timedelta(hours=5, minutes=30))
CSV = ("kind,subject,title,source_url,published_at,event_at\n"
       "policy_event,RBI,Policy statement,https://www.rbi.org.in/example,"
       "2026-10-04T08:00:00+05:30,2026-10-05T10:30:00+05:30\n").encode()


class EventContextTests(unittest.TestCase):
    def test_first_seen_prevents_backfilled_event_leakage(self):
        first_seen = datetime(2026, 10, 5, 9, 0, tzinfo=IST)
        with TemporaryDirectory() as folder:
            source = Path(folder) / "events.csv"
            database = Path(folder) / "market.sqlite3"
            source.write_bytes(CSV)
            self.assertEqual(stage_event_csv(database, source, first_seen)["inserted"], 1)
            self.assertEqual(stage_event_csv(database, source,
                first_seen + timedelta(hours=2))["inserted"], 0)
            self.assertEqual(load_known_events(database, first_seen - timedelta(seconds=1)), [])
            known = load_known_events(database, first_seen)
            self.assertEqual(len(known), 1)
            before = describe_event_window(first_seen - timedelta(seconds=1), known)
            self.assertEqual(before["source_status"], "no_event_source")
            self.assertIsNone(before["known_upcoming_events"])
            at_ten = describe_event_window(datetime(2026, 10, 5, 10, 0, tzinfo=IST), known)
            self.assertEqual(at_ten["known_upcoming_events"], 1)
            self.assertEqual(at_ten["known_recent_macro_events"], 0)
            self.assertFalse(at_ten["forecast_eligible"])

    def test_unverified_input_must_be_explicit_and_bounded(self):
        observed = datetime(2026, 10, 5, 9, 0, tzinfo=IST)
        with self.assertRaisesRegex(ValueError, "publication"):
            parse_event_csv(CSV.replace(b"2026-10-04T08:00", b"2026-10-06T08:00"), observed)
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            parse_event_csv(CSV.replace(b"https://", b"http://"), observed)
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            parse_event_csv(CSV.replace(b"/example", b"/example?api_key=secret"), observed)
        with self.assertRaisesRegex(ValueError, "headers"):
            parse_event_csv(CSV.replace(b"source_url", b"source"), observed)
        with self.assertRaisesRegex(ValueError, "size"):
            parse_event_csv(b"x" * 512_001, observed)


if __name__ == "__main__":
    unittest.main()

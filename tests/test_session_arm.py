"""Pre-open arming never starts on holidays or outside the short window."""
from datetime import datetime, timedelta
import unittest

from banknifty.groww import IST
from banknifty.session_arm import armable_open, wait_until


class ClockEvent:
    def __init__(self, current, cancel=False):
        self.current = current
        self.cancel = cancel
        self.waits = 0

    def is_set(self):
        return self.cancel

    def wait(self, seconds):
        self.waits += 1
        self.current += timedelta(seconds=seconds)


class SessionArmTests(unittest.TestCase):
    def test_regular_preopen_only(self):
        open_at = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
        self.assertEqual(armable_open(datetime(2026, 10, 5, 9, 0, tzinfo=IST)), open_at)
        self.assertEqual(armable_open(datetime(2026, 10, 5, 9, 14, tzinfo=IST)), open_at)
        for moment in (datetime(2026, 10, 4, 9, 5, tzinfo=IST),
                       datetime(2026, 10, 5, 8, 59, tzinfo=IST),
                       datetime(2026, 10, 5, 9, 15, tzinfo=IST),
                       datetime(2026, 10, 2, 9, 5, tzinfo=IST)):
            with self.assertRaises(ValueError):
                armable_open(moment)

    def test_wait_reaches_target_or_cancels(self):
        start = datetime(2026, 10, 5, 9, 13, tzinfo=IST)
        target = start + timedelta(seconds=12)
        event = ClockEvent(start)
        self.assertTrue(wait_until(target, event, now=lambda: event.current))
        self.assertEqual(event.current, target)
        self.assertEqual(event.waits, 3)
        cancelled = ClockEvent(start, cancel=True)
        self.assertFalse(wait_until(target, cancelled, now=lambda: cancelled.current))
        self.assertEqual(cancelled.waits, 0)


if __name__ == "__main__":
    unittest.main()

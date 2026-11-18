import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from banknifty.feed import IndexTick
from banknifty.market_activity import BankQuote
from banknifty.market_activity_history import count_bank_quotes, save_bank_quotes
from banknifty.parallel_capture import run_parallel_captures
from banknifty.tick_history import count_index_ticks, save_index_tick


class ParallelCaptureTests(unittest.TestCase):
    def test_index_and_activity_both_complete(self):
        ready = threading.Event()
        def activity(cancelled):
            ready.set()
            return {"status": "completed", "saved": 14}
        def index():
            self.assertTrue(ready.wait(1))
            return {"status": "completed", "inserted": 10}
        self.assertEqual(run_parallel_captures(index, activity),
                         ({"status": "completed", "inserted": 10},
                          {"status": "completed", "saved": 14}))

    def test_activity_failure_does_not_mask_index(self):
        def bad_activity(cancelled):
            raise RuntimeError("quote failure")
        result, quote = run_parallel_captures(lambda: {"status": "completed"}, bad_activity)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(quote["status"], "activity_error")
        self.assertIsNone(quote["saved"])
        self.assertEqual(quote["error_type"], "RuntimeError")

    def test_index_completion_requests_activity_stop(self):
        ready = threading.Event()
        def activity(cancelled):
            ready.set()
            while not cancelled():
                threading.Event().wait(.001)
            return {"status": "stopped"}
        def index():
            self.assertTrue(ready.wait(1))
            return "done"
        self.assertEqual(run_parallel_captures(index, activity, join_timeout=1),
                         ("done", {"status": "stopped"}))

    def test_slow_shutdown_never_returns_with_worker_still_running(self):
        ready = threading.Event()
        release = threading.Event()
        notices = []
        def activity(cancelled):
            ready.set()
            self.assertTrue(release.wait(1))
            return {"status": "stopped", "saved": 7}
        def index():
            self.assertTrue(ready.wait(1))
            threading.Timer(.05, release.set).start()
            return "index_done"
        result = run_parallel_captures(index, activity, join_timeout=.001,
                                       on_slow_shutdown=lambda: notices.append("waiting"))
        self.assertEqual(result, ("index_done", {"status": "stopped", "saved": 7}))
        self.assertEqual(notices, ["waiting"])

    def test_parallel_writes_keep_both_archives_intact(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "market.sqlite3"
            at = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
            barrier = threading.Barrier(2)
            def activity(cancelled):
                barrier.wait(timeout=2)
                save_bank_quotes(database, "sweep-1",
                    [BankQuote("NSE-BANK", at, 100, 10, 20, 200.0)])
                return {"status": "completed", "saved": 1}
            def index():
                barrier.wait(timeout=2)
                save_index_tick(database, IndexTick("NSE-BANKNIFTY", at, at, 55000.0))
                return {"status": "completed", "inserted": 1}
            result, quote = run_parallel_captures(index, activity)
            self.assertEqual((result["inserted"], quote["saved"]), (1, 1))
            self.assertEqual(count_index_ticks(database), 1)
            self.assertEqual(count_bank_quotes(database), 1)


if __name__ == "__main__":
    unittest.main()

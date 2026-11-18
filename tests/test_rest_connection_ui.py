"""Close waits for the REST worker; essential controls fit a small laptop window."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from connect_groww import ConnectionWindow
from dashboard import Dashboard


class RestConnectionUITests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.child = tk.Toplevel(self.root)
        self.window = ConnectionWindow(self.child, Path(self.temp.name))
        def cleanup():
            try:
                self.root.destroy()
            except tk.TclError:
                pass
        self.addCleanup(cleanup)

    def tick_until(self, predicate, seconds=2):
        end = time.monotonic() + seconds
        while not predicate() and time.monotonic() < end:
            self.root.update()
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_close_waits_for_recorder_and_invokes_completion_once(self):
        alive = [True]
        closed = []
        self.window._rest_worker = SimpleNamespace(is_alive=lambda: alive[0])
        self.window.token.insert(0, "fixture-token")
        callback = lambda: closed.append(True)
        self.window.request_close(on_closed=callback)
        self.root.update()
        self.assertTrue(self.child.winfo_exists())
        self.assertTrue(self.window.stop_event.is_set())
        self.assertEqual(self.window.token.get(), "")
        self.assertIsNone(self.window._poll_id)
        self.assertEqual(closed, [])
        self.window.request_close(on_closed=callback)
        alive[0] = False
        self.tick_until(lambda: bool(closed))
        self.assertEqual(closed, [True])
        self.assertFalse(self.child.winfo_exists())

    def test_parent_close_while_child_close_pending_waits_for_worker(self):
        alive = [True]
        completed = []
        self.window._rest_worker = SimpleNamespace(is_alive=lambda: alive[0])
        app = SimpleNamespace(root=self.root, connection_controller=self.window, connection_window=self.child)
        self.window.request_close(on_closed=lambda: completed.append("child"))
        self.root.bind("<Destroy>", lambda event: completed.append("parent") if event.widget is self.root else None)
        Dashboard.request_close(app)
        self.root.update()
        self.assertEqual(completed, [])
        alive[0] = False
        self.tick_until(lambda: "parent" in completed)
        self.assertEqual(completed, ["child", "parent"])

    def test_idle_close_is_immediate(self):
        self.window.request_close()
        self.assertFalse(self.child.winfo_exists())

    def test_parent_then_child_close_does_not_refresh_destroyed_widgets(self):
        self.window.shutdown()
        self.child.destroy()
        app = Dashboard(self.root, self.temp.name, offline=False)
        app.connect()  # Opens only the local form; no request or credential.
        alive = [True]
        finished = []
        errors = []
        self.root.report_callback_exception = lambda kind, value, trace: errors.append(kind.__name__)
        self.root.bind("<Destroy>", lambda event: finished.append(True) if event.widget is self.root else None)
        app.connection_controller._rest_worker = SimpleNamespace(is_alive=lambda: alive[0])
        app.request_close()
        command = app.connection_window.protocol("WM_DELETE_WINDOW")
        self.root.tk.call(command)
        self.assertEqual(finished, [])
        alive[0] = False
        self.tick_until(lambda: bool(finished))
        self.assertEqual(errors, [])
        self.assertIsNone(app.connection_window)
        self.assertIsNone(app.connection_controller)

    def test_rest_worker_is_tracked_and_keeps_process_alive_to_finalize(self):
        self.window.token.insert(0, "fixture-token")
        with patch("connect_groww.regular_market_status", return_value="regular_session"), \
             patch("connect_groww.threading.Thread") as thread:
            self.window.capture_rest()
        self.assertIs(self.window._rest_worker, thread.return_value)
        self.assertFalse(thread.call_args.kwargs["daemon"])
        thread.return_value.start.assert_called_once()
        self.assertEqual(self.window.token.get(), "")

    def test_duration_and_stop_fit_820_high_window_and_lower_controls_scroll(self):
        self.child.geometry("700x820")
        self.root.update()
        top = self.child.winfo_rooty()
        for widget in (self.window.capture_minutes, self.window.rest_button, self.window.stop_button):
            self.assertGreaterEqual(widget.winfo_rooty() - top, 0)
            self.assertLess(widget.winfo_rooty() - top + widget.winfo_height(), 820)
        self.assertLess(self.window.stop_button.winfo_rooty(), self.window.audit_button.winfo_rooty())
        self.child.event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertGreater(self.window.scroll_canvas.yview()[0], 0)
        self.window.scroll_canvas.yview_moveto(1)
        self.root.update()
        self.assertGreater(self.window.scroll_canvas.yview()[0], 0)
        self.assertLess(self.window.activity_button.winfo_rooty() - top + self.window.activity_button.winfo_height(), 820)

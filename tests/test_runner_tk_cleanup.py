"""The offline runner owns fixture finalization, while exposing actual errors."""

import gc
import io
import sys
import threading
import tkinter as tk
import unittest
from unittest.mock import Mock, patch
import weakref

from run_tests import (MainThreadCleanupResult, collect_test_cycles,
                       main_thread_test_runtime)


class RunnerTkCleanupTests(unittest.TestCase):
    def test_tcl_fixture_cycle_is_collected_on_main_thread_after_worker_allocations(self):
        finalized_on = []

        class Fixture:
            def __init__(self):
                self.variable = tk.StringVar(master=tk.Tcl())
                self.cycle = self

            def __del__(self):
                finalized_on.append(threading.get_ident())

        def allocate_cycles():
            for _ in range(5000):
                cycle = []
                cycle.append(cycle)

        with main_thread_test_runtime() as issues:
            fixture = Fixture()
            reference = weakref.ref(fixture)
            del fixture
            worker = threading.Thread(target=allocate_cycles)
            worker.start()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(gc.isenabled())
            self.assertIsNotNone(reference())
            self.assertEqual(finalized_on, [])
            collect_test_cycles()
            self.assertIsNone(reference())
            self.assertEqual(finalized_on, [threading.main_thread().ident])
        self.assertEqual(issues, [])

    def test_runtime_restores_gc_and_hooks_even_when_test_body_raises(self):
        automatic = gc.isenabled()
        unraisable, worker_hook = sys.unraisablehook, threading.excepthook
        with self.assertRaisesRegex(ValueError, 'fixture test failed'):
            with main_thread_test_runtime():
                self.assertFalse(gc.isenabled())
                raise ValueError('fixture test failed')
        self.assertEqual(gc.isenabled(), automatic)
        self.assertIs(sys.unraisablehook, unraisable)
        self.assertIs(threading.excepthook, worker_hook)

    def test_result_collects_fixture_cycles_between_tests(self):
        result = MainThreadCleanupResult(unittest.runner._WritelnDecorator(io.StringIO()),
                                         descriptions=True, verbosity=0)
        sample = unittest.FunctionTestCase(lambda: None)
        with patch('run_tests.collect_test_cycles') as collect:
            result.startTest(sample)
            result.stopTest(sample)
        self.assertEqual(collect.call_count, 2)
        self.assertEqual(result.testsRun, 1)

    def test_runtime_restores_disabled_gc_when_body_enabled_it(self):
        automatic = gc.isenabled()
        gc.disable()
        try:
            with main_thread_test_runtime():
                gc.enable()
                self.assertTrue(gc.isenabled())
            self.assertFalse(gc.isenabled())
        finally:
            if automatic:
                gc.enable()
            else:
                gc.disable()

    def test_new_unfinished_worker_is_flagged_without_thread_names(self):
        release = threading.Event()
        baseline = threading.Thread(target=release.wait, name='baseline-synthetic-worker')
        new_worker = threading.Thread(target=release.wait, name='new-synthetic-worker')
        baseline.start()
        try:
            with main_thread_test_runtime() as issues:
                new_worker.start()
                self.assertTrue(baseline.is_alive())
                self.assertTrue(new_worker.is_alive())
            self.assertEqual(issues, [{'kind': 'unfinished_workers', 'count': 1}])
        finally:
            release.set()
            baseline.join(timeout=5)
            if new_worker.ident is not None:
                new_worker.join(timeout=5)
        self.assertFalse(baseline.is_alive())
        self.assertFalse(new_worker.is_alive())

    def test_worker_error_is_recorded_and_forwarded_to_previous_hook(self):
        previous_hook = Mock()

        def worker():
            raise RuntimeError('synthetic worker failure')

        with patch('threading.excepthook', previous_hook):
            with main_thread_test_runtime() as issues:
                thread = threading.Thread(target=worker)
                thread.start()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(issues, [{'kind': 'worker_exception', 'exception_type': 'RuntimeError'}])
        previous_hook.assert_called_once()
        self.assertIs(previous_hook.call_args.args[0].exc_type, RuntimeError)

    def test_finalizer_error_is_recorded_and_forwarded_to_previous_hook(self):
        previous_hook = Mock()

        class BrokenFixture:
            def __del__(self):
                raise RuntimeError('synthetic finalizer failure')

        with patch('sys.unraisablehook', previous_hook):
            with main_thread_test_runtime() as issues:
                fixture = BrokenFixture()
                del fixture
        self.assertEqual(issues, [{'kind': 'unraisable', 'exception_type': 'RuntimeError'}])
        previous_hook.assert_called_once()
        self.assertIs(previous_hook.call_args.args[0].exc_type, RuntimeError)

    def test_off_main_thread_collection_is_rejected(self):
        errors = []

        def worker():
            try:
                collect_test_cycles()
            except RuntimeError as error:
                errors.append(str(error))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['Test fixture collection must run on the main thread'])


if __name__ == '__main__':
    unittest.main()

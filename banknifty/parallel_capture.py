"""Keep index-feed capture independent of optional bank-quote failures."""
import threading


def run_parallel_captures(index_job, activity_job=None, *, join_timeout=35,
                          on_slow_shutdown=None):
    """Run read-only quote sweeps beside the index job, stopping them afterward.

    `activity_job` receives a cancellation predicate. A quote failure is
    reported separately; it never turns the index feed into a successful run.
    A slow quote request may exceed the soft timeout. In that case, keep
    waiting rather than returning while it can still use the token or write.
    """
    if activity_job is None:
        return index_job(), None
    if join_timeout <= 0:
        raise ValueError("Positive activity shutdown timeout required")
    stop = threading.Event()
    activity = {}

    def work():
        try:
            activity["result"] = activity_job(stop.is_set)
        except Exception as exc:
            activity["result"] = {"status": "activity_error", "saved": None,
                                  "failures": None, "sweeps": None,
                                  "error_type": type(exc).__name__}

    worker = threading.Thread(target=work, daemon=True, name="banknifty-bank-quotes")
    worker.start()
    try:
        index_result = index_job()
    finally:
        stop.set()
        worker.join(join_timeout)
        if worker.is_alive():
            if on_slow_shutdown is not None:
                try:
                    on_slow_shutdown()
                except Exception:
                    pass
            worker.join()
    return index_result, activity["result"]

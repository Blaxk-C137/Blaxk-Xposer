"""Bridge blocking work from worker threads back onto the GTK main loop.

Every UI mutation must happen on the GTK main thread. Callers hand this
module a blocking callable plus two callbacks; the work runs on a daemon
thread and exactly one callback is delivered on the main loop.
"""

import threading
from collections.abc import Callable
from typing import TypeVar

from gi.repository import GLib

from utils.logger import get_logger

logger = get_logger("ui-tasks")

T = TypeVar("T")


def run_off_thread(
    name: str,
    work: Callable[[], T],
    on_success: Callable[[T], None],
    on_error: Callable[[Exception], None],
) -> None:
    """Run work() on a daemon thread; deliver its outcome on the main loop.

    on_success receives the work result; on_error receives any exception
    raised. Unexpected (non-domain) exceptions are logged here before the
    error callback runs so nothing fails silently.
    """

    def worker() -> None:
        try:
            result = work()
        except Exception as exc:  # last-resort guard at the thread boundary
            logger.warning(f"{name} failed: {exc}")
            _deliver(on_error, exc)
            return
        _deliver(on_success, result)

    threading.Thread(target=worker, name=name, daemon=True).start()


def _deliver(callback: Callable[[], object], *args: object) -> None:
    """Schedule callback on the GTK main loop exactly once."""

    def run() -> bool:
        callback(*args)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(run)

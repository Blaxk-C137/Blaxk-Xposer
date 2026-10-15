"""Bridge blocking work from worker threads back onto the GTK main loop.

Every UI mutation must happen on the GTK main thread. Callers hand this
module a blocking callable plus optional callbacks; the work runs on a
daemon thread and at most one callback is delivered on the main loop.

Threading safety: the worker thread never touches GTK. It only calls
GLib.idle_add, which is documented as safe from any thread, and the
callbacks themselves run on the main loop.
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
    on_success: Callable[[T], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Run work() on a daemon thread; deliver its outcome on the main loop.

    on_success receives the work result; on_error receives any exception
    raised. Either callback may be None. Failures are logged here so a
    missing on_error never hides a problem.
    """

    def worker() -> None:
        try:
            result = work()
        except Exception as exc:  # last-resort guard at the thread boundary
            logger.warning(f"{name} failed: {exc}")
            if on_error is not None:
                deliver_on_main_loop(on_error, exc)
            return
        if on_success is not None:
            deliver_on_main_loop(on_success, result)

    threading.Thread(target=worker, name=name, daemon=True).start()


def deliver_on_main_loop(callback: Callable[..., object], *args: object) -> None:
    """Schedule callback(*args) on the GTK main loop exactly once."""

    def run() -> bool:
        callback(*args)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(run)

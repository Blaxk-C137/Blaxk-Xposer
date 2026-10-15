"""One asyncio event loop on a daemon thread, shared by the network engines.

Threading safety: the loop object is only driven by its own thread. Other
threads hand it work exclusively through asyncio.run_coroutine_threadsafe
and loop.call_soon_threadsafe, the two entry points asyncio documents as
thread-safe. Results come back through concurrent.futures.Future, which
is itself thread-safe.
"""

import asyncio
import ssl
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from utils.logger import get_logger

logger = get_logger("async")

T = TypeVar("T")

STARTUP_TIMEOUT_SECONDS = 5.0
SHUTDOWN_TIMEOUT_SECONDS = 3.0
# Peers vanishing mid-transfer (phones locking, tabs closing) are routine
# for a LAN proxy; asyncio would otherwise print a traceback for each.
ROUTINE_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,
    ssl.SSLError,
)


class AsyncRunner:
    """Owns an event loop thread and runs coroutines on it for any caller."""

    def __init__(self, name: str = "xposer-net") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The running loop, started on first use."""
        return self._ensure_started()

    def run(self, coroutine: Coroutine[Any, Any, T], timeout: float) -> T:
        """Run coroutine on the loop and block the calling thread for its result.

        Must never be called from the loop thread itself (it would deadlock).
        """
        loop = self._ensure_started()
        if threading.current_thread() is self._thread:
            coroutine.close()
            raise RuntimeError("AsyncRunner.run() called from its own loop thread")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        return future.result(timeout)

    def stop(self) -> None:
        """Stop the loop and join its thread; safe to call more than once."""
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = None
            self._thread = None
        if loop is None or thread is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        """Start the loop thread once; later calls return the same loop."""
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()
            loop.set_exception_handler(_quiet_exception_handler)
            ready = threading.Event()
            thread = threading.Thread(
                target=self._run_loop, args=(loop, ready), name=self._name, daemon=True
            )
            thread.start()
            if not ready.wait(STARTUP_TIMEOUT_SECONDS):
                raise RuntimeError("Network event loop failed to start")
            self._loop = loop
            self._thread = thread
            return loop

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop, ready: threading.Event) -> None:
        """Thread body: run the loop until stopped, then tidy up."""
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        try:
            loop.run_forever()
        finally:
            _cancel_pending(loop)
            loop.close()


def _cancel_pending(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel leftover tasks so closing the loop does not warn."""
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


def _quiet_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Demote routine disconnect errors to debug; keep real problems visible."""
    exception = context.get("exception")
    message = context.get("message", "event loop error")
    if isinstance(exception, ROUTINE_NETWORK_ERRORS):
        logger.debug(f"{message}: {exception!r}")
        return
    logger.warning(f"{message}: {exception!r}" if exception else message)

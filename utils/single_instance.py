"""Keep one Xposer per user session; a second launch raises the first.

The first instance listens on a fixed loopback port. A later launch finds
the port taken, sends a short token, and exits; the first instance then
shows its dashboard. Works the same on Linux and Windows (no D-Bus or
named pipes needed). If an unrelated program owns the port, Xposer simply
runs without the guard.
"""

import socket
import threading
from collections.abc import Callable

from network.socket_utils import LOOPBACK_V4, open_listener
from utils.logger import get_logger

logger = get_logger("instance")

INSTANCE_PORT = 47131
ACTIVATE_TOKEN = b"xposer:show\n"
CONNECT_TIMEOUT_SECONDS = 1.0
READ_TIMEOUT_SECONDS = 1.0


class SingleInstance:
    """Owns the instance lock and forwards activation requests."""

    def __init__(self, on_activate: Callable[[], None], port: int = INSTANCE_PORT) -> None:
        self._on_activate = on_activate
        self._port = port
        self._listener: socket.socket | None = None

    def acquire(self) -> bool:
        """True when this process should run; False when another one was raised."""
        try:
            listener = open_listener(LOOPBACK_V4, self._port, backlog=4)
        except OSError:
            if self._activate_existing():
                logger.info("Xposer is already running; asked it to show itself")
                return False
            logger.warning(f"Port {self._port} is busy; running without single-instance guard")
            return True
        listener.setblocking(True)
        self._listener = listener
        threading.Thread(target=self._serve, name="xposer-instance", daemon=True).start()
        return True

    def release(self) -> None:
        """Close the lock socket (process exit also releases it)."""
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    def _activate_existing(self) -> bool:
        """Ask the running instance to show itself."""
        try:
            with socket.create_connection(
                (LOOPBACK_V4, self._port), CONNECT_TIMEOUT_SECONDS
            ) as sock:
                sock.sendall(ACTIVATE_TOKEN)
            return True
        except OSError:
            return False

    def _serve(self) -> None:
        """Accept activation requests until the listener closes."""
        while self._listener is not None:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(READ_TIMEOUT_SECONDS)
                try:
                    message = connection.recv(len(ACTIVATE_TOKEN))
                except OSError:
                    continue
            if message == ACTIVATE_TOKEN:
                self._on_activate()

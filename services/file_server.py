"""Internal static file servers for Folder Mode."""

import functools
import http.server
import threading
from pathlib import Path

from utils.logger import get_logger

logger = get_logger("fileserver")

LISTEN_HOST_ANY = "0.0.0.0"
THREAD_JOIN_TIMEOUT_SECONDS = 2.0
SERVER_POLL_TIMEOUT_SECONDS = 0.2


class FileServerError(RuntimeError):
    """Raised when a static file server cannot be started."""


class _CountingHandler(http.server.SimpleHTTPRequestHandler):
    """Static file handler recording unique client IPs on its server."""

    def do_GET(self) -> None:
        """Serve GET requests after registering the client."""
        self.server.record_client(self.client_address[0])
        super().do_GET()

    def do_HEAD(self) -> None:
        """Serve HEAD requests after registering the client."""
        self.server.record_client(self.client_address[0])
        super().do_HEAD()

    def log_message(self, format: str, *args: object) -> None:
        """Route request logging to xposer's debug channel."""
        logger.debug(f"{self.address_string()} {self.path}")


class _FolderHTTPServer(http.server.ThreadingHTTPServer):
    """Threading HTTP server that tracks unique client addresses."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], directory: str) -> None:
        handler = functools.partial(_CountingHandler, directory=directory)
        super().__init__(address, handler)
        self._client_lock = threading.Lock()
        self._client_ips: set[str] = set()

    def record_client(self, ip: str) -> None:
        """Register a client IP as seen."""
        with self._client_lock:
            self._client_ips.add(ip)

    @property
    def connected_devices(self) -> int:
        """Unique client IPs observed this session."""
        with self._client_lock:
            return len(self._client_ips)


class _FolderServerInstance:
    """One static file server bound to one service."""

    def __init__(self, service_id: str, folder_path: Path, port: int) -> None:
        self.service_id = service_id
        self._address = (LISTEN_HOST_ANY, port)
        self._root = folder_path
        self._directory = str(folder_path)
        self._server: _FolderHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def connected_devices(self) -> int:
        """Unique clients served by this instance."""
        return self._server.connected_devices if self._server else 0

    @property
    def is_alive(self) -> bool:
        """True while the serving thread runs."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Bind and serve; raises FileServerError on failure."""
        # Binding succeeds even for a missing root, so validate before listen.
        if not self._root.is_dir():
            raise FileServerError(
                f"Serving root is not a directory: {self._directory}"
            )
        try:
            self._server = _FolderHTTPServer(self._address, self._directory)
        except OSError as exc:
            raise FileServerError(
                f"Cannot bind port {self._address[1]}: {exc}"
            ) from exc
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": SERVER_POLL_TIMEOUT_SECONDS},
            name=f"xposer-fileserver-{self._address[1]}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"Serving {self._directory} on "
            f"{self._address[0]}:{self._address[1]}"
        )

    def stop(self) -> None:
        """Shut down the server and join its thread."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
            self._thread = None
        logger.info(f"File server on port {self._address[1]} stopped")


class FileServer:
    """Owns all static file server instances, keyed by service id."""

    def __init__(self) -> None:
        self._instances: dict[str, _FolderServerInstance] = {}
        self._lock = threading.Lock()

    def start_server(
        self, service_id: str, folder_path: Path, port: int
    ) -> None:
        """Serve folder_path over HTTP on the given port."""
        with self._lock:
            if service_id in self._instances:
                raise FileServerError(
                    f"Service {service_id} already has a file server"
                )
            instance = _FolderServerInstance(service_id, folder_path, port)
            instance.start()
            self._instances[service_id] = instance

    def stop_server(self, service_id: str) -> None:
        """Stop and forget the server registered for service_id, if any."""
        with self._lock:
            instance = self._instances.pop(service_id, None)
        if instance is not None:
            instance.stop()

    def stop_all(self) -> None:
        """Stop every file server instance."""
        with self._lock:
            instances = list(self._instances.values())
            self._instances.clear()
        for instance in instances:
            instance.stop()

    def is_running(self, service_id: str) -> bool:
        """Return True when service_id has a live serving thread."""
        with self._lock:
            instance = self._instances.get(service_id)
        return instance.is_alive if instance else False

    def connected_devices(self, service_id: str) -> int:
        """Unique clients served for service_id, or 0."""
        with self._lock:
            instance = self._instances.get(service_id)
        return instance.connected_devices if instance else 0

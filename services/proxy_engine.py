"""TCP reverse proxy exposing localhost ports to the LAN."""

import select
import socket
import threading

from utils.logger import get_logger

logger = get_logger("proxy")

LISTEN_HOST_ANY = "0.0.0.0"
UPSTREAM_HOST = "127.0.0.1"
BACKLOG = 16
SELECT_TIMEOUT_SECONDS = 1.0
UPSTREAM_CONNECT_TIMEOUT_SECONDS = 5.0
SEND_STALL_TIMEOUT_SECONDS = 30.0
BUFFER_SIZE = 65536
THREAD_JOIN_TIMEOUT_SECONDS = 2.0


class ProxyError(RuntimeError):
    """Raised when a proxy listener cannot be established."""


class _ProxyInstance:
    """One listening socket forwarding connections to a localhost target."""

    def __init__(
        self, service_id: str, listen_host: str, listen_port: int, target_port: int
    ) -> None:
        self.service_id = service_id
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.target_port = target_port
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._client_ips: set[str] = set()
        self._lock = threading.Lock()
        self._stopping = threading.Event()

    @property
    def connected_devices(self) -> int:
        """Unique client IPs seen since this instance started."""
        with self._lock:
            return len(self._client_ips)

    def start(self) -> None:
        """Bind and begin accepting; raises ProxyError on failure."""
        try:
            self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listener.bind((self.listen_host, self.listen_port))
            self._listener.listen(BACKLOG)
            # Non-blocking: _accept_loop gates accept() behind select() so the
            # loop can notice _stopping instead of parking in the kernel.
            self._listener.setblocking(False)
        except OSError as exc:
            if self._listener is not None:
                self._listener.close()
                self._listener = None
            raise ProxyError(f"Cannot bind port {self.listen_port}: {exc}") from exc
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name=f"xposer-proxy-{self.listen_port}",
            daemon=True,
        )
        self._accept_thread.start()
        logger.info(
            f"Proxying {self.listen_host}:{self.listen_port} -> "
            f"{UPSTREAM_HOST}:{self.target_port}"
        )

    def stop(self) -> None:
        """Drain the accept loop within one select timeout, then close."""
        self._stopping.set()
        # Join before close: Linux keeps a listening socket alive (still
        # accepting handshakes) until an in-flight accept()/select() on
        # another thread returns, so closing first would leave the port
        # reachable. The loop wakes within SELECT_TIMEOUT_SECONDS.
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
            self._accept_thread = None
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError as exc:
                logger.debug(f"Listener close error: {exc}")
            self._listener = None
        logger.info(f"Proxy on port {self.listen_port} stopped")

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stopping.is_set():
            try:
                readable, _, _ = select.select(
                    [listener], [], [], SELECT_TIMEOUT_SECONDS
                )
            except OSError:
                break
            if not readable:
                continue
            try:
                client, address = listener.accept()
            except (BlockingIOError, ConnectionAbortedError):
                # EAGAIN from the non-blocking race, or ECONNABORTED when a
                # phone on flaky WiFi drops mid-handshake: transient, keep
                # serving.
                continue
            except OSError as exc:
                logger.warning(
                    f"Proxy accept loop on port {self.listen_port} "
                    f"stopping: {exc}"
                )
                break
            with self._lock:
                self._client_ips.add(address[0])
            threading.Thread(
                target=self._pump,
                args=(client,),
                name=f"xproxy-{self.listen_port}-{address[0]}",
                daemon=True,
            ).start()

    def _pump(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(
                (UPSTREAM_HOST, self.target_port),
                timeout=UPSTREAM_CONNECT_TIMEOUT_SECONDS,
            )
            # recv() is only reached when select() reports readable, so it
            # never blocks; the per-socket timeout is purely a send-stall
            # backstop. Quiet-but-healthy connections (WebSockets, SSE,
            # keep-alive) are unaffected - the timer arms only while a
            # sendall has data a stalled peer refuses to read.
            client.settimeout(SEND_STALL_TIMEOUT_SECONDS)
            upstream.settimeout(SEND_STALL_TIMEOUT_SECONDS)
        except OSError as exc:
            logger.warning(
                f"Upstream {UPSTREAM_HOST}:{self.target_port} unreachable: {exc}"
            )
            client.close()
            return
        sockets = [client, upstream]
        try:
            while not self._stopping.is_set():
                readable, _, _ = select.select(
                    sockets, [], [], SELECT_TIMEOUT_SECONDS
                )
                if not readable:
                    continue
                for source in readable:
                    data = source.recv(BUFFER_SIZE)
                    if not data:
                        return
                    destination = upstream if source is client else client
                    destination.sendall(data)
        except OSError as exc:
            logger.debug(f"Proxy connection ended: {exc}")
        finally:
            for sock in (client, upstream):
                try:
                    sock.close()
                except OSError:
                    pass


class ProxyEngine:
    """Owns all reverse proxy instances, keyed by service id."""

    def __init__(self) -> None:
        self._instances: dict[str, _ProxyInstance] = {}
        self._lock = threading.Lock()

    def start_proxy(
        self, service_id: str, listen_host: str, listen_port: int, target_port: int
    ) -> None:
        """Start forwarding listen_host:listen_port to localhost:target_port."""
        with self._lock:
            if service_id in self._instances:
                raise ProxyError(f"Service {service_id} already has a proxy")
            instance = _ProxyInstance(
                service_id, listen_host, listen_port, target_port
            )
            instance.start()
            self._instances[service_id] = instance

    def stop_proxy(self, service_id: str) -> None:
        """Stop and forget the proxy registered for service_id, if any."""
        with self._lock:
            instance = self._instances.pop(service_id, None)
        if instance is not None:
            instance.stop()

    def stop_all(self) -> None:
        """Stop every proxy instance."""
        with self._lock:
            instances = list(self._instances.values())
            self._instances.clear()
        for instance in instances:
            instance.stop()

    def is_running(self, service_id: str) -> bool:
        """Return True when service_id has a live proxy."""
        with self._lock:
            return service_id in self._instances

    def connected_devices(self, service_id: str) -> int:
        """Unique clients seen by the proxy for service_id, or 0."""
        with self._lock:
            instance = self._instances.get(service_id)
        return instance.connected_devices if instance else 0

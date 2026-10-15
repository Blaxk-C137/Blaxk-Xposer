"""Reverse proxy exposing localhost ports to the LAN.

All proxies share one asyncio event loop on a daemon thread (see
utils.async_runner), so a dozen shared apps with hundreds of connections
cost one thread instead of two threads per connection. The public API is
synchronous and thread-safe; it is called from ServiceManager workers.
"""

import asyncio
import socket
import threading
from collections.abc import Sequence
from functools import partial

from network.socket_utils import LOOPBACK_HOSTS, disable_nagle, open_listener
from services.http_rewriter import RewriteMap
from services.proxy_session import (
    SESSION_ERRORS,
    BufferedStream,
    ClientGone,
    ProxySession,
    ProxyStatsSnapshot,
    ProxyTarget,
    StreamClosed,
)
from utils.async_runner import AsyncRunner
from utils.logger import get_logger

logger = get_logger("proxy")

LISTEN_HOST_ANY = "0.0.0.0"
START_TIMEOUT_SECONDS = 5.0
STOP_TIMEOUT_SECONDS = 5.0
SERVER_CLOSE_WAIT_SECONDS = 2.0


class ProxyError(RuntimeError):
    """Raised when a proxy listener cannot be established."""


class ProxyEngine:
    """Manages every active proxy listener."""

    def __init__(self, runner: AsyncRunner | None = None) -> None:
        self._runner = runner or AsyncRunner()
        self._owns_runner = runner is None
        self._targets: dict[str, ProxyTarget] = {}
        self._servers: dict[str, asyncio.Server] = {}
        self._lock = threading.Lock()
        self._rewrite_map = RewriteMap()
        self._smart = True

    # ---- configuration (any thread) ----

    def set_rewrite_map(self, rewrite_map: RewriteMap) -> None:
        """Publish a new share map; sessions pick it up on their next request."""
        self._rewrite_map = rewrite_map

    def rewrite_map(self) -> RewriteMap:
        """The share map currently in force."""
        return self._rewrite_map

    def set_smart_mode(self, enabled: bool) -> None:
        """Toggle HTTP rewriting; off means byte-for-byte TCP relaying."""
        self._smart = enabled

    def smart_mode(self) -> bool:
        """True when HTTP-aware rewriting is enabled."""
        return self._smart

    # ---- lifecycle ----

    def start_proxy(
        self,
        service_id: str,
        listen_host: str,
        listen_port: int,
        target_port: int,
        upstream_hosts: Sequence[str] | None = None,
        upstream_tls: bool = False,
        raw_mode: bool = False,
        service_name: str = "",
    ) -> None:
        """Listen on listen_host:listen_port and forward to the local app.

        upstream_hosts lists the local addresses to try (default: 127.0.0.1
        then ::1). Raises ProxyError when the listener cannot be created.
        """
        with self._lock:
            if service_id in self._targets:
                raise ProxyError(f"Service {service_id} is already proxied")
        try:
            listener = open_listener(listen_host, listen_port)
        except OSError as exc:
            raise ProxyError(f"Cannot bind {listen_host}:{listen_port}: {exc}") from exc
        bound_port = int(listener.getsockname()[1])
        target = ProxyTarget(
            service_name=service_name,
            local_port=target_port,
            upstream_hosts=list(upstream_hosts or LOOPBACK_HOSTS),
            upstream_tls=upstream_tls,
            raw_mode=raw_mode,
            listen_host=listen_host,
            listen_port=bound_port,
            smart_enabled=self.smart_mode,
            rewrite_map=self.rewrite_map,
        )
        try:
            server = self._runner.run(self._serve(listener, target), START_TIMEOUT_SECONDS)
        except (OSError, RuntimeError, TimeoutError) as exc:
            listener.close()
            raise ProxyError(f"Cannot start proxy on {listen_host}:{listen_port}: {exc}") from exc
        with self._lock:
            self._targets[service_id] = target
            self._servers[service_id] = server
        logger.info(
            f"Proxy {service_id}: {listen_host}:{bound_port} -> localhost:{target_port}"
            f"{' (TLS)' if upstream_tls else ''}{' (raw)' if raw_mode else ''}"
        )

    def stop_proxy(self, service_id: str) -> None:
        """Stop one proxy and drop its connections. No-op if unknown."""
        with self._lock:
            target = self._targets.pop(service_id, None)
            server = self._servers.pop(service_id, None)
        if target is None or server is None:
            return
        try:
            self._runner.run(self._close(server, target), STOP_TIMEOUT_SECONDS)
        except (RuntimeError, TimeoutError) as exc:
            logger.warning(f"Proxy {service_id} did not stop cleanly: {exc}")
        logger.info(f"Proxy {service_id} stopped")

    def stop_all(self) -> None:
        """Stop every proxy (and the event loop if this engine owns it)."""
        with self._lock:
            service_ids = list(self._targets)
        for service_id in service_ids:
            self.stop_proxy(service_id)
        if self._owns_runner:
            self._runner.stop()

    # ---- queries (any thread) ----

    def is_running(self, service_id: str) -> bool:
        """True while a proxy for service_id is listening."""
        with self._lock:
            return service_id in self._targets

    def listen_port(self, service_id: str) -> int | None:
        """Port actually bound for service_id (useful when 0 was requested)."""
        with self._lock:
            target = self._targets.get(service_id)
        return target.listen_port if target is not None else None

    def connected_devices(self, service_id: str) -> int:
        """Devices that used this proxy in the last few minutes (0 if unknown)."""
        stats = self.stats(service_id)
        return stats.connected_devices if stats is not None else 0

    def stats(self, service_id: str) -> ProxyStatsSnapshot | None:
        """Counter snapshot for service_id, or None if it is not running."""
        with self._lock:
            target = self._targets.get(service_id)
        return target.stats.snapshot() if target is not None else None

    # ---- loop-thread internals ----

    async def _serve(self, listener: socket.socket, target: ProxyTarget) -> asyncio.Server:
        """Start accepting on an already-bound listener."""
        return await asyncio.start_server(partial(self._handle, target), sock=listener)

    @staticmethod
    async def _handle(
        target: ProxyTarget, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one accepted connection."""
        task = asyncio.current_task()
        disable_nagle(writer.get_extra_info("socket"))
        client = BufferedStream(reader, writer)
        if task is not None:
            target.tasks.add(task)
        target.stats.connection_opened(client.peer_ip)
        try:
            await ProxySession(target, client).run()
        except (StreamClosed, ClientGone) + SESSION_ERRORS as exc:
            logger.debug(f"Connection from {client.peer_ip} ended: {exc!r}")
        finally:
            client.close()
            target.stats.connection_closed(client.peer_ip)
            if task is not None:
                target.tasks.discard(task)

    @staticmethod
    async def _close(server: asyncio.Server, target: ProxyTarget) -> None:
        """Stop listening, cancel live sessions, wait briefly for teardown."""
        server.close()
        tasks = list(target.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(server.wait_closed(), SERVER_CLOSE_WAIT_SECONDS)
        except asyncio.TimeoutError:
            logger.debug("Server close timed out; connections were already cancelled")

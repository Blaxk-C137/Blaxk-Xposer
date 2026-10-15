"""mDNS hostname publishing for custom .local links.

Backends, tried in order:
* avahi — the system daemon on Linux desktops, driven via avahi-publish.
* zeroconf — the optional pure-Python responder (python-zeroconf), used on
  Windows and on Linux systems without a working avahi daemon.

Names are reference-counted: two shared services can use the same custom
link, and stopping one keeps the name alive for the other. When nothing
can publish, publish() returns False and callers keep the IP link, so
sharing itself never breaks.
"""

import importlib.util
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from utils.logger import get_logger

logger = get_logger("network")

LOCAL_SUFFIX = ".local"
# One label: 1-63 chars, no leading/trailing hyphen (avahi rejects those).
HOSTNAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.local)?$")
# avahi-publish exits within ~50ms when the daemon rejects a record.
SETTLE_SECONDS = 0.3
PROCESS_STOP_TIMEOUT_SECONDS = 1.0
ZEROCONF_SERVICE_TYPE = "_http._tcp.local."
DEFAULT_ADVERTISED_PORT = 80


def normalize_hostname(raw: str) -> str:
    """Lowercase and hyphenate user input, rejecting anything else.

    Spaces and underscores become hyphens so "My App" reads naturally;
    any other character outside [a-z0-9-.] raises ValueError so typos
    are surfaced instead of silently mangled.
    """
    cleaned = raw.strip().lower().replace(" ", "-").replace("_", "-")
    if not HOSTNAME_PATTERN.match(cleaned):
        raise ValueError(f"Not a usable hostname: {raw!r}")
    return cleaned


def to_local_name(hostname: str) -> str:
    """Append the .local suffix unless it is already present."""
    if hostname.endswith(LOCAL_SUFFIX):
        return hostname
    return hostname + LOCAL_SUFFIX


class MdnsBackend(Protocol):
    """Something that can announce hostname -> IPv4 on the local network."""

    name: str

    def publish(self, hostname: str, ip: str, port: int) -> bool:
        """Announce hostname; False when this backend cannot."""

    def unpublish(self, hostname: str) -> None:
        """Stop announcing hostname."""

    def close(self) -> None:
        """Release every resource."""


class AvahiBackend:
    """One avahi-publish process per hostname."""

    name = "avahi"

    def __init__(
        self,
        spawn: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        settle_seconds: float = SETTLE_SECONDS,
    ) -> None:
        self._spawn = spawn
        self._settle_seconds = settle_seconds
        self._processes: dict[str, subprocess.Popen[bytes]] = {}

    @staticmethod
    def installed() -> bool:
        """True when the avahi-publish tool is on PATH."""
        return shutil.which("avahi-publish") is not None

    def publish(self, hostname: str, ip: str, port: int) -> bool:
        """Spawn avahi-publish; a process dead after the settle time means rejection."""
        self._terminate(hostname)
        try:
            process = self._spawn(
                ["avahi-publish", "-a", "-R", hostname, ip],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            logger.warning(f"avahi-publish unavailable for {hostname}: {exc}")
            return False
        if self._settle_seconds > 0:
            time.sleep(self._settle_seconds)
        if process.poll() is not None:
            logger.warning(f"avahi rejected {hostname} (exit {process.returncode})")
            return False
        self._processes[hostname] = process
        return True

    def unpublish(self, hostname: str) -> None:
        """Stop the process announcing hostname, if any."""
        self._terminate(hostname)

    def close(self) -> None:
        """Stop every avahi-publish process."""
        for hostname in list(self._processes):
            self._terminate(hostname)

    def _terminate(self, hostname: str) -> None:
        """Kill and forget the process publishing hostname, if any."""
        process = self._processes.pop(hostname, None)
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()


class ZeroconfBackend:
    """Pure-Python mDNS responder (optional dependency python-zeroconf).

    Each name is registered as an _http._tcp service whose server name is
    the custom hostname, so the responder also answers A queries for it.
    """

    name = "zeroconf"

    def __init__(self) -> None:
        self._zeroconf: Any = None
        self._infos: dict[str, Any] = {}

    @staticmethod
    def installed() -> bool:
        """True when python-zeroconf can be imported."""
        return importlib.util.find_spec("zeroconf") is not None

    def publish(self, hostname: str, ip: str, port: int) -> bool:
        """Register hostname -> ip (blocks ~1 s while mDNS probes)."""
        try:
            import zeroconf
        except ImportError:
            return False
        self.unpublish(hostname)
        label = hostname.removesuffix(LOCAL_SUFFIX)
        try:
            if self._zeroconf is None:
                self._zeroconf = zeroconf.Zeroconf(ip_version=zeroconf.IPVersion.V4Only)
            info = zeroconf.ServiceInfo(
                ZEROCONF_SERVICE_TYPE,
                f"{label}.{ZEROCONF_SERVICE_TYPE}",
                addresses=[socket.inet_aton(ip)],
                port=port or DEFAULT_ADVERTISED_PORT,
                server=f"{hostname}.",
                properties={"path": "/"},
            )
            self._zeroconf.register_service(info, allow_name_change=True)
        except (OSError, zeroconf.Error) as exc:
            logger.warning(f"zeroconf could not publish {hostname}: {exc}")
            return False
        self._infos[hostname] = info
        return True

    def unpublish(self, hostname: str) -> None:
        """Withdraw hostname's registration."""
        info = self._infos.pop(hostname, None)
        if info is None or self._zeroconf is None:
            return
        try:
            self._zeroconf.unregister_service(info)
        except (OSError, RuntimeError) as exc:
            logger.debug(f"zeroconf unregister of {hostname} failed: {exc}")

    def close(self) -> None:
        """Withdraw everything and close the responder."""
        for hostname in list(self._infos):
            self.unpublish(hostname)
        if self._zeroconf is not None:
            self._zeroconf.close()
            self._zeroconf = None


def default_backends() -> list[MdnsBackend]:
    """Backends usable on this machine, preferred first."""
    backends: list[MdnsBackend] = []
    if AvahiBackend.installed():
        backends.append(AvahiBackend())
    if ZeroconfBackend.installed():
        backends.append(ZeroconfBackend())
    return backends


@dataclass
class _Record:
    """One published hostname."""

    ip: str
    port: int
    backend: MdnsBackend
    refs: int = 1


class MdnsPublisher:
    """Reference-counted hostname publishing over the first working backend.

    Threading safety: all state changes happen under one lock, so service
    workers may publish and unpublish concurrently.
    """

    def __init__(
        self,
        spawn: Callable[..., subprocess.Popen[bytes]] | None = None,
        settle_seconds: float = SETTLE_SECONDS,
        backends: list[MdnsBackend] | None = None,
    ) -> None:
        if backends is None:
            backends = (
                [AvahiBackend(spawn, settle_seconds)] if spawn is not None else default_backends()
            )
        self._backends = backends
        self._records: dict[str, _Record] = {}
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        """True when at least one backend exists on this machine."""
        return bool(self._backends)

    def backend_names(self) -> list[str]:
        """Names of usable backends, preferred first."""
        return [backend.name for backend in self._backends]

    def active_backend(self, hostname: str) -> str | None:
        """Backend currently publishing hostname, if any."""
        with self._lock:
            record = self._records.get(hostname)
            return record.backend.name if record else None

    def publish(self, hostname: str, ip: str, port: int = 0) -> bool:
        """Announce hostname -> ip; True when some backend accepted it."""
        with self._lock:
            record = self._records.get(hostname)
            if record is not None and record.ip == ip:
                record.refs += 1
                return True
            refs = record.refs + 1 if record is not None else 1
            if record is not None:
                record.backend.unpublish(hostname)
                del self._records[hostname]
            backend = self._publish_first(hostname, ip, port)
            if backend is None:
                return False
            self._records[hostname] = _Record(ip=ip, port=port, backend=backend, refs=refs)
            logger.info(f"Published {hostname} -> {ip} via {backend.name}")
            return True

    def unpublish(self, hostname: str) -> None:
        """Release one use of hostname; the last release withdraws it."""
        with self._lock:
            record = self._records.get(hostname)
            if record is None:
                return
            record.refs -= 1
            if record.refs > 0:
                return
            del self._records[hostname]
            record.backend.unpublish(hostname)
            logger.info(f"Unpublished {hostname}")

    def update_ip(self, ip: str) -> list[str]:
        """Re-announce every name at a new IP; returns names that failed."""
        failed: list[str] = []
        with self._lock:
            for hostname, record in list(self._records.items()):
                if record.ip == ip:
                    continue
                record.backend.unpublish(hostname)
                backend = self._publish_first(hostname, ip, record.port)
                if backend is None:
                    del self._records[hostname]
                    failed.append(hostname)
                    continue
                self._records[hostname] = _Record(
                    ip=ip, port=record.port, backend=backend, refs=record.refs
                )
        return failed

    def stop_all(self) -> None:
        """Withdraw every name and release backend resources (shutdown)."""
        with self._lock:
            for hostname, record in list(self._records.items()):
                record.backend.unpublish(hostname)
            self._records.clear()
            for backend in self._backends:
                backend.close()

    def _publish_first(self, hostname: str, ip: str, port: int) -> MdnsBackend | None:
        """Try each backend in order; return the one that succeeded."""
        for backend in self._backends:
            if backend.publish(hostname, ip, port):
                return backend
        return None

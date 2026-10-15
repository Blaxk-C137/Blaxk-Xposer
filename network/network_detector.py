"""Stateless network and port detection helpers (Linux, Windows, macOS).

Everything here is call-it-get-a-result: no caches, no background work.
psutil provides interfaces and listening sockets on every platform, so
there is no Unix-only code path (the old fcntl/ioctl probe is gone).
"""

import ipaddress
import re
import socket
import ssl
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePath, PureWindowsPath

import psutil

from network.socket_utils import (
    LOOPBACK_HOSTS,
    LOOPBACK_V4,
    WILDCARD_V4,
    can_bind,
    is_loopback,
    is_wildcard,
)
from utils.logger import get_logger

logger = get_logger("network")

MIN_PORT = 1
MAX_PORT = 65535
# Xposer's own listeners come from a quiet range so they never squat on
# 3000/5173/8000/8080, ports developers start their own servers on.
SCAN_RANGE_START = 47100
SCAN_RANGE_END = 48999
ALTERNATE_PORT_OFFSET = 10000
CONNECT_TIMEOUT_SECONDS = 0.25
PROBE_CONNECT_TIMEOUT_SECONDS = 1.0
BANNER_WAIT_SECONDS = 0.25
TLS_PROBE_TIMEOUT_SECONDS = 1.0
TLS_HANDSHAKE_RECORD = 0x16
TLS_ALERT_RECORD = 0x15
REPLY_SAMPLE_LENGTH = 5
REDIS_ERROR_PREFIX = b"-ERR"
PRINTABLE_BYTES = frozenset(range(0x20, 0x7F)) | frozenset(b"\t\r\n")
DEFAULT_ROUTE_PROBE: tuple[str, int] = ("8.8.8.8", 80)
LAN_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
)
VIRTUAL_INTERFACE_PREFIX = re.compile(
    r"^(lo|docker|br-|veth|virbr|vboxnet|vmnet|tun|tap|wg|tailscale|zt|utun|"
    r"ppp|cni|flannel|kube|podman|lxc|lxd|incus|waydroid|anbox)",
    re.IGNORECASE,
)
VIRTUAL_INTERFACE_WORDS = re.compile(
    r"vethernet|virtualbox|vmware|hyper-v|loopback|wsl|tailscale|zerotier|vpn|"
    r"bluetooth|npcap|teredo|isatap|pseudo",
    re.IGNORECASE,
)
COMMAND_SUFFIXES = (".js", ".mjs", ".cjs", ".cmd", ".exe", ".py", ".ps1", ".bat", ".sh")
# (tokens that identify the tool, friendly label) — most specific first.
APP_SIGNATURES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"vite"}), "Vite"),
    (frozenset({"next", "next-server", "next-router-worker"}), "Next.js"),
    (frozenset({"nuxt", "nuxi"}), "Nuxt"),
    (frozenset({"astro"}), "Astro"),
    (frozenset({"react-scripts"}), "Create React App"),
    (frozenset({"ng"}), "Angular"),
    (frozenset({"svelte-kit"}), "SvelteKit"),
    (frozenset({"remix"}), "Remix"),
    (frozenset({"gatsby"}), "Gatsby"),
    (frozenset({"parcel"}), "Parcel"),
    (frozenset({"expo"}), "Expo"),
    (frozenset({"storybook", "start-storybook"}), "Storybook"),
    (frozenset({"webpack", "webpack-dev-server"}), "webpack"),
    (frozenset({"runserver"}), "Django"),
    (frozenset({"uvicorn"}), "Uvicorn"),
    (frozenset({"gunicorn"}), "Gunicorn"),
    (frozenset({"hypercorn"}), "Hypercorn"),
    (frozenset({"flask"}), "Flask"),
    (frozenset({"streamlit"}), "Streamlit"),
    (frozenset({"jupyter", "jupyter-lab", "jupyter-notebook"}), "Jupyter"),
    (frozenset({"http.server"}), "Python http.server"),
    (frozenset({"rails", "puma"}), "Rails"),
    (frozenset({"artisan"}), "Laravel"),
    (frozenset({"hugo"}), "Hugo"),
    (frozenset({"jekyll"}), "Jekyll"),
    (frozenset({"live-server"}), "Live Server"),
    (frozenset({"http-server"}), "http-server"),
    (frozenset({"json-server"}), "json-server"),
    (frozenset({"dotnet"}), ".NET"),
    (frozenset({"php"}), "PHP"),
    (frozenset({"bun"}), "Bun"),
    (frozenset({"deno"}), "Deno"),
)
UNKNOWN_PROCESS = "Unknown"


class UpstreamKind(Enum):
    """What a local server speaks, as far as a quick probe can tell."""

    HTTP = "http"
    TLS = "tls"
    SERVER_FIRST = "server_first"
    NON_HTTP = "non_http"

    @property
    def is_raw(self) -> bool:
        """True for protocols that must be relayed byte for byte."""
        return self in (UpstreamKind.SERVER_FIRST, UpstreamKind.NON_HTTP)


@dataclass(frozen=True)
class ListeningPort:
    """A local port currently accepting TCP connections."""

    port: int
    pid: int | None
    process_name: str
    bind_addresses: tuple[str, ...] = ()
    app_label: str = ""

    @property
    def local_only(self) -> bool:
        """True when only this computer can reach the port directly."""
        return bool(self.bind_addresses) and all(
            is_loopback(address) for address in self.bind_addresses
        )

    @property
    def display_name(self) -> str:
        """Friendly description such as 'Vite · node' (or just 'Next.js')."""
        if not self.app_label:
            return self.process_name
        if " " in self.process_name or self.app_label.lower() == self.process_name.lower():
            return self.app_label
        return f"{self.app_label} · {self.process_name}"


@dataclass(frozen=True)
class InterfaceInfo:
    """A network interface and one of its IPv4 addresses."""

    name: str
    ip: str
    is_up: bool = True
    is_virtual: bool = False


@dataclass(frozen=True)
class UpstreamProbe:
    """Result of probing a local port before sharing it."""

    port: int
    host: str | None
    kind: UpstreamKind = UpstreamKind.HTTP

    @property
    def reachable(self) -> bool:
        """True when something accepted a connection on the port."""
        return self.host is not None

    @property
    def is_tls(self) -> bool:
        """True when the local server speaks HTTPS/TLS."""
        return self.kind == UpstreamKind.TLS


@dataclass
class _PortAccumulator:
    """Collects every listening socket that shares one port number."""

    pid: int | None = None
    addresses: set[str] = field(default_factory=set)


class NetworkDetector:
    """Stateless network detection: call a method, get a result."""

    def validate_port(self, port: int) -> bool:
        """Return True when port is a valid TCP port number."""
        return MIN_PORT <= port <= MAX_PORT

    # ---- addresses ----

    def get_lan_ip(self) -> str:
        """Best IPv4 address for other devices on the WiFi to reach us.

        A VPN or Docker bridge can own the default route; physical private
        interfaces win over those, and loopback is the last resort.
        """
        return choose_lan_ip(self.list_interfaces(), self._default_route_ip())

    def list_interfaces(self) -> list[InterfaceInfo]:
        """IPv4 interfaces other than loopback, via psutil (all platforms)."""
        try:
            addresses = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
        except (OSError, psutil.Error) as exc:
            logger.warning(f"Cannot enumerate interfaces: {exc}")
            return []
        results: list[InterfaceInfo] = []
        for name, entries in addresses.items():
            stat = stats.get(name)
            is_up = stat.isup if stat is not None else True
            for entry in entries:
                if entry.family != socket.AF_INET or not entry.address:
                    continue
                if is_loopback(entry.address):
                    continue
                results.append(
                    InterfaceInfo(
                        name=name,
                        ip=entry.address,
                        is_up=is_up,
                        is_virtual=looks_virtual(name),
                    )
                )
        return results

    # ---- listening ports ----

    def detect_listening_ports(
        self, exclude: frozenset[tuple[str, int]] = frozenset()
    ) -> list[ListeningPort]:
        """Every listening TCP port (IPv4 and IPv6) with a friendly app name.

        exclude lists (address, port) sockets to ignore — Xposer's own
        listeners, which may reuse an app's port number on the LAN address.
        """
        try:
            connections = psutil.net_connections(kind="tcp")
        except (psutil.AccessDenied, PermissionError, OSError) as exc:
            logger.warning(f"System-wide port scan unavailable: {exc}")
            return []
        grouped: dict[int, _PortAccumulator] = {}
        for conn in connections:
            if conn.status != psutil.CONN_LISTEN or not conn.laddr:
                continue
            if (conn.laddr.ip, conn.laddr.port) in exclude:
                continue
            accumulator = grouped.setdefault(conn.laddr.port, _PortAccumulator())
            accumulator.addresses.add(conn.laddr.ip)
            if accumulator.pid is None and conn.pid is not None:
                accumulator.pid = conn.pid
        identities: dict[int | None, tuple[str, str]] = {}
        results: list[ListeningPort] = []
        for port, accumulator in grouped.items():
            if accumulator.pid not in identities:
                identities[accumulator.pid] = self._identify_process(accumulator.pid)
            process_name, app_label = identities[accumulator.pid]
            results.append(
                ListeningPort(
                    port=port,
                    pid=accumulator.pid,
                    process_name=process_name,
                    bind_addresses=tuple(sorted(accumulator.addresses)),
                    app_label=app_label,
                )
            )
        return sorted(results, key=lambda entry: entry.port)

    def is_port_in_use(self, port: int, host: str = LOOPBACK_V4) -> bool:
        """Return True when something accepts TCP connections on host:port."""
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(CONNECT_TIMEOUT_SECONDS)
                return sock.connect_ex((host, port)) == 0
        except OSError:
            return False

    def find_listening_host(self, port: int, preferred: Sequence[str] = ()) -> str | None:
        """First address (preferred, then 127.0.0.1, then ::1) that accepts port.

        Node 17+ binds 'localhost' to ::1 only, so IPv4-only probing would
        report a running Vite server as dead.
        """
        for host in _unique(list(preferred) + list(LOOPBACK_HOSTS)):
            if self.is_port_in_use(port, host):
                return host
        return None

    def is_port_listening(self, port: int, preferred: Sequence[str] = ()) -> bool:
        """True when a local server answers on port over either loopback family."""
        return self.find_listening_host(port, preferred) is not None

    def probe_upstream(self, port: int, preferred_host: str | None = None) -> UpstreamProbe:
        """Find where a local server listens and whether it speaks TLS."""
        preferred = [preferred_host] if preferred_host else []
        host = self.find_listening_host(port, preferred)
        if host is None:
            host = self._specific_bind_host(port)
        if host is None:
            return UpstreamProbe(port=port, host=None)
        return UpstreamProbe(port=port, host=host, kind=self._classify(host, port))

    # ---- binding ----

    def is_port_bindable(self, port: int, host: str = "") -> bool:
        """Return True when Xposer could bind host:port right now."""
        return can_bind(host or WILDCARD_V4, port)

    def find_available_port(self, preferred: int | None = None, host: str = "") -> int | None:
        """A bindable port: preferred first, then Xposer's quiet range, then any."""
        candidates: list[int] = []
        if preferred is not None and self.validate_port(preferred):
            candidates.append(preferred)
        candidates.extend(range(SCAN_RANGE_START, SCAN_RANGE_END + 1))
        for port in candidates:
            if self.is_port_bindable(port, host):
                return port
        return self._os_assigned_port(host)

    # ---- internals ----

    @staticmethod
    def _default_route_ip() -> str | None:
        """Source address of the default route (no packet is sent)."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(DEFAULT_ROUTE_PROBE)
                address = sock.getsockname()[0]
        except OSError as exc:
            logger.debug(f"Default route probe failed: {exc}")
            return None
        return None if not address or is_loopback(address) else address

    @staticmethod
    def _os_assigned_port(host: str) -> int | None:
        """Let the OS pick a free port when the quiet range is exhausted."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((host or WILDCARD_V4, 0))
                return int(sock.getsockname()[1])
        except OSError:
            return None

    @staticmethod
    def _specific_bind_host(port: int) -> str | None:
        """A non-wildcard, non-loopback address a server bound port to, if any."""
        try:
            connections = psutil.net_connections(kind="tcp")
        except (psutil.AccessDenied, PermissionError, OSError):
            return None
        for conn in connections:
            if conn.status != psutil.CONN_LISTEN or not conn.laddr:
                continue
            address = conn.laddr.ip
            if conn.laddr.port == port and not is_wildcard(address) and not is_loopback(address):
                return address
        return None

    @staticmethod
    def _classify(host: str, port: int) -> UpstreamKind:
        """Tell server-first protocols, TLS servers and plain HTTP apart."""
        try:
            with socket.create_connection(
                (host, port), timeout=PROBE_CONNECT_TIMEOUT_SECONDS
            ) as sock:
                if _server_speaks_first(sock):
                    return UpstreamKind.SERVER_FIRST
                return _classify_reply(sock)
        except OSError as exc:
            logger.debug(f"Probe of {host}:{port} failed: {exc}")
            return UpstreamKind.HTTP

    @staticmethod
    def _identify_process(pid: int | None) -> tuple[str, str]:
        """(process name, framework label) for pid; tolerant of access errors."""
        if pid is None:
            return UNKNOWN_PROCESS, ""
        try:
            process = psutil.Process(pid)
            name = process.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return UNKNOWN_PROCESS, ""
        try:
            command = process.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            command = []
        return _strip_suffix(name), detect_app_label(command or [name])


def choose_lan_ip(interfaces: Iterable[InterfaceInfo], route_ip: str | None) -> str:
    """Pick the address phones on the same WiFi are most likely to reach."""
    candidates = [info for info in interfaces if not is_loopback(info.ip)]
    if route_ip and all(info.ip != route_ip for info in candidates):
        candidates.append(InterfaceInfo(name="default-route", ip=route_ip))
    if not candidates:
        return LOOPBACK_V4

    def rank(info: InterfaceInfo) -> tuple[int, int, int, int]:
        return (
            0 if is_lan_address(info.ip) else 1,
            1 if info.is_virtual else 0,
            0 if info.ip == route_ip else 1,
            0 if info.is_up else 1,
        )

    return min(candidates, key=rank).ip


def is_lan_address(ip: str) -> bool:
    """True for RFC 1918 addresses (home/office networks)."""
    try:
        address = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return any(address in network for network in LAN_NETWORKS)


def looks_virtual(interface_name: str) -> bool:
    """Heuristic: VPN, container, VM and loopback adapters are 'virtual'."""
    return bool(
        VIRTUAL_INTERFACE_PREFIX.search(interface_name)
        or VIRTUAL_INTERFACE_WORDS.search(interface_name)
    )


def detect_app_label(command: Sequence[str]) -> str:
    """Name the dev tool behind a command line, e.g. 'Vite' or 'Django'.

    Arguments are split on spaces too, because tools such as Next.js
    rewrite their process title to 'next-server (v16.4.1)'.
    """
    tokens = {_command_token(word) for part in command for word in part.split() if word}
    for signature, label in APP_SIGNATURES:
        if tokens & signature:
            return label
    return ""


def _command_token(part: str) -> str:
    """Lower-cased basename of one argv entry without script suffixes."""
    base = PureWindowsPath(part).name if "\\" in part else PurePath(part).name
    return _strip_suffix(base.lower())


def _strip_suffix(name: str) -> str:
    """Drop .exe/.js/.cmd-style suffixes from a process or script name."""
    lowered = name.lower()
    for suffix in COMMAND_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _server_speaks_first(sock: socket.socket) -> bool:
    """True when the server sends a banner unprompted (SSH, MySQL, SMTP…)."""
    sock.settimeout(BANNER_WAIT_SECONDS)
    try:
        return bool(sock.recv(1))
    except OSError:
        return False


def _classify_reply(sock: socket.socket) -> UpstreamKind:
    """Send a TLS ClientHello and judge the server by its first bytes.

    A TLS record means HTTPS. Silence (line-buffered servers waiting for a
    newline), an immediate close or any printable reply — 'HTTP/1.1 400',
    or the bare HTML error page Python's http.server and Django send — mean
    plain HTTP. Only binary replies (PostgreSQL, MongoDB…) and Redis-style
    '-ERR' errors mark a client-first protocol that must be relayed raw;
    the bias is deliberate, since treating a web app as raw TCP would lose
    every HTTP fix.
    """
    sock.settimeout(TLS_PROBE_TIMEOUT_SECONDS)
    try:
        sock.sendall(_client_hello())
        reply = _recv_up_to(sock, REPLY_SAMPLE_LENGTH)
    except OSError:
        return UpstreamKind.HTTP
    if not reply:
        return UpstreamKind.HTTP
    if reply[0] in (TLS_HANDSHAKE_RECORD, TLS_ALERT_RECORD):
        return UpstreamKind.TLS
    if reply.startswith(REDIS_ERROR_PREFIX) or any(byte not in PRINTABLE_BYTES for byte in reply):
        return UpstreamKind.NON_HTTP
    return UpstreamKind.HTTP


def _recv_up_to(sock: socket.socket, size: int) -> bytes:
    """Read until size bytes, EOF or timeout (whatever arrived is returned)."""
    data = b""
    while len(data) < size:
        try:
            chunk = sock.recv(size - len(data))
        except OSError:
            return data
        if not chunk:
            return data
        data += chunk
    return data


def _client_hello() -> bytes:
    """Bytes of a TLS ClientHello, produced without touching the network."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname="localhost")
    try:
        tls.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return outgoing.read()


def _unique(items: Iterable[str]) -> list[str]:
    """Deduplicate while preserving order."""
    return list(dict.fromkeys(item for item in items if item))

"""Stateless network and port detection helpers.

netifaces is used when importable but is optional: it fails to build on
recent Python versions, so all functionality has a stdlib fallback.
"""

import fcntl
import socket
import struct

import psutil

try:
    import netifaces
except ImportError:
    netifaces = None

from dataclasses import dataclass

from utils.logger import get_logger

logger = get_logger("network")

SIOCGIFADDR = 0x8915
PROBE_TARGETS: tuple[tuple[str, int], ...] = (
    ("8.8.8.8", 80),
    ("10.255.255.255", 1),
    ("192.168.255.255", 1),
)
MIN_PORT = 1
MAX_PORT = 65535
SCAN_RANGE_START = 8000
CONNECT_TIMEOUT_SECONDS = 0.25


@dataclass(frozen=True)
class ListeningPort:
    """A localhost port currently accepting connections."""

    port: int
    pid: int | None
    process_name: str


@dataclass(frozen=True)
class InterfaceInfo:
    """A network interface and its IPv4 address."""

    name: str
    ip: str


class NetworkDetector:
    """Stateless network detection: call a method, get a result."""

    def validate_port(self, port: int) -> bool:
        """Return True when port is a valid TCP port number."""
        return MIN_PORT <= port <= MAX_PORT

    def get_lan_ip(self) -> str:
        """Return the primary LAN IPv4 address, loopback as last resort.

        Uses UDP connect probes which route packets without sending any.
        """
        for target in PROBE_TARGETS:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.connect(target)
                    ip = sock.getsockname()[0]
                if ip and not ip.startswith("127."):
                    return ip
            except OSError as exc:
                logger.debug(f"LAN probe {target} failed: {exc}")
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"

    def list_interfaces(self) -> list[InterfaceInfo]:
        """List interfaces with an IPv4 address, preferring netifaces."""
        if netifaces is not None:
            return self._interfaces_netifaces()
        return self._interfaces_ioctl()

    def detect_listening_ports(self) -> list[ListeningPort]:
        """List every currently-listening TCP port with its process name."""
        try:
            connections = psutil.net_connections(kind="inet")
        except psutil.AccessDenied as exc:
            logger.warning(f"System-wide port scan unavailable: {exc}")
            return []
        found: dict[int, ListeningPort] = {}
        for conn in connections:
            if conn.status != psutil.CONN_LISTEN or conn.laddr is None:
                continue
            if conn.laddr.port in found:
                continue
            found[conn.laddr.port] = ListeningPort(
                port=conn.laddr.port,
                pid=conn.pid,
                process_name=self._process_name(conn.pid),
            )
        return sorted(found.values(), key=lambda entry: entry.port)

    def is_port_in_use(self, port: int, host: str = "127.0.0.1") -> bool:
        """Return True when something accepts TCP connections on host:port."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(CONNECT_TIMEOUT_SECONDS)
                return sock.connect_ex((host, port)) == 0
        except OSError:
            return False

    def is_port_bindable(self, port: int, host: str = "") -> bool:
        """Return True when we can bind host:port right now."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, port))
            return True
        except OSError:
            return False

    def find_available_port(self, preferred: int | None = None) -> int | None:
        """Return the first bindable port, trying preferred then the scan range."""
        candidates: list[int] = []
        if preferred is not None:
            candidates.append(preferred)
        candidates.extend(range(SCAN_RANGE_START, MAX_PORT + 1))
        for port in candidates:
            if self.is_port_bindable(port):
                return port
        return None

    @staticmethod
    def _process_name(pid: int | None) -> str:
        if pid is None:
            return "Unknown"
        try:
            return psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return "Unknown"

    @staticmethod
    def _interfaces_netifaces() -> list[InterfaceInfo]:
        results: list[InterfaceInfo] = []
        try:
            names = netifaces.interfaces()
        except OSError as exc:
            logger.warning(f"netifaces interface listing failed: {exc}")
            return results
        for name in names:
            addresses = netifaces.ifaddresses(name).get(netifaces.AF_INET, [])
            for address in addresses:
                ip = address.get("addr")
                if ip:
                    results.append(InterfaceInfo(name=name, ip=ip))
        return results

    @staticmethod
    def _interfaces_ioctl() -> list[InterfaceInfo]:
        results: list[InterfaceInfo] = []
        try:
            names = socket.if_nameindex()
        except OSError as exc:
            logger.warning(f"Cannot enumerate interfaces: {exc}")
            return results
        for _index, name in names:
            ip = NetworkDetector._interface_ip_ioctl(name)
            if ip:
                results.append(InterfaceInfo(name=name, ip=ip))
        return results

    @staticmethod
    def _interface_ip_ioctl(name: str) -> str | None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                packed = struct.pack("256s", name[:15].encode("utf-8"))
                reply = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, packed)
            return socket.inet_ntoa(reply[20:24])
        except OSError:
            return None

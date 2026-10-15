"""Low-level socket helpers with platform-correct semantics.

Address reuse differs dangerously between systems: on Windows SO_REUSEADDR
lets a socket *steal* a port another program is listening on, so Xposer
must use SO_EXCLUSIVEADDRUSE there. On Linux/macOS SO_REUSEADDR only
skips the TIME_WAIT delay, which is exactly what a restartable proxy wants.
"""

import ipaddress
import socket

from utils.platform_env import IS_WINDOWS

LOOPBACK_V4 = "127.0.0.1"
LOOPBACK_V6 = "::1"
WILDCARD_V4 = "0.0.0.0"
WILDCARD_V6 = "::"
LOOPBACK_HOSTS: tuple[str, ...] = (LOOPBACK_V4, LOOPBACK_V6)
WILDCARD_HOSTS: frozenset[str] = frozenset({WILDCARD_V4, WILDCARD_V6, ""})
DEFAULT_BACKLOG = 128


def family_for(host: str) -> socket.AddressFamily:
    """AF_INET6 for IPv6 literals, AF_INET for everything else."""
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def apply_bind_policy(sock: socket.socket) -> None:
    """Set the address-reuse option appropriate for this platform."""
    if IS_WINDOWS:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        return
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def open_listener(host: str, port: int, backlog: int = DEFAULT_BACKLOG) -> socket.socket:
    """Bind and listen on host:port; raises OSError when that is impossible.

    The protocol is set explicitly to IPPROTO_TCP: asyncio only disables
    Nagle's algorithm on accepted sockets whose proto says TCP, and with
    Nagle on, a response head and body sent as two writes wait for the
    client's delayed ACK (~40 ms per request).
    """
    sock = socket.socket(family_for(host), socket.SOCK_STREAM, socket.IPPROTO_TCP)
    try:
        apply_bind_policy(sock)
        sock.bind((host, port))
        sock.listen(backlog)
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


def can_bind(host: str, port: int) -> bool:
    """True when host:port could be bound right now with Xposer's policy."""
    try:
        sock = socket.socket(family_for(host), socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        apply_bind_policy(sock)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def is_loopback(host: str) -> bool:
    """True for 127.0.0.0/8, ::1 and the name localhost."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_wildcard(host: str) -> bool:
    """True for addresses meaning 'every interface'."""
    return host in WILDCARD_HOSTS


def host_for_url(host: str) -> str:
    """Bracket IPv6 literals so they can sit in front of ':port' in a URL."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def disable_nagle(sock: socket.socket | None) -> None:
    """Send small writes immediately (no-op for non-TCP or closed sockets)."""
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        return

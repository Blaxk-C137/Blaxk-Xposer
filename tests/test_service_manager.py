"""Tests for the central service lifecycle manager."""

import http.server
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from models.service import ServiceStatus, ServiceType
from services.service_manager import (
    EVENT_SERVICES_CHANGED,
    FolderInvalidError,
    PortNotListeningError,
    ValidationError,
)

UPSTREAM_BODY = b"upstream-ok"


def wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def open_local_listener() -> tuple[socket.socket, int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    return server, server.getsockname()[1]


def listen_on(port: int) -> socket.socket:
    """Bind a listener to a specific localhost port."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", port))
    server.listen(4)
    return server


class _UpstreamHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP responder standing in for a real localhost app."""

    def do_GET(self) -> None:
        """Answer every GET with the fixed upstream body."""
        self.send_response(200)
        self.send_header("Content-Length", str(len(UPSTREAM_BODY)))
        self.end_headers()
        self.wfile.write(UPSTREAM_BODY)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the test output clean."""


def start_http_upstream() -> tuple[http.server.ThreadingHTTPServer, int]:
    """Run a throwaway HTTP server on a free loopback port."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def make_site(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("manager-site", encoding="utf-8")
    return site


@pytest.fixture()
def manager(components):
    yield components["manager"]
    components["manager"].shutdown()


def test_expose_port_end_to_end(manager, components):
    upstream, port = start_http_upstream()
    events: list[str] = []
    manager.add_observer(events.append)
    service = manager.expose_port("Backend", port)
    try:
        assert service.status == ServiceStatus.RUNNING
        assert service.type == ServiceType.PORT
        assert service.port == port
        assert service.lan_url.endswith(f":{service.assigned_port}")
        assert components["qr"] and service.qr_image_path is not None
        assert EVENT_SERVICES_CHANGED in events
        with urllib.request.urlopen(service.lan_url, timeout=3) as body:
            assert body.read() == UPSTREAM_BODY
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_expose_rejects_dead_port(manager):
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    unused_port = dead.getsockname()[1]
    dead.close()
    with pytest.raises(PortNotListeningError):
        manager.expose_port("Ghost", unused_port)


def test_expose_rejects_bad_input(manager):
    real, port = open_local_listener()
    try:
        with pytest.raises(ValidationError):
            manager.expose_port("   ", port)
        with pytest.raises(ValidationError):
            manager.expose_port("Bad", 70000)
    finally:
        real.close()


def test_serve_folder_and_stop(manager, components):
    site = make_site(components["tmp_path"])
    events: list[str] = []
    manager.add_observer(events.append)
    service = manager.serve_folder("Site", str(site))
    url = f"http://127.0.0.1:{service.assigned_port}"
    with urllib.request.urlopen(url, timeout=3) as response:
        assert response.read() == b"manager-site"

    manager.stop_service(service.id)
    assert service.status == ServiceStatus.STOPPED
    assert manager.get_service(service.id) is not None
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(url, timeout=1)

    manager.restart_service(service.id)
    assert service.status == ServiceStatus.RUNNING
    with urllib.request.urlopen(url, timeout=3) as response:
        assert response.read() == b"manager-site"


def test_serve_folder_rejects_empty_dir(manager, components):
    empty = components["tmp_path"] / "empty"
    empty.mkdir()
    with pytest.raises(FolderInvalidError):
        manager.serve_folder("Empty", str(empty))


def test_monitor_flags_dead_port_service(manager):
    target, port = open_local_listener()
    service = manager.expose_port("Doomed", port)
    manager.start_monitor()
    target.close()
    assert wait_until(lambda: service.status == ServiceStatus.ERROR), (
        f"expected ERROR, got {service.status}"
    )


def test_monitor_recovers_port_service_when_upstream_returns(manager):
    target, port = open_local_listener()
    service = manager.expose_port("Recovering", port)
    manager.start_monitor()
    try:
        target.close()
        assert wait_until(lambda: service.status == ServiceStatus.ERROR), (
            f"expected ERROR, got {service.status}"
        )
        revived = listen_on(port)
        try:
            assert wait_until(lambda: service.status == ServiceStatus.RUNNING), (
                f"expected recovery to RUNNING, got {service.status}"
            )
        finally:
            revived.close()
    finally:
        target.close()


def test_saved_services_persisted(manager, components):
    site = make_site(components["tmp_path"])
    manager.serve_folder("Persisted", str(site))
    loaded = components["config"].load()
    assert [saved.name for saved in loaded.saved_services] == ["Persisted"]


def test_active_ports_reports_assigned(manager, components):
    site = make_site(components["tmp_path"])
    service = manager.serve_folder("Ports", str(site))
    assert service.assigned_port in manager.active_ports()


def test_sensitive_folder_detection(manager, tmp_path):
    import sys
    from pathlib import Path

    assert manager.is_sensitive_folder(Path.home()) is True
    assert manager.is_sensitive_folder(Path("/")) is True
    if sys.platform != "win32":
        assert manager.is_sensitive_folder(Path("/etc")) is True
    assert manager.is_sensitive_folder(tmp_path) is False

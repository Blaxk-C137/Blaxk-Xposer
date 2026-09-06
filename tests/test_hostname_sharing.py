"""Hostname (.local) sharing through ServiceManager with a fake publisher."""

import pytest

from config.config_store import ConfigStore
from network.mdns_publisher import MdnsPublisher
from network.network_detector import NetworkDetector
from network.qr_generator import QRGenerator
from services.service_manager import ServiceManager, ValidationError
from tests.test_service_manager import open_local_listener

import threading
import http.server


class FakePublisher:
    """Records publish/unpublish calls; publish success is configurable."""

    def __init__(self, publish_ok=True) -> None:
        self.publish_ok = publish_ok
        self.published: list[tuple[str, str]] = []
        self.unpublished: list[str] = []

    def publish(self, hostname: str, ip: str) -> bool:
        self.published.append((hostname, ip))
        return self.publish_ok

    def unpublish(self, hostname: str) -> None:
        self.unpublished.append(hostname)

    def stop_all(self) -> None:
        for hostname, _ in self.published:
            if hostname not in self.unpublished:
                self.unpublished.append(hostname)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args) -> None:
        pass


@pytest.fixture()
def stack(tmp_path):
    """ServiceManager wired to a FakePublisher and temp config."""
    publisher = FakePublisher()
    manager = ServiceManager(
        config_store=ConfigStore(path=tmp_path / "config.json"),
        detector=NetworkDetector(),
        qr_generator=QRGenerator(output_dir=tmp_path / "qr"),
        mdns_publisher=publisher,
        monitor_interval=0.1,
    )
    return manager, publisher, tmp_path


def test_expose_port_with_hostname_uses_local_url(stack):
    manager, publisher, _ = stack
    server, port = open_local_listener()
    try:
        service = manager.expose_port("My App", port, hostname="myapp")
    finally:
        server.close()
    assert service.lan_url == f"http://myapp.local:{service.assigned_port}"
    assert publisher.published == [("myapp.local", manager.current_lan_ip())]


def test_publish_failure_falls_back_to_ip_url(stack):
    manager, publisher, _ = stack
    publisher.publish_ok = False
    server, port = open_local_listener()
    try:
        service = manager.expose_port("App", port, hostname="myapp")
    finally:
        server.close()
    assert service.lan_url == (
        f"http://{manager.current_lan_ip()}:{service.assigned_port}"
    )
    # The chosen hostname is still recorded so a restart can retry it.
    assert service.hostname == "myapp.local"


def test_stop_service_unpublishes_hostname(stack):
    manager, publisher, _ = stack
    server, port = open_local_listener()
    try:
        service = manager.expose_port("App", port, hostname="myapp")
    finally:
        server.close()
    manager.stop_service(service.id)
    assert publisher.unpublished == ["myapp.local"]


def test_restart_service_republishes_hostname(stack):
    manager, publisher, _ = stack
    server, port = open_local_listener()
    try:
        service = manager.expose_port("App", port, hostname="myapp")
        manager.stop_service(service.id)
        manager.restart_service(service.id)
        service = manager.get_service(service.id)
    finally:
        server.close()
    assert service.lan_url == f"http://myapp.local:{service.assigned_port}"
    assert ("myapp.local", manager.current_lan_ip()) in publisher.published


def test_restart_failed_republish_falls_back_to_ip_url(stack):
    manager, publisher, _ = stack
    server, port = open_local_listener()
    try:
        service = manager.expose_port("App", port, hostname="myapp")
        manager.stop_service(service.id)
        publisher.publish_ok = False  # avahi dies between runs
        manager.restart_service(service.id)
        service = manager.get_service(service.id)
    finally:
        server.close()
    assert service.lan_url == (
        f"http://{manager.current_lan_ip()}:{service.assigned_port}"
    ), "restart kept a stale hostname URL after failed republish"


def test_serve_folder_with_hostname_uses_local_url(stack, tmp_path):
    manager, publisher, _ = stack
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("x", encoding="utf-8")
    service = manager.serve_folder("Site", str(site), hostname="mysite")
    assert service.lan_url == f"http://mysite.local:{service.assigned_port}"


def test_invalid_hostname_raises_validation(stack):
    manager, _, _ = stack
    server, port = open_local_listener()
    try:
        with pytest.raises(ValidationError):
            manager.expose_port("App", port, hostname="!!!")
    finally:
        server.close()


def test_hostname_persisted_in_saved_service(stack, tmp_path):
    manager, _, _ = stack
    site = tmp_path / "persist-site"
    site.mkdir()
    (site / "index.html").write_text("x", encoding="utf-8")
    service = manager.serve_folder("Site", str(site), hostname="kept")
    saved = manager._config_store.load().saved_services[-1]
    assert saved.hostname == "kept.local"
    assert saved.to_dict()["hostname"] == "kept.local"
    round_tripped = type(saved).from_dict(saved.to_dict())
    assert round_tripped.hostname == "kept.local"


def test_shutdown_unpublishes_all_hostnames(stack):
    manager, publisher, _ = stack
    site = stack[2] / "down-site"
    site.mkdir()
    (site / "index.html").write_text("x", encoding="utf-8")
    manager.serve_folder("Site", str(site), hostname="down")
    manager.shutdown()
    assert publisher.unpublished == ["down.local"]


# ---- UI wiring (GTK) ----

gi = pytest.importorskip("gi")
import gi as _gi  # noqa: E402

_gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402


def _gtk_ready() -> bool:
    try:
        result = Gtk.init_check()
        return bool(result[0] if isinstance(result, tuple) else result)
    except Exception:
        return False


pytestmark_ui = pytest.mark.skipif(not _gtk_ready(), reason="no display available")


def _pump(timeout: float = 2.0) -> None:
    """Run GTK events until quiet, allowing worker threads to finish."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.02)


class StubService:
    lan_url = "http://stub.local:8000"
    id = "stub-id"


class StubManager:
    """Records the exact arguments the form windows forward."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def expose_port(self, name, port, hostname=None):
        self.calls.append(("expose", name, port, hostname))
        return StubService()

    def serve_folder(self, name, folder_path, hostname=None):
        self.calls.append(("serve", name, folder_path, hostname))
        return StubService()

    @staticmethod
    def lan_reachable(_service) -> bool:
        return True

    @staticmethod
    def is_sensitive_folder(_folder) -> bool:
        return False


@ pytestmark_ui
def test_expose_window_forwards_hostname():
    from windows.expose_port_window import ExposePortWindow

    manager = StubManager()
    window = ExposePortWindow(manager, None, None)
    window._name_entry.set_text("App")
    window._hostname_entry.set_text("myapp")
    window._on_expose_clicked(None)
    _pump()
    assert manager.calls == [("expose", "App", 3000, "myapp")]
    if not window._destroyed:
        window.destroy()
    _pump(0.5)


@ pytestmark_ui
def test_expose_window_blank_hostname_forwards_none():
    from windows.expose_port_window import ExposePortWindow

    manager = StubManager()
    window = ExposePortWindow(manager, None, None)
    window._name_entry.set_text("App")
    window._hostname_entry.set_text("   ")
    window._on_expose_clicked(None)
    _pump()
    assert manager.calls == [("expose", "App", 3000, None)]
    if not window._destroyed:
        window.destroy()
    _pump(0.5)


@ pytestmark_ui
def test_serve_window_forwards_hostname(tmp_path):
    from windows.serve_folder_window import ServeFolderWindow

    manager = StubManager()
    site = tmp_path / "ui-site"
    site.mkdir()
    (site / "index.html").write_text("x", encoding="utf-8")
    window = ServeFolderWindow(manager, None)
    window._name_entry.set_text("Site")
    window._path_entry.set_text(str(site))
    window._hostname_entry.set_text("mysite")
    window._on_serve_clicked(None)
    _pump()
    assert manager.calls == [("serve", "Site", str(site), "mysite")]
    if not window._destroyed:
        window.destroy()
    _pump(0.5)

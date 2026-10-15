"""ServiceManager tests for the sharing scenarios users actually hit."""

import json
import socket
import ssl
import urllib.request

import pytest

from config.config_store import ConfigStore
from models.service import LocalProtocol, ServiceStatus, ShareScheme
from network.mdns_publisher import MdnsPublisher
from network.network_detector import InterfaceInfo, NetworkDetector
from network.qr_generator import QRGenerator
from services.service_manager import (
    PortAlreadySharedError,
    ServiceManager,
    build_url,
)
from tests.test_service_manager import wait_until
from tests.upstreams import BannerServer, DevAppServer, free_port

UNVERIFIED = ssl._create_unverified_context()


class FakeBackend:
    """mDNS backend that records calls instead of touching the network."""

    name = "fake"

    def __init__(self) -> None:
        self.published: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def publish(self, hostname: str, ip: str, port: int) -> bool:
        self.calls.append(("publish", hostname))
        self.published[hostname] = ip
        return True

    def unpublish(self, hostname: str) -> None:
        self.calls.append(("unpublish", hostname))
        self.published.pop(hostname, None)

    def close(self) -> None:
        self.published.clear()


class SwitchableDetector(NetworkDetector):
    """Detector whose LAN address the test controls."""

    def __init__(self, ip: str) -> None:
        self.ip = ip

    def get_lan_ip(self) -> str:
        return self.ip

    def list_interfaces(self) -> list[InterfaceInfo]:
        return [InterfaceInfo(name="wlan0", ip=self.ip)]


def fetch(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(
        request, timeout=5, context=UNVERIFIED if url.startswith("https") else None
    ) as response:
        return response.status, dict(response.headers), response.read()


@pytest.fixture()
def manager(components):
    yield components["manager"]


@pytest.fixture()
def apps():
    started: list[DevAppServer] = []

    def make(**options: object) -> DevAppServer:
        server = DevAppServer(**options)
        started.append(server)
        return server

    yield make
    for server in started:
        server.close()


def make_manager(tmp_path, backend=None, detector=None) -> ServiceManager:
    return ServiceManager(
        config_store=ConfigStore(path=tmp_path / "config.json"),
        detector=detector or NetworkDetector(),
        qr_generator=QRGenerator(output_dir=tmp_path / "qr"),
        mdns_publisher=MdnsPublisher(backends=[backend] if backend else []),
        monitor_interval=0.1,
    )


def test_ipv6_only_dev_server_can_be_shared(manager, apps):
    app = apps(host="::1")
    service = manager.expose_port("Vite", app.port)
    status, _, body = fetch(f"{service.ip_url}/echo")
    assert status == 200
    assert json.loads(body)["host"] == f"localhost:{app.port}"


def test_https_dev_server_gets_plain_http_link_by_default(manager, apps):
    app = apps(tls=True)
    service = manager.expose_port("Secure app", app.port)
    assert service.local_protocol == LocalProtocol.HTTPS
    assert service.share_scheme == ShareScheme.HTTP
    assert service.ip_url.startswith("http://")
    status, _, _ = fetch(f"{service.ip_url}/echo")
    assert status == 200


def test_https_dev_server_can_keep_an_https_link(manager, apps):
    app = apps(tls=True)
    service = manager.expose_port("Secure app", app.port, share_scheme=ShareScheme.HTTPS)
    assert service.share_scheme == ShareScheme.HTTPS
    assert service.ip_url.startswith("https://")
    status, _, _ = fetch(f"{service.ip_url}/echo")
    assert status == 200
    plain = service.ip_url.replace("https://", "http://")
    assert fetch(f"{plain}/echo")[0] == 200, "http:// must keep working on an https share"


def test_frontend_and_backend_work_together(manager, apps):
    frontend = apps()
    backend = apps()
    frontend.backend_port = backend.port
    backend.allowed_origins = {f"http://localhost:{frontend.port}"}
    front = manager.expose_port("Shop", frontend.port)
    _, _, script = fetch(f"{front.ip_url}/js")
    assert f"http://localhost:{backend.port}/api" in script.decode()
    manager.start_monitor()
    assert wait_until(lambda: [h.port for h in front.backend_hints] == [backend.port], timeout=3)
    back = manager.share_backend(front.id, backend.port)
    assert front.backend_hints == () or wait_until(lambda: front.backend_hints == (), timeout=3)
    _, _, script = fetch(f"{front.ip_url}/js")
    assert f"{back.ip_url}/api" in script.decode()
    origin = front.ip_url
    _, headers, body = fetch(f"{back.ip_url}/cors", {"Origin": origin})
    assert json.loads(body)["origin_seen"] == f"http://localhost:{frontend.port}"
    assert headers.get("Access-Control-Allow-Origin") == origin


def test_same_port_cannot_be_shared_twice(manager, apps):
    app = apps()
    manager.expose_port("One", app.port)
    with pytest.raises(PortAlreadySharedError):
        manager.expose_port("Two", app.port)


def test_sharing_again_reuses_the_stopped_record(tmp_path, apps):
    manager = make_manager(tmp_path, detector=SwitchableDetector("10.255.0.1"))
    try:
        app = apps()
        first = manager.expose_port("Old name", app.port)
        link = first.ip_url
        manager.stop_service(first.id)
        second = manager.expose_port("New name", app.port)
        assert second.id == first.id
        assert second.name == "New name"
        assert second.ip_url == link, "re-sharing should keep the same link"
        assert len(ConfigStore(path=tmp_path / "config.json").load().saved_services) == 1
    finally:
        manager.shutdown()


def test_saved_services_come_back_stopped(tmp_path, apps):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("hi", encoding="utf-8")
    app = apps()
    first = make_manager(tmp_path)
    folder = first.serve_folder("Site", str(site))
    first.expose_port("App", app.port)
    folder_port = folder.assigned_port
    first.shutdown()
    second = make_manager(tmp_path)
    try:
        second.restore_saved_services()
        restored = {service.name: service for service in second.all_services()}
        assert set(restored) == {"Site", "App"}
        assert all(service.status == ServiceStatus.STOPPED for service in restored.values())
        second.restart_service(restored["Site"].id)
        assert restored["Site"].assigned_port == folder_port, "folder links should be stable"
        assert fetch(restored["Site"].ip_url)[2] == b"hi"
    finally:
        second.shutdown()


def test_remove_service_forgets_everything(manager, apps, components):
    app = apps()
    service = manager.expose_port("Temp", app.port)
    manager.remove_service(service.id)
    assert manager.get_service(service.id) is None
    assert components["config"].load().saved_services == []
    assert manager.shared_local_ports() == set()


def test_server_first_protocol_is_relayed_raw(manager):
    banner = BannerServer()
    try:
        service = manager.expose_port("SSH-like", banner.port)
        assert service.share_scheme == ShareScheme.TCP
        assert "://" not in service.ip_url
        host, port = service.ip_url.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            assert sock.recv(64) == BannerServer.BANNER
    finally:
        banner.close()


def test_one_custom_link_for_two_services_is_reference_counted(tmp_path, apps):
    backend_mdns = FakeBackend()
    manager = make_manager(tmp_path, backend=backend_mdns)
    try:
        frontend = manager.expose_port("Front", apps().port, hostname="shop")
        api = manager.expose_port("API", apps().port, hostname="shop")
        assert frontend.lan_url.startswith("http://shop.local:")
        assert api.lan_url.startswith("http://shop.local:")
        assert backend_mdns.calls.count(("publish", "shop.local")) == 1
        manager.stop_service(frontend.id)
        assert "shop.local" in backend_mdns.published, "stopping one service broke the other's link"
        manager.stop_service(api.id)
        assert "shop.local" not in backend_mdns.published
    finally:
        manager.shutdown()


def test_backend_shared_from_hint_reuses_custom_link(tmp_path, apps):
    backend_mdns = FakeBackend()
    manager = make_manager(tmp_path, backend=backend_mdns)
    try:
        frontend_app, backend_app = apps(), apps()
        front = manager.expose_port("Shop", frontend_app.port, hostname="shop")
        back = manager.share_backend(front.id, backend_app.port)
        assert back.hostname == "shop.local"
        assert back.lan_url == f"http://shop.local:{back.assigned_port}"
    finally:
        manager.shutdown()


def test_custom_link_failure_keeps_ip_link(manager, apps):
    service = manager.expose_port("No mdns", apps().port, hostname="nomdns")
    assert service.hostname_active is False
    assert service.lan_url == service.ip_url
    assert service.qr_custom_path is None


def test_lan_ip_change_updates_links_and_qr(tmp_path, apps):
    detector = SwitchableDetector("10.255.0.1")
    manager = make_manager(tmp_path, detector=detector)
    try:
        app = apps()
        service = manager.expose_port("Roaming", app.port)
        first_qr = service.qr_image_path
        assert service.ip_url.startswith("http://10.255.0.1:")
        manager.start_monitor()
        detector.ip = "10.255.0.2"
        assert wait_until(lambda: service.ip_url.startswith("http://10.255.0.2:"), timeout=3)
        assert service.qr_image_path != first_qr
        endpoint = manager._proxy_engine.rewrite_map().for_local_port(app.port)
        assert endpoint is not None and endpoint.ip_host == "10.255.0.2"
        assert fetch(f"http://127.0.0.1:{service.assigned_port}/echo")[0] == 200
    finally:
        manager.shutdown()


def test_detected_ports_hide_xposer_listeners(manager, apps):
    app = apps()
    service = manager.expose_port("Visible", app.port)
    entries = {entry.port: entry for entry in manager.detected_ports()}
    assert app.port in entries
    assert "127.0.0.1" in entries[app.port].bind_addresses
    reported = {
        (address, entry.port) for entry in entries.values() for address in entry.bind_addresses
    }
    own = manager._own_listeners()
    assert own and not own & reported, "Xposer's own sockets leaked into the list"
    assert service.status == ServiceStatus.RUNNING


def test_smart_proxy_setting_persists(manager, components):
    manager.set_smart_proxy(False)
    assert components["config"].load().smart_proxy is False
    assert manager.smart_proxy_enabled() is False
    manager.set_smart_proxy(True)
    assert manager.smart_proxy_enabled() is True


def test_exposing_xposers_own_port_is_rejected(manager, apps):
    service = manager.expose_port("App", apps().port)
    from services.service_manager import ValidationError

    with pytest.raises(ValidationError):
        manager.expose_port("Loop", service.assigned_port)


def test_build_url_forms():
    assert build_url(ShareScheme.HTTP, "192.168.1.5", 80) == "http://192.168.1.5:80"
    assert build_url(ShareScheme.HTTPS, "app.local", 5443) == "https://app.local:5443"
    assert build_url(ShareScheme.TCP, "192.168.1.5", 22) == "192.168.1.5:22"
    assert build_url(ShareScheme.HTTP, "fe80::1", 8000) == "http://[fe80::1]:8000"


def test_proxy_candidates_prefer_previous_then_same_port(manager):
    candidates = manager._proxy_candidates(5173, 0, "192.168.1.5")
    assert candidates[0] == ("192.168.1.5", 5173)
    assert candidates[1] == ("0.0.0.0", 15173)
    again = manager._proxy_candidates(5173, 15173, "192.168.1.5")
    assert again[0] == ("0.0.0.0", 15173)
    offline = manager._proxy_candidates(5173, 0, "127.0.0.1")
    assert offline[0] == ("0.0.0.0", 15173)
    assert free_port() > 0


def test_client_first_non_web_service_is_relayed_raw(manager):
    from tests.upstreams import ScriptedServer

    redis_like = ScriptedServer(b"-ERR unknown command\r\n", echo_after=True)
    try:
        service = manager.expose_port("Redis-like", redis_like.port)
        assert service.share_scheme == ShareScheme.TCP
        host, port = service.ip_url.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            sock.sendall(b"PING\r\n")
            assert sock.recv(64) == b"-ERR unknown command\r\n"
            sock.sendall(b"again")
            assert sock.recv(64) == b"again"
    finally:
        redis_like.close()


def test_restore_collapses_duplicate_records_from_older_versions(tmp_path):
    from models.service import SavedService, ServiceType

    store = ConfigStore(path=tmp_path / "config.json")
    config = store.load()
    config.saved_services = [
        SavedService(id=f"old-{index}", name=f"My App v{index}", type=ServiceType.PORT, port=3000)
        for index in range(5)
    ] + [SavedService(id="site", name="Site", type=ServiceType.FOLDER, folder_path="/srv/site")]
    store.save(config)
    manager = make_manager(tmp_path)
    try:
        manager.restore_saved_services()
        names = sorted(service.name for service in manager.all_services())
        assert names == ["My App v4", "Site"]
        assert len(store.load().saved_services) == 2
    finally:
        manager.shutdown()

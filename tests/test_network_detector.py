"""Tests for stateless network detection."""

import socket

import pytest

from network.network_detector import NetworkDetector


@pytest.fixture()
def detector() -> NetworkDetector:
    return NetworkDetector()


def open_local_listener() -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server


def test_validate_port_bounds(detector):
    assert detector.validate_port(1) is True
    assert detector.validate_port(65535) is True
    assert detector.validate_port(0) is False
    assert detector.validate_port(65536) is False
    assert detector.validate_port(-5) is False


def test_is_port_in_use(detector):
    server = open_local_listener()
    port = server.getsockname()[1]
    try:
        assert detector.is_port_in_use(port) is True
    finally:
        server.close()
    assert detector.is_port_in_use(port) is False


def test_detect_listening_ports_finds_listener(detector):
    server = open_local_listener()
    port = server.getsockname()[1]
    try:
        found = {entry.port: entry for entry in detector.detect_listening_ports()}
    finally:
        server.close()
    assert port in found
    assert found[port].process_name
    assert found[port].port == port


def test_find_available_port_is_bindable(detector):
    port = detector.find_available_port()
    assert port is not None
    assert detector.is_port_bindable(port)


def test_get_lan_ip_returns_address(detector):
    ip = detector.get_lan_ip()
    assert isinstance(ip, str)
    assert len(ip.split(".")) == 4


def test_list_interfaces_returns_entries(detector):
    interfaces = detector.list_interfaces()
    assert isinstance(interfaces, list)


# ---- protocol probing ----

from network.network_detector import UpstreamKind  # noqa: E402
from tests.upstreams import BannerServer, DevAppServer, ScriptedServer  # noqa: E402


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (lambda: DevAppServer(), UpstreamKind.HTTP),
        (lambda: DevAppServer(tls=True), UpstreamKind.TLS),
        (lambda: BannerServer(), UpstreamKind.SERVER_FIRST),
        (lambda: ScriptedServer(b"-ERR unknown command\r\n"), UpstreamKind.NON_HTTP),
        (lambda: ScriptedServer(b"HTTP/1.1 400 Bad Request\r\n\r\n"), UpstreamKind.HTTP),
        (lambda: ScriptedServer(None), UpstreamKind.HTTP),
    ],
)
def test_probe_classifies_local_servers(detector, factory, expected):
    server = factory()
    try:
        probe = detector.probe_upstream(server.port)
        assert probe.reachable and probe.host == "127.0.0.1"
        assert probe.kind == expected
    finally:
        server.close()


def test_probe_finds_ipv6_only_server(detector):
    server = DevAppServer(host="::1")
    try:
        probe = detector.probe_upstream(server.port)
        assert probe.host == "::1"
        assert probe.kind == UpstreamKind.HTTP
    finally:
        server.close()


def test_probe_reports_unreachable_port(detector):
    port = detector.find_available_port()
    assert detector.probe_upstream(port).reachable is False


@pytest.mark.parametrize(
    ("command", "label"),
    [
        (["node", "/app/node_modules/.bin/vite", "--port", "5173"], "Vite"),
        (["next-server (v16.4.1)"], "Next.js"),
        (["C:\\Program Files\\nodejs\\node.exe", "C:\\app\\node_modules\\next\\dist\\bin\\next", "dev"], "Next.js"),
        (["python3", "manage.py", "runserver"], "Django"),
        (["/usr/bin/python3", "-m", "uvicorn", "main:app"], "Uvicorn"),
        (["python", "-m", "http.server", "8000"], "Python http.server"),
        (["bash"], ""),
    ],
)
def test_app_labels_from_command_lines(command, label):
    from network.network_detector import detect_app_label

    assert detect_app_label(command) == label


def test_display_names():
    from network.network_detector import ListeningPort

    assert ListeningPort(5173, 1, "node", app_label="Vite").display_name == "Vite · node"
    assert ListeningPort(3000, 1, "next-server (v16.4.1)", app_label="Next.js").display_name == "Next.js"
    assert ListeningPort(22, None, "Unknown").display_name == "Unknown"


def test_lan_ip_prefers_physical_private_interfaces():
    from network.network_detector import InterfaceInfo, choose_lan_ip

    interfaces = [
        InterfaceInfo("docker0", "172.17.0.1", True, True),
        InterfaceInfo("tun0", "10.8.0.2", True, True),
        InterfaceInfo("wlan0", "192.168.1.5"),
    ]
    assert choose_lan_ip(interfaces, route_ip="10.8.0.2") == "192.168.1.5"
    assert choose_lan_ip([InterfaceInfo("tailscale0", "100.101.1.2", True, True)], None) == "100.101.1.2"
    assert choose_lan_ip([], None) == "127.0.0.1"
    assert choose_lan_ip([InterfaceInfo("eth0", "10.0.0.4"), InterfaceInfo("wlan0", "192.168.1.9")], "192.168.1.9") == "192.168.1.9"

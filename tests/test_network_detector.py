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

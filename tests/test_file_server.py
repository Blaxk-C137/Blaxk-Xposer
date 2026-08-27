"""Tests for the static file server."""

import socket
import urllib.error
import urllib.request

import pytest

from services.file_server import FileServerError, FileServer


def make_site(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("hello-xposer", encoding="utf-8")
    return site


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=3) as response:
        return response.read()


def test_serves_index_html(tmp_path):
    site = make_site(tmp_path)
    port_probe = socket.socket()
    port_probe.bind(("", 0))
    port = port_probe.getsockname()[1]
    port_probe.close()

    server = FileServer()
    server.start_server("svc", site, port)
    try:
        assert fetch(f"http://127.0.0.1:{port}/") == b"hello-xposer"
        assert server.is_running("svc") is True
        assert server.connected_devices("svc") >= 1
    finally:
        server.stop_all()


def test_stop_makes_url_unreachable(tmp_path):
    site = make_site(tmp_path)
    probe = socket.socket()
    probe.bind(("", 0))
    port = probe.getsockname()[1]
    probe.close()

    server = FileServer()
    server.start_server("svc", site, port)
    server.stop_server("svc")
    assert server.is_running("svc") is False
    with pytest.raises(urllib.error.URLError):
        fetch(f"http://127.0.0.1:{port}/")


def test_missing_directory_raises(tmp_path):
    server = FileServer()
    with pytest.raises(FileServerError):
        server.start_server("svc", tmp_path / "nope", 8080)

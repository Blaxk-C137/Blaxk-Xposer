"""Tests for the static file server."""

import http.client
import os
import socket
import urllib.error
import urllib.request

import pytest

from models.service import FolderOptions
from services.file_server import FileServer, FileServerError, parse_byte_range


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


# ---- hardened serving ----


def start(site, options=None):
    server = FileServer()
    port = free_test_port()
    server.start_server("svc", site, port, options)
    return server, port


def free_test_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def request(port, path, headers=None, method="GET"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request(method, path, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response, body


@pytest.fixture()
def rich_site(tmp_path):
    site = tmp_path / "rich"
    site.mkdir()
    (site / "index.html").write_text("<h1>home</h1>", encoding="utf-8")
    (site / "app.js").write_text("export const x = 1;", encoding="utf-8")
    (site / "module.mjs").write_text("export {};", encoding="utf-8")
    (site / "mod.wasm").write_bytes(b"\x00asm")
    (site / "video.mp4").write_bytes(bytes(range(256)) * 40)
    (site / ".env").write_text("SECRET=1", encoding="utf-8")
    (site / ".git").mkdir()
    (site / ".git" / "config").write_text("[core]", encoding="utf-8")
    (site / ".well-known").mkdir()
    (site / ".well-known" / "assetlinks.json").write_text("[]", encoding="utf-8")
    (site / "docs").mkdir()
    (site / "docs" / "readme.txt").write_text("docs", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        os.symlink(outside, site / "escape.txt")
    except (OSError, NotImplementedError):
        pass
    return site


def test_web_mime_types_are_fixed(rich_site):
    server, port = start(rich_site)
    try:
        assert (
            request(port, "/app.js")[0].getheader("Content-Type")
            == "text/javascript; charset=utf-8"
        )
        assert (
            request(port, "/module.mjs")[0].getheader("Content-Type")
            == "text/javascript; charset=utf-8"
        )
        assert request(port, "/mod.wasm")[0].getheader("Content-Type") == "application/wasm"
        assert request(port, "/video.mp4")[0].getheader("Content-Type") == "video/mp4"
    finally:
        server.stop_all()


def test_byte_ranges_for_video_seeking(rich_site):
    server, port = start(rich_site)
    try:
        whole = bytes(range(256)) * 40
        response, body = request(port, "/video.mp4", {"Range": "bytes=100-199"})
        assert response.status == 206
        assert response.getheader("Content-Range") == f"bytes 100-199/{len(whole)}"
        assert body == whole[100:200]
        tail, tail_body = request(port, "/video.mp4", {"Range": "bytes=-10"})
        assert tail.status == 206 and tail_body == whole[-10:]
        bad, _ = request(port, "/video.mp4", {"Range": f"bytes={len(whole) + 5}-"})
        assert bad.status == 416
        full, full_body = request(port, "/video.mp4")
        assert full.status == 200 and full_body == whole
        assert full.getheader("Accept-Ranges") == "bytes"
    finally:
        server.stop_all()


def test_hidden_files_are_not_served_by_default(rich_site):
    server, port = start(rich_site)
    try:
        assert request(port, "/.env")[0].status == 404
        assert request(port, "/.git/config")[0].status == 404
        assert request(port, "/.well-known/assetlinks.json")[0].status == 200
        listing = request(port, "/")[1].decode()
        assert ".env" not in listing and ".git" not in listing
    finally:
        server.stop_all()


def test_hidden_files_can_be_enabled(rich_site):
    server, port = start(rich_site, FolderOptions(show_hidden=True))
    try:
        assert request(port, "/.env")[1] == b"SECRET=1"
    finally:
        server.stop_all()


def test_symlinks_cannot_escape_the_folder(rich_site):
    if not (rich_site / "escape.txt").is_symlink():
        pytest.skip("symlinks unavailable on this system")
    server, port = start(rich_site)
    try:
        assert request(port, "/escape.txt")[0].status == 404
    finally:
        server.stop_all()


def test_spa_fallback_serves_index_for_client_routes(rich_site):
    server, port = start(rich_site, FolderOptions(spa_fallback=True))
    try:
        response, body = request(port, "/dashboard/settings", {"Accept": "text/html"})
        assert response.status == 200 and body == b"<h1>home</h1>"
        assert request(port, "/missing.js", {"Accept": "*/*"})[0].status == 404
    finally:
        server.stop_all()
    plain, plain_port = start(rich_site)
    try:
        assert request(plain_port, "/dashboard", {"Accept": "text/html"})[0].status == 404
    finally:
        plain.stop_all()


def test_conditional_requests_and_no_cache(rich_site):
    server, port = start(rich_site)
    try:
        response, _ = request(port, "/app.js")
        etag = response.getheader("ETag")
        assert response.getheader("Cache-Control") == "no-cache"
        assert request(port, "/app.js", {"If-None-Match": etag})[0].status == 304
        modified = response.getheader("Last-Modified")
        assert request(port, "/app.js", {"If-Modified-Since": modified})[0].status == 304
    finally:
        server.stop_all()


def test_keep_alive_and_directory_redirect(rich_site):
    server, port = start(rich_site)
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        for path in ("/app.js", "/index.html", "/docs/readme.txt"):
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 200
            response.read()
        connection.request("GET", "/docs")
        redirect = connection.getresponse()
        redirect.read()
        assert redirect.status == 301 and redirect.getheader("Location") == "/docs/"
        connection.close()
        listing = request(port, "/docs/")[1].decode()
        assert "readme.txt" in listing and "Parent folder" in listing
    finally:
        server.stop_all()


def test_head_requests_have_headers_only(rich_site):
    server, port = start(rich_site)
    try:
        response, body = request(port, "/video.mp4", method="HEAD")
        assert response.status == 200
        assert response.getheader("Content-Length") == str(256 * 40)
        assert body == b""
    finally:
        server.stop_all()


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        ("bytes=0-9", 100, (0, 9)),
        ("bytes=90-", 100, (90, 99)),
        ("bytes=-5", 100, (95, 99)),
        ("bytes=50-500", 100, (50, 99)),
        ("bytes=100-", 100, "unsatisfiable"),
        ("bytes=0-1,5-6", 100, None),
        ("items=0-1", 100, None),
        ("bytes=9-3", 100, None),
        ("bytes=abc", 100, None),
    ],
)
def test_parse_byte_range(header, size, expected):
    assert parse_byte_range(header, size) == expected

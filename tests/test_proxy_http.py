"""End-to-end tests of the smart proxy against realistic local apps.

The proxy listens on 127.0.0.1 and requests carry LAN-style Host headers
(192.168.1.50:PORT or myapp.local:PORT), which is exactly what a phone
sends; the share map tells the proxy those are its public addresses.
"""

import gzip
import http.client
import json
import socket
import ssl
import threading
import time

import pytest

from services.http_rewriter import PublicEndpoint, RewriteMap
from services.proxy_engine import ProxyEngine
from tests.upstreams import (
    BannerServer,
    DevAppServer,
    WebSocketEchoServer,
    free_port,
    read_head,
    websocket_handshake,
)

LAN_IP = "192.168.1.50"
CUSTOM_HOST = "myapp.local"
API_HOST = "api.local"


@pytest.fixture()
def engine():
    proxy = ProxyEngine()
    yield proxy
    proxy.stop_all()


@pytest.fixture()
def app():
    server = DevAppServer()
    yield server
    server.close()


@pytest.fixture()
def backend():
    server = DevAppServer()
    yield server
    server.close()


def share(engine: ProxyEngine, app_port: int, **options: object) -> int:
    """Start a proxy for app_port on a free loopback port; return that port."""
    listen_port = free_port()
    engine.start_proxy(f"svc-{app_port}", "127.0.0.1", listen_port, app_port, **options)
    return listen_port


def publish(engine: ProxyEngine, *endpoints: PublicEndpoint, version: int = 1) -> None:
    """Install a share map."""
    engine.set_rewrite_map(RewriteMap(endpoints, version))


def get(
    port: int,
    path: str,
    host: str,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: bytes | None = None,
) -> tuple[http.client.HTTPResponse, bytes]:
    """One request through the proxy with an explicit Host header."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, body=body, headers={"Host": host, **(headers or {})})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response, data


# ---- custom links and host checks ----


def test_custom_link_passes_dev_server_host_check(engine, app):
    app.require_local_host = True
    port = share(engine, app.port)
    publish(engine, PublicEndpoint(app.port, port, LAN_IP, CUSTOM_HOST))
    response, body = get(port, "/echo", f"{CUSTOM_HOST}:{port}")
    assert response.status == 200, body
    assert json.loads(body)["host"] == f"localhost:{app.port}"


def test_ip_link_passes_host_check_without_share_map(engine, app):
    app.require_local_host = True
    port = share(engine, app.port)
    response, body = get(port, "/echo", f"{LAN_IP}:{port}")
    assert response.status == 200, body


def test_ipv6_only_app_is_reachable():
    app = DevAppServer(host="::1")
    engine = ProxyEngine()
    try:
        port = share(engine, app.port)
        response, body = get(port, "/echo", f"{LAN_IP}:{port}")
        assert response.status == 200
        assert json.loads(body)["host"] == f"localhost:{app.port}"
    finally:
        engine.stop_all()
        app.close()


def test_origin_and_referer_become_local(engine, app):
    port = share(engine, app.port)
    publish(engine, PublicEndpoint(app.port, port, LAN_IP, CUSTOM_HOST))
    _, body = get(
        port,
        "/echo",
        f"{CUSTOM_HOST}:{port}",
        {
            "Origin": f"http://{CUSTOM_HOST}:{port}",
            "Referer": f"http://{CUSTOM_HOST}:{port}/page?q=1",
        },
    )
    seen = json.loads(body)
    assert seen["origin"] == f"http://localhost:{app.port}"
    assert seen["referer"] == f"http://localhost:{app.port}/page?q=1"
    assert seen["forwarded_for"] == "127.0.0.1"


def test_foreign_origin_is_left_alone(engine, app):
    port = share(engine, app.port)
    publish(engine, PublicEndpoint(app.port, port, LAN_IP))
    _, body = get(port, "/echo", f"{LAN_IP}:{port}", {"Origin": "https://evil.example"})
    assert json.loads(body)["origin"] == "https://evil.example"


# ---- frontend + backend ----


def test_backend_cors_allow_list_accepts_lan_frontend(engine, app, backend):
    backend.allowed_origins = {f"http://localhost:{app.port}"}
    front_port = share(engine, app.port)
    back_port = share(engine, backend.port)
    publish(
        engine,
        PublicEndpoint(app.port, front_port, LAN_IP),
        PublicEndpoint(backend.port, back_port, LAN_IP),
    )
    lan_origin = f"http://{LAN_IP}:{front_port}"
    preflight, _ = get(
        back_port,
        "/cors",
        f"{LAN_IP}:{back_port}",
        {"Origin": lan_origin, "Access-Control-Request-Method": "POST"},
        method="OPTIONS",
    )
    assert preflight.status == 204
    assert preflight.getheader("Access-Control-Allow-Origin") == lan_origin
    response, body = get(back_port, "/cors", f"{LAN_IP}:{back_port}", {"Origin": lan_origin})
    assert json.loads(body)["origin_seen"] == f"http://localhost:{app.port}"
    assert response.getheader("Access-Control-Allow-Origin") == lan_origin


def test_frontend_code_points_at_shared_backend(engine, app, backend):
    app.backend_port = backend.port
    app.unshared_port = free_port()
    front_port = share(engine, app.port)
    back_port = share(engine, backend.port)
    publish(
        engine,
        PublicEndpoint(app.port, front_port, LAN_IP, CUSTOM_HOST),
        PublicEndpoint(backend.port, back_port, LAN_IP, API_HOST),
    )
    _, by_ip = get(front_port, "/js", f"{LAN_IP}:{front_port}")
    text = by_ip.decode()
    assert f'"http://{LAN_IP}:{back_port}/api"' in text
    assert f'"ws://{LAN_IP}:{front_port}/hmr"' in text
    assert f'"http:\\/\\/{LAN_IP}:{back_port}\\/x"' in text
    assert f"http://localhost:{app.unshared_port}/y" in text
    _, by_name = get(front_port, "/js", f"{CUSTOM_HOST}:{front_port}")
    assert f'"http://{API_HOST}:{back_port}/api"' in by_name.decode()
    assert f'"ws://{CUSTOM_HOST}:{front_port}/hmr"' in by_name.decode()
    stats = engine.stats(f"svc-{app.port}")
    assert stats is not None and app.unshared_port in stats.unresolved_ports


def test_redirect_to_localhost_keeps_the_link_the_device_used(engine, app):
    port = share(engine, app.port)
    publish(engine, PublicEndpoint(app.port, port, LAN_IP, CUSTOM_HOST))
    response, _ = get(port, "/redirect", f"{CUSTOM_HOST}:{port}")
    assert response.status == 302
    assert response.getheader("Location") == f"http://{CUSTOM_HOST}:{port}/target?x=1"


def test_etag_is_versioned_and_revalidates(engine, app):
    port = share(engine, app.port)
    publish(engine, PublicEndpoint(app.port, port, LAN_IP), version=3)
    response, body = get(port, "/etag", f"{LAN_IP}:{port}")
    etag = response.getheader("ETag")
    assert etag == 'W/"v1.xp3s"'
    assert response.getheader("Last-Modified") is None
    assert f"http://{LAN_IP}:{port}/self" in body.decode()
    revalidated, _ = get(port, "/etag", f"{LAN_IP}:{port}", {"If-None-Match": etag})
    assert revalidated.status == 304
    assert revalidated.getheader("ETag") == 'W/"v1.xp3s"'
    publish(engine, PublicEndpoint(app.port, port, LAN_IP), version=4)
    stale, _ = get(port, "/etag", f"{LAN_IP}:{port}", {"If-None-Match": etag})
    assert stale.status == 200


# ---- HTTPS dev servers ----


def test_https_app_is_shared_over_plain_http(engine):
    app = DevAppServer(tls=True)
    try:
        port = share(engine, app.port, upstream_tls=True)
        publish(engine, PublicEndpoint(app.port, port, LAN_IP, upstream_tls=True))
        response, body = get(
            port, "/echo", f"{LAN_IP}:{port}", {"Origin": f"http://{LAN_IP}:{port}"}
        )
        assert response.status == 200
        seen = json.loads(body)
        assert seen["host"] == f"localhost:{app.port}"
        assert seen["origin"] == f"https://localhost:{app.port}"
    finally:
        app.close()


def test_https_app_cookies_survive_plain_http(engine):
    app = DevAppServer(tls=True)
    try:
        port = share(engine, app.port, upstream_tls=True)
        publish(engine, PublicEndpoint(app.port, port, LAN_IP, upstream_tls=True))
        response, _ = get(port, "/cookie", f"{LAN_IP}:{port}")
        cookies = response.headers.get_all("Set-Cookie")
        assert "sid=abc; Path=/; HttpOnly" in cookies
        assert "XpHost__csrf=t0k; Path=/; SameSite=Lax" in cookies
        _, body = get(port, "/echo", f"{LAN_IP}:{port}", {"Cookie": "sid=abc; XpHost__csrf=t0k"})
        assert json.loads(body)["cookie"] == "sid=abc; __Host-csrf=t0k"
    finally:
        app.close()


def test_https_csp_and_hsts_are_adapted(engine, backend):
    app = DevAppServer(tls=True)
    try:
        app.backend_port = backend.port
        port = share(engine, app.port, upstream_tls=True)
        back_port = share(engine, backend.port)
        publish(
            engine,
            PublicEndpoint(app.port, port, LAN_IP, upstream_tls=True),
            PublicEndpoint(backend.port, back_port, LAN_IP),
        )
        response, _ = get(port, "/csp", f"{LAN_IP}:{port}")
        csp = response.getheader("Content-Security-Policy")
        assert f"connect-src http://{LAN_IP}:{back_port}" in csp
        assert "upgrade-insecure-requests" not in csp
        assert response.getheader("Strict-Transport-Security") is None
    finally:
        app.close()


def test_https_link_passes_tls_through(engine):
    app = DevAppServer(tls=True)
    try:
        port = share(engine, app.port, upstream_tls=True)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection("127.0.0.1", port, timeout=5, context=context)
        connection.request("GET", "/echo", headers={"Host": f"localhost:{app.port}"})
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["host"] == f"localhost:{app.port}"
        connection.close()
    finally:
        app.close()


# ---- streaming, compression, framing ----


def test_gzip_body_is_decoded_rewritten_and_recompressed(engine, app, backend):
    app.backend_port = backend.port
    port = share(engine, app.port)
    back_port = share(engine, backend.port)
    publish(
        engine,
        PublicEndpoint(app.port, port, LAN_IP),
        PublicEndpoint(backend.port, back_port, LAN_IP),
    )
    response, body = get(port, "/gzip", f"{LAN_IP}:{port}", {"Accept-Encoding": "gzip"})
    assert response.getheader("Content-Encoding") == "gzip"
    text = gzip.decompress(body).decode()
    assert text.count(f"http://{LAN_IP}:{back_port}/api") == 50
    plain_response, plain = get(port, "/gzip", f"{LAN_IP}:{port}")
    assert plain_response.getheader("Content-Encoding") is None
    assert f"http://{LAN_IP}:{back_port}/api" in plain.decode()


def test_app_receives_identity_encoding_request(engine, app):
    port = share(engine, app.port)
    _, body = get(port, "/echo", f"{LAN_IP}:{port}", {"Accept-Encoding": "gzip, br"})
    assert (
        json.loads(gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body)["accept_encoding"]
        == "identity"
    )


def test_event_stream_is_not_buffered(engine, app):
    port = share(engine, app.port)
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(f"GET /sse HTTP/1.1\r\nHost: {LAN_IP}:{port}\r\n\r\n".encode())
    received = b""
    deadline = time.monotonic() + 3
    while b"data: first" not in received and time.monotonic() < deadline:
        received += sock.recv(4096)
    assert b"data: first" in received, "first event was held back"
    assert b"data: second" not in received
    app.sse_release.set()
    while b"0\r\n\r\n" not in received and time.monotonic() < deadline + 3:
        chunk = sock.recv(4096)
        if not chunk:
            break
        received += chunk
    assert b"data: second" in received
    sock.close()


def test_keep_alive_serves_many_requests_on_one_connection(engine, app):
    port = share(engine, app.port)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    for index in range(5):
        connection.request("GET", f"/echo?n={index}", headers={"Host": f"{LAN_IP}:{port}"})
        response = connection.getresponse()
        assert json.loads(response.read())["path"] == f"/echo?n={index}"
    connection.close()


def test_uploads_with_length_and_chunked_bodies(engine, app):
    port = share(engine, app.port)
    payload = bytes(range(256)) * 2000
    _, body = get(port, "/upload", f"{LAN_IP}:{port}", method="POST", body=payload)
    assert json.loads(body)["length"] == len(payload)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(
        "POST",
        "/upload",
        body=iter([payload[:1000], payload[1000:]]),
        headers={"Host": f"{LAN_IP}:{port}"},
        encode_chunked=True,
    )
    result = json.loads(connection.getresponse().read())
    assert result["length"] == len(payload)
    connection.close()


def test_expect_100_continue(engine, app):
    port = share(engine, app.port)
    body = b"x" * 5000
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(
        (
            f"POST /upload HTTP/1.1\r\nHost: {LAN_IP}:{port}\r\nContent-Length: {len(body)}\r\n"
            "Expect: 100-continue\r\n\r\n"
        ).encode()
    )
    interim = read_head(sock)
    assert interim.startswith(b"HTTP/1.1 100")
    sock.sendall(body)
    final = b""
    while b'"length"' not in final:
        chunk = sock.recv(4096)
        if not chunk:
            break
        final += chunk
    assert b"200" in final.split(b"\r\n", 1)[0]
    assert b'"length": 5000' in final
    sock.close()


def test_close_delimited_body_is_rechunked(engine, app):
    port = share(engine, app.port)
    response, body = get(port, "/close", f"{LAN_IP}:{port}")
    assert response.status == 200
    assert body.decode() == f"closing body http://{LAN_IP}:{port}/z"


def test_head_has_no_body(engine, app):
    port = share(engine, app.port)
    response, body = get(port, "/echo", f"{LAN_IP}:{port}", method="HEAD")
    assert response.status == 200
    assert body == b""


def test_binary_bodies_pass_through_intact(engine, app):
    port = share(engine, app.port)
    response, body = get(port, "/big", f"{LAN_IP}:{port}")
    assert response.getheader("Content-Length") == str(256 * 4096)
    assert body == bytes(range(256)) * 4096


def test_stale_upstream_keepalive_is_retried(engine):
    app = DevAppServer()
    original = app.RequestHandlerClass.do_GET

    def closing_get(handler: object) -> None:
        original(handler)
        handler.close_connection = True

    app.RequestHandlerClass = type("Closing", (app.RequestHandlerClass,), {"do_GET": closing_get})
    try:
        port = share(engine, app.port)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        for _ in range(3):
            connection.request("GET", "/echo", headers={"Host": f"{LAN_IP}:{port}"})
            response = connection.getresponse()
            assert response.status == 200
            response.read()
        connection.close()
    finally:
        app.close()


# ---- WebSockets ----


def test_websocket_upgrade_with_lan_origin(engine):
    echo = WebSocketEchoServer()
    try:
        port = share(engine, echo.port)
        publish(engine, PublicEndpoint(echo.port, port, LAN_IP, CUSTOM_HOST))
        sock = websocket_handshake(
            "127.0.0.1", port, f"{CUSTOM_HOST}:{port}", f"http://{CUSTOM_HOST}:{port}"
        )
        head = read_head(sock)
        assert head.startswith(b"HTTP/1.1 101"), head
        assert echo.seen_origins[-1] == f"http://localhost:{echo.port}"
        sock.sendall(b"frame-bytes")
        assert sock.recv(64) == b"frame-bytes"
        sock.close()
    finally:
        echo.close()


# ---- failure handling and raw modes ----


def test_friendly_page_while_app_is_down(engine):
    port = share(engine, free_port(), service_name="My App")
    response, body = get(port, "/", f"{LAN_IP}:{port}")
    assert response.status == 502
    assert b"My App isn" in body
    assert b'http-equiv="refresh"' in body


def test_malformed_request_gets_400(engine, app):
    port = share(engine, app.port)
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(b"GET / HTTP/1.1\r\nBroken header line\r\n\r\n")
    assert read_head(sock).startswith(b"HTTP/1.1 400")
    sock.close()


def test_smart_mode_off_relays_bytes_unchanged(engine, app):
    app.require_local_host = True
    engine.set_smart_mode(False)
    port = share(engine, app.port)
    response, _ = get(port, "/echo", f"{CUSTOM_HOST}:{port}")
    assert response.status == 403


def test_server_first_protocol_in_raw_mode(engine):
    banner = BannerServer()
    try:
        port = share(engine, banner.port, raw_mode=True)
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert sock.recv(64) == BannerServer.BANNER
        sock.sendall(b"PING\r\n")
        assert sock.recv(64) == b"PING\r\n"
        sock.close()
    finally:
        banner.close()


def test_non_http_client_falls_back_to_raw(engine):
    echo = BannerServer()
    try:
        port = share(engine, echo.port)
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(b"\x00\x01binary-protocol")
        received = b""
        while b"binary-protocol" not in received:
            received += sock.recv(64)
        assert received.startswith(BannerServer.BANNER)
        sock.close()
    finally:
        echo.close()


def test_device_and_request_counters(engine, app):
    port = share(engine, app.port)
    for _ in range(3):
        get(port, "/echo", f"{LAN_IP}:{port}")
    stats = engine.stats(f"svc-{app.port}")
    assert stats is not None
    assert stats.requests == 3
    assert stats.connected_devices == 1


def test_stop_drops_live_connections(engine, app):
    port = share(engine, app.port)
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(f"GET /sse HTTP/1.1\r\nHost: {LAN_IP}:{port}\r\n\r\n".encode())
    received = b""
    while b"data: first" not in received:
        chunk = sock.recv(4096)
        assert chunk, "stream ended before the first event"
        received += chunk
    stopped = threading.Event()
    threading.Thread(
        target=lambda: (engine.stop_proxy(f"svc-{app.port}"), stopped.set()), daemon=True
    ).start()
    assert stopped.wait(5), "stop_proxy hung on a live streaming connection"
    sock.settimeout(2)
    remaining = b""
    try:
        while chunk := sock.recv(4096):
            remaining += chunk
    except OSError:
        pass
    assert b"data: second" not in remaining
    sock.close()

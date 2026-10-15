"""Regression guard: no Nagle/delayed-ACK stalls on keep-alive connections.

With Nagle's algorithm on, a response head and body written separately
wait for the client's delayed ACK, adding ~40 ms to every request. A dev
page loading hundreds of modules would crawl. Twenty requests must take
far less than twenty delayed ACKs.
"""

import http.client
import socket
import threading
import time

from services.file_server import FileServer
from services.proxy_engine import ProxyEngine
from tests.upstreams import free_port

REQUESTS = 20
BUDGET_SECONDS = 0.4
SINGLE_WRITE_RESPONSE = (
    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"
)


def single_write_upstream() -> tuple[socket.socket, int]:
    """Keep-alive server answering each request with one send()."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)

    def serve(connection: socket.socket) -> None:
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        pending = b""
        with connection:
            while data := connection.recv(65536):
                pending += data
                while b"\r\n\r\n" in pending:
                    _, pending = pending.split(b"\r\n\r\n", 1)
                    connection.sendall(SINGLE_WRITE_RESPONSE)

    def accept_loop() -> None:
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=serve, args=(connection,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    return server, server.getsockname()[1]


def timed_requests(port: int, path: str) -> float:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    started = time.perf_counter()
    for _ in range(REQUESTS):
        connection.request("GET", path, headers={"Host": f"192.168.1.5:{port}"})
        response = connection.getresponse()
        response.read()
        assert response.status == 200
    elapsed = time.perf_counter() - started
    connection.close()
    return elapsed


def test_proxy_adds_no_delayed_ack_stalls():
    upstream, upstream_port = single_write_upstream()
    engine = ProxyEngine()
    port = free_port()
    engine.start_proxy("latency", "127.0.0.1", port, upstream_port)
    try:
        assert timed_requests(port, "/") < BUDGET_SECONDS
    finally:
        engine.stop_all()
        upstream.close()


def test_file_server_adds_no_delayed_ack_stalls(tmp_path):
    (tmp_path / "small.js").write_text("export const ok = 1;", encoding="utf-8")
    server = FileServer()
    port = free_port()
    server.start_server("latency", tmp_path, port)
    try:
        assert timed_requests(port, "/small.js") < BUDGET_SECONDS
    finally:
        server.stop_all()

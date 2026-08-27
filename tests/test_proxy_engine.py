"""Tests for the TCP reverse proxy engine."""

import socket
import threading

import pytest

from services.proxy_engine import ProxyEngine, ProxyError


def echo_server(ready: threading.Event) -> tuple[socket.socket, int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(4)

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                while True:
                    data = conn.recv(1024)
                    if not data:
                        break
                    conn.sendall(data)

    threading.Thread(target=serve, daemon=True).start()
    ready.set()
    return server, server.getsockname()[1]


def test_proxy_forwards_traffic():
    ready = threading.Event()
    target, target_port = echo_server(ready)
    ready.wait(timeout=1)
    free = socket.socket()
    free.bind(("", 0))
    listen_port = free.getsockname()[1]
    free.close()

    engine = ProxyEngine()
    engine.start_proxy("svc", "127.0.0.1", listen_port, target_port)
    assert engine.is_running("svc") is True
    try:
        with socket.create_connection(("127.0.0.1", listen_port), timeout=3) as client:
            client.sendall(b"ping")
            assert client.recv(1024) == b"ping"
        assert engine.connected_devices("svc") >= 1
    finally:
        engine.stop_all()
        target.close()


def test_stop_proxy_closes_listener():
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    listen_port = free.getsockname()[1]
    free.close()

    engine = ProxyEngine()
    engine.start_proxy("svc", "127.0.0.1", listen_port, 1)
    engine.stop_proxy("svc")
    assert engine.is_running("svc") is False
    probe = socket.socket()
    assert probe.connect_ex(("127.0.0.1", listen_port)) != 0
    probe.close()


def test_bind_conflict_raises():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    engine = ProxyEngine()
    with pytest.raises(ProxyError):
        engine.start_proxy("svc", "127.0.0.1", port, 9)
    blocker.close()

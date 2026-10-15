"""Tests for the one-instance-per-session guard."""

import socket
import threading

from tests.upstreams import free_port
from utils.single_instance import ACTIVATE_TOKEN, SingleInstance


def test_second_instance_activates_the_first():
    port = free_port()
    activated = threading.Event()
    first = SingleInstance(activated.set, port=port)
    second = SingleInstance(lambda: None, port=port)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
        assert activated.wait(2), "first instance was not asked to show itself"
    finally:
        first.release()


def test_unrelated_connections_do_not_activate():
    port = free_port()
    activated = threading.Event()
    guard = SingleInstance(activated.set, port=port)
    try:
        assert guard.acquire() is True
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(b"GET / HTTP/1.1\r\n\r\n")
        assert not activated.wait(0.5)
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(ACTIVATE_TOKEN)
        assert activated.wait(2)
    finally:
        guard.release()


def test_port_held_by_another_program_still_runs():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    port = blocker.getsockname()[1]
    try:
        assert SingleInstance(lambda: None, port=port).acquire() is True
    finally:
        blocker.close()

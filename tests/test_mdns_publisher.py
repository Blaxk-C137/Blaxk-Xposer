"""Tests for the avahi-publish wrapper and hostname normalization."""

import subprocess

import pytest

from network.mdns_publisher import (
    MdnsPublisher,
    normalize_hostname,
    to_local_name,
)


class FakeProcess:
    """Stands in for a Popen handle; records termination."""

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


def make_publisher(process=None, error=None):
    """Build an MdnsPublisher around a recording spawn function."""
    calls = []

    def spawn(cmd, **_kwargs):
        calls.append(cmd)
        if error is not None:
            raise error
        return process if process is not None else FakeProcess()

    return MdnsPublisher(spawn=spawn), calls


def test_publish_spawns_avahi_publish():
    process = FakeProcess()
    publisher, calls = make_publisher(process=process)
    assert publisher.publish("myapp.local", "192.168.1.5") is True
    assert calls == [
        ["avahi-publish", "-a", "-R", "myapp.local", "192.168.1.5"]
    ]


def test_publish_returns_false_when_avahi_missing():
    publisher, calls = make_publisher(error=FileNotFoundError("avahi-publish"))
    assert publisher.publish("myapp.local", "192.168.1.5") is False
    assert len(calls) == 1, "spawn should have been attempted exactly once"


def test_unpublish_terminates_process():
    process = FakeProcess()
    publisher, _ = make_publisher(process=process)
    publisher.publish("myapp.local", "192.168.1.5")
    publisher.unpublish("myapp.local")
    assert process.terminated


def test_unpublish_unknown_hostname_is_noop():
    publisher, _ = make_publisher()
    publisher.unpublish("never-published.local")  # must not raise


def test_unpublish_after_spawn_failure_is_noop():
    publisher, _ = make_publisher(error=FileNotFoundError())
    publisher.publish("myapp.local", "192.168.1.5")
    publisher.unpublish("myapp.local")  # must not raise


def test_publish_replaces_previous_process_for_same_hostname():
    publisher, _ = make_publisher()
    publisher.publish("myapp.local", "192.168.1.5")
    first = publisher._processes["myapp.local"]
    publisher.publish("myapp.local", "192.168.1.6")
    assert first.terminated, "old process left running"
    assert publisher._processes["myapp.local"] is not first


# ---- hostname normalization ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("myapp", "myapp"),
        ("My App", "my-app"),
        ("  spaced  ", "spaced"),
        ("my_project", "my-project"),
        ("UPPER", "upper"),
        ("myapp.local", "myapp.local"),
        ("My Project.local", "my-project.local"),
    ],
)
def test_normalize_hostname(raw, expected):
    assert normalize_hostname(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "!!!", "a b!!c"])
def test_normalize_hostname_rejects_garbage(raw):
    with pytest.raises(ValueError):
        normalize_hostname(raw)


def test_to_local_name_appends_suffix_once():
    assert to_local_name("myapp") == "myapp.local"
    assert to_local_name("myapp.local") == "myapp.local"

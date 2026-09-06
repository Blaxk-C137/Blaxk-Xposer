"""Tests for the avahi-publish wrapper and hostname normalization."""

import subprocess

import pytest

from network.mdns_publisher import (
    MdnsPublisher,
    normalize_hostname,
    to_local_name,
)


class FakeProcess:
    """Stands in for a Popen handle; records termination.

    exit_code simulates avahi-daemon rejecting the record: the process
    is already dead when polled.
    """

    def __init__(self, exit_code: int | None = None) -> None:
        self.terminated = False
        self.killed = False
        self.exit_code = exit_code
        self.returncode = exit_code

    def poll(self) -> int | None:
        return self.exit_code

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

    return MdnsPublisher(spawn=spawn, settle_seconds=0.0), calls


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


def test_publish_returns_false_when_avahi_rejects_name():
    # A process that is already dead stands for a rejected record:
    # avahi-publish exits ~0.05s after spawn when the daemon says no.
    publisher, _ = make_publisher(process=FakeProcess(exit_code=1))
    assert publisher.publish("-bad.local", "192.168.1.5") is False


def test_publish_returns_true_while_process_stays_alive():
    publisher, _ = make_publisher(process=FakeProcess(exit_code=None))
    assert publisher.publish("myapp.local", "192.168.1.5") is True


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


@pytest.mark.parametrize("raw", ["", "   ", "!!!", "a b!!c", "-lead", "trail-", "a" * 64])
def test_normalize_hostname_rejects_garbage(raw):
    with pytest.raises(ValueError):
        normalize_hostname(raw)


def test_normalize_hostname_accepts_single_char_and_63_chars():
    assert normalize_hostname("a") == "a"
    assert normalize_hostname("a" * 63) == "a" * 63


def test_to_local_name_appends_suffix_once():
    assert to_local_name("myapp") == "myapp.local"
    assert to_local_name("myapp.local") == "myapp.local"

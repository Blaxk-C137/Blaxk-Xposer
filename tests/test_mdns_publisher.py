"""Tests for mDNS publishing backends, reference counting and names."""

import sys
import types

import pytest

from network.mdns_publisher import (
    AvahiBackend,
    MdnsPublisher,
    ZeroconfBackend,
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


def avahi_processes(publisher: MdnsPublisher) -> dict:
    """The avahi backend's live process table."""
    backend = publisher._backends[0]
    assert isinstance(backend, AvahiBackend)
    return backend._processes


def test_publish_replaces_previous_process_for_same_hostname():
    publisher, _ = make_publisher()
    publisher.publish("myapp.local", "192.168.1.5")
    first = avahi_processes(publisher)["myapp.local"]
    publisher.publish("myapp.local", "192.168.1.6")
    assert first.terminated, "old process left running"
    assert avahi_processes(publisher)["myapp.local"] is not first


# ---- reference counting and fallback ----


class RecordingBackend:
    """Backend double that can be told to refuse."""

    def __init__(self, name: str, accept: bool = True) -> None:
        self.name = name
        self.accept = accept
        self.published: dict[str, str] = {}
        self.closed = False

    def publish(self, hostname: str, ip: str, port: int) -> bool:
        if self.accept:
            self.published[hostname] = ip
        return self.accept

    def unpublish(self, hostname: str) -> None:
        self.published.pop(hostname, None)

    def close(self) -> None:
        self.closed = True


def test_shared_hostname_survives_until_last_user_stops():
    backend = RecordingBackend("fake")
    publisher = MdnsPublisher(backends=[backend])
    assert publisher.publish("shop.local", "192.168.1.5", 5173)
    assert publisher.publish("shop.local", "192.168.1.5", 8000)
    publisher.unpublish("shop.local")
    assert backend.published == {"shop.local": "192.168.1.5"}
    publisher.unpublish("shop.local")
    assert backend.published == {}


def test_falls_back_to_next_backend():
    broken = RecordingBackend("avahi", accept=False)
    working = RecordingBackend("zeroconf")
    publisher = MdnsPublisher(backends=[broken, working])
    assert publisher.publish("shop.local", "192.168.1.5")
    assert publisher.active_backend("shop.local") == "zeroconf"
    assert publisher.backend_names() == ["avahi", "zeroconf"]


def test_no_backends_means_no_custom_links():
    publisher = MdnsPublisher(backends=[])
    assert publisher.available is False
    assert publisher.publish("shop.local", "192.168.1.5") is False


def test_update_ip_republishes_every_name():
    backend = RecordingBackend("fake")
    publisher = MdnsPublisher(backends=[backend])
    publisher.publish("a.local", "192.168.1.5")
    publisher.publish("b.local", "192.168.1.5")
    assert publisher.update_ip("192.168.1.9") == []
    assert backend.published == {"a.local": "192.168.1.9", "b.local": "192.168.1.9"}
    publisher.unpublish("a.local")
    assert "a.local" not in backend.published


def test_stop_all_withdraws_and_closes():
    backend = RecordingBackend("fake")
    publisher = MdnsPublisher(backends=[backend])
    publisher.publish("a.local", "192.168.1.5")
    publisher.publish("a.local", "192.168.1.5")
    publisher.stop_all()
    assert backend.published == {}
    assert backend.closed


def test_zeroconf_backend_registers_hostname_as_server(monkeypatch):
    registered: list[object] = []
    unregistered: list[object] = []

    class FakeServiceInfo:
        def __init__(self, type_: str, name: str, **kwargs: object) -> None:
            self.type = type_
            self.name = name
            self.kwargs = kwargs

    class FakeZeroconf:
        def __init__(self, ip_version: object) -> None:
            self.closed = False

        def register_service(self, info: object, allow_name_change: bool) -> None:
            registered.append(info)

        def unregister_service(self, info: object) -> None:
            unregistered.append(info)

        def close(self) -> None:
            self.closed = True

    fake = types.ModuleType("zeroconf")
    fake.Zeroconf = FakeZeroconf
    fake.ServiceInfo = FakeServiceInfo
    fake.IPVersion = types.SimpleNamespace(V4Only="v4")
    fake.Error = type("Error", (Exception,), {})
    monkeypatch.setitem(sys.modules, "zeroconf", fake)
    backend = ZeroconfBackend()
    assert backend.publish("shop.local", "192.168.1.5", 5173) is True
    info = registered[0]
    assert info.name == "shop._http._tcp.local."
    assert info.kwargs["server"] == "shop.local."
    assert info.kwargs["port"] == 5173
    assert info.kwargs["addresses"] == [bytes([192, 168, 1, 5])]
    backend.close()
    assert unregistered == [info]


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

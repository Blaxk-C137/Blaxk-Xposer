"""Tests for tray host availability detection."""

import pytest

gi = pytest.importorskip("gi")

gi.require_version("Gio", "2.0")
from gi.repository import GLib  # noqa: E402

from tray.tray_host import WATCHER_NAME, tray_host_available  # noqa: E402


class FakeConnection:
    """Stands in for Gio.DBusConnection.call_sync results."""

    def __init__(self, result: GLib.Variant | Exception) -> None:
        self._result = result
        self.last_name: str | None = None

    def call_sync(
        self,
        bus_name: str,
        object_path: str,
        interface_name: str,
        method_name: str,
        parameters: GLib.Variant,
        reply_type: object,
        flags: object,
        timeout_msec: int,
        cancellable: object,
    ) -> GLib.Variant:
        self.last_name = parameters[0]
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_watcher_name_is_the_sni_standard():
    assert WATCHER_NAME == "org.kde.StatusNotifierWatcher"


def test_host_present_when_watcher_has_owner():
    connection = FakeConnection(GLib.Variant("(b)", (True,)))
    assert tray_host_available(connection) is True
    assert connection.last_name == WATCHER_NAME


def test_no_host_when_watcher_absent():
    connection = FakeConnection(GLib.Variant("(b)", (False,)))
    assert tray_host_available(connection) is False


def test_bus_errors_mean_no_host():
    connection = FakeConnection(GLib.Error("no bus"))
    assert tray_host_available(connection) is False


def test_real_session_bus_call_does_not_raise():
    # Whatever the desktop answers, the helper must return a bool.
    try:
        result = tray_host_available()
    except GLib.Error as exc:
        raise AssertionError(f"helper raised on real bus: {exc}") from exc
    assert isinstance(result, bool)

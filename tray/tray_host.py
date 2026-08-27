"""Detect whether the desktop session can actually host a tray icon.

An AppIndicator only becomes visible when a StatusNotifierHost is
listening - on GNOME Wayland without the AppIndicator extension there is
none, and an indicator constructed anyway silently never appears.
"""

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from utils.logger import get_logger

WATCHER_NAME = "org.kde.StatusNotifierWatcher"
DBUS_SERVICE_NAME = "org.freedesktop.DBus"
DBUS_OBJECT_PATH = "/org/freedesktop/DBus"
DBUS_INTERFACE_NAME = "org.freedesktop.DBus"

logger = get_logger("tray")


def tray_host_available(connection: Gio.DBusConnection | None = None) -> bool:
    """Return True when something owns the StatusNotifierWatcher name.

    Any bus error counts as "no host": a session where we cannot even ask
    is a session where no icon will be shown.
    """
    try:
        if connection is None:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = connection.call_sync(
            DBUS_SERVICE_NAME,
            DBUS_OBJECT_PATH,
            DBUS_INTERFACE_NAME,
            "NameHasOwner",
            GLib.Variant("(s)", (WATCHER_NAME,)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
    except GLib.Error as exc:
        logger.warning(f"Could not query {WATCHER_NAME}: {exc}")
        return False
    # unpack() of a "(b)" variant yields a 1-tuple; index it before testing.
    return bool(result.unpack()[0])

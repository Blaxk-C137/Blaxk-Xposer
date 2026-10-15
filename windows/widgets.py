"""Small reusable widgets and text helpers shared by Xposer windows."""

from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

from models.service import LocalProtocol, Service, ServiceStatus, ShareScheme
from network.firewall import FirewallHelp
from utils.logger import get_logger
from windows.theme import (
    EMPTY_ICON_SIZE,
    SPACING_M,
    SPACING_S,
    SPACING_XS,
    add_classes,
    padded,
)

logger = get_logger("windows")

COPY_FEEDBACK_MILLISECONDS = 1200
# A wrapped label's minimum width decides how tall GTK sizes its window;
# a sensible minimum avoids windows that open with empty space below.
WRAP_MIN_CHARS = 28
WRAP_MAX_CHARS = 48
PIXBUF_CACHE_LIMIT = 32
STATUS_TEXT = {
    ServiceStatus.RUNNING: ("Live", "xposer-pill-running"),
    ServiceStatus.STOPPED: ("Stopped", "xposer-pill-stopped"),
    ServiceStatus.ERROR: ("Problem", "xposer-pill-error"),
}
SCHEME_TOOLTIPS = {
    ShareScheme.HTTP: "Devices open this with http://",
    ShareScheme.HTTPS: "Devices open this with https:// (certificate warning expected)",
    ShareScheme.TCP: "Raw TCP relay for a non-web service",
}

_pixbuf_cache: dict[tuple[str, int, float], GdkPixbuf.Pixbuf] = {}


def copy_to_clipboard(text: str) -> None:
    """Put text on the system clipboard."""
    display = Gdk.Display.get_default()
    if display is None:
        return
    clipboard = Gtk.Clipboard.get_default(display)
    clipboard.set_text(text, -1)
    clipboard.store()


def open_link(parent: Gtk.Window | None, url: str) -> None:
    """Open url in the default browser; failures are logged, not raised."""
    target = url if "://" in url else f"http://{url}"
    try:
        Gtk.show_uri_on_window(parent, target, Gdk.CURRENT_TIME)
    except GLib.Error as exc:
        logger.warning(f"Could not open {target}: {exc}")


def load_pixbuf(path: str | None, size: int) -> GdkPixbuf.Pixbuf | None:
    """Load an image scaled to size, cached by path, size and mtime."""
    if not path:
        return None
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return None
    key = (path, size, mtime)
    cached = _pixbuf_cache.get(key)
    if cached is not None:
        return cached
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(path, size, size)
    except GLib.Error as exc:
        logger.debug(f"Image unavailable: {exc}")
        return None
    if len(_pixbuf_cache) >= PIXBUF_CACHE_LIMIT:
        _pixbuf_cache.clear()
    _pixbuf_cache[key] = pixbuf
    return pixbuf


def icon_button(icon_name: str, tooltip: str, handler: Callable[[Gtk.Button], None]) -> Gtk.Button:
    """Flat button showing a symbolic icon, with an accessible name."""
    button = Gtk.Button.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
    button.set_tooltip_text(tooltip)
    button.set_relief(Gtk.ReliefStyle.NONE)
    button.set_valign(Gtk.Align.CENTER)
    button.get_accessible().set_name(tooltip)
    button.connect("clicked", handler)
    return button


def label(
    text: str = "",
    *classes: str,
    xalign: float = 0.0,
    wrap: bool = False,
    ellipsize: Pango.EllipsizeMode = Pango.EllipsizeMode.NONE,
) -> Gtk.Label:
    """Left-aligned label with optional CSS classes, wrapping or ellipsizing."""
    widget = Gtk.Label(label=text, xalign=xalign)
    if wrap:
        widget.set_line_wrap(True)
        widget.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        widget.set_width_chars(WRAP_MIN_CHARS)
        widget.set_max_width_chars(WRAP_MAX_CHARS)
    widget.set_ellipsize(ellipsize)
    add_classes(widget, *classes)
    return widget


def url_label() -> Gtk.Label:
    """Selectable monospace label for links."""
    widget = label("", "xposer-url", ellipsize=Pango.EllipsizeMode.MIDDLE)
    widget.set_selectable(True)
    widget.set_can_focus(False)
    widget.set_hexpand(True)
    return widget


class StatusPill(Gtk.Label):
    """Rounded 'Live / Stopped / Problem' indicator."""

    def __init__(self) -> None:
        super().__init__()
        add_classes(self, "xposer-pill")
        self.set_valign(Gtk.Align.CENTER)
        self._css_class: str | None = None

    def set_status(self, status: ServiceStatus) -> None:
        """Show status as text and colour."""
        text, css_class = STATUS_TEXT[status]
        self.set_text(text)
        context = self.get_style_context()
        if self._css_class and self._css_class != css_class:
            context.remove_class(self._css_class)
        context.add_class(css_class)
        self._css_class = css_class


class SchemeBadge(Gtk.Label):
    """Small 'HTTP' / 'HTTPS' / 'TCP' tag."""

    def __init__(self) -> None:
        super().__init__()
        add_classes(self, "xposer-badge")
        self.set_valign(Gtk.Align.CENTER)

    def set_service(self, service: Service) -> None:
        """Describe how devices open the service."""
        self.set_text(service.share_scheme.value.upper())
        tooltip = SCHEME_TOOLTIPS[service.share_scheme]
        if (
            service.local_protocol == LocalProtocol.HTTPS
            and service.share_scheme == ShareScheme.HTTP
        ):
            tooltip = "HTTPS app opened with plain http:// — no certificate warning"
        self.set_tooltip_text(tooltip)


class CopyButton(Gtk.Button):
    """Copy-to-clipboard button that briefly confirms with a check mark."""

    def __init__(self, tooltip: str = "Copy link") -> None:
        super().__init__()
        self._text = ""
        self._tooltip = tooltip
        self._image = Gtk.Image.new_from_icon_name("edit-copy-symbolic", Gtk.IconSize.BUTTON)
        self.set_image(self._image)
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.set_valign(Gtk.Align.CENTER)
        self.set_tooltip_text(tooltip)
        self.get_accessible().set_name(tooltip)
        self._restore_id: int | None = None
        self.connect("clicked", self._on_clicked)
        self.connect("destroy", self._on_destroy)

    def set_text(self, text: str) -> None:
        """Text to copy on click."""
        self._text = text
        self.set_sensitive(bool(text))

    def _on_clicked(self, _button: Gtk.Button) -> None:
        """Copy and show feedback."""
        copy_to_clipboard(self._text)
        self._image.set_from_icon_name("object-select-symbolic", Gtk.IconSize.BUTTON)
        self.set_tooltip_text("Copied")
        if self._restore_id is not None:
            GLib.source_remove(self._restore_id)
        self._restore_id = GLib.timeout_add(COPY_FEEDBACK_MILLISECONDS, self._restore)

    def _restore(self) -> bool:
        """Return to the copy icon."""
        self._restore_id = None
        self._image.set_from_icon_name("edit-copy-symbolic", Gtk.IconSize.BUTTON)
        self.set_tooltip_text(self._tooltip)
        return GLib.SOURCE_REMOVE

    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        """Cancel a pending restore."""
        if self._restore_id is not None:
            GLib.source_remove(self._restore_id)
            self._restore_id = None


class EmptyState(Gtk.Box):
    """Centered icon, title and explanation for empty views."""

    def __init__(self, icon_name: str, title: str, subtitle: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=SPACING_S)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.DIALOG)
        image.set_pixel_size(EMPTY_ICON_SIZE)
        add_classes(image, "xposer-dim")
        self.pack_start(image, False, False, SPACING_S)
        self.title_label = label(title, "xposer-heading", xalign=0.5)
        self.pack_start(self.title_label, False, False, 0)
        subtitle_label = label(subtitle, "xposer-dim", xalign=0.5, wrap=True)
        subtitle_label.set_justify(Gtk.Justification.CENTER)
        self.pack_start(subtitle_label, False, False, 0)
        self.actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_S)
        self.actions.set_halign(Gtk.Align.CENTER)
        self.pack_start(self.actions, False, False, SPACING_S)


def firewall_help_popover(
    relative_to: Gtk.Widget, help_text: FirewallHelp, on_auto_allow: Callable[[], None] | None
) -> Gtk.Popover:
    """Popover explaining how to let other devices connect."""
    popover = Gtk.Popover.new(relative_to)
    box = padded(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACING_S), SPACING_M)
    box.pack_start(label("Can't open it on your phone?", "xposer-title"), False, False, 0)
    box.pack_start(label(help_text.summary, wrap=True), False, False, 0)
    for step in help_text.steps:
        step_label = label(f"•  {step}", "xposer-small", wrap=True)
        step_label.set_selectable(True)
        box.pack_start(step_label, False, False, 0)
    if help_text.can_auto_allow and on_auto_allow is not None:
        allow = Gtk.Button(label="Allow through firewall…")
        add_classes(allow, "suggested-action")
        allow.connect("clicked", lambda _b: (popover.popdown(), on_auto_allow()))
        box.pack_start(allow, False, False, SPACING_XS)
    box.show_all()
    popover.add(box)
    return popover


def traffic_text(service: Service) -> str:
    """'2 devices · 41 requests' style summary."""
    devices = service.connected_devices
    parts = [f"{devices} device{'s' if devices != 1 else ''}"]
    if service.requests_served:
        parts.append(
            f"{service.requests_served} request{'s' if service.requests_served != 1 else ''}"
        )
    return " · ".join(parts)


def source_text(service: Service) -> str:
    """What is being shared: 'localhost:5173 (HTTPS)' or the folder name."""
    if service.folder_path:
        return Path(service.folder_path).name or service.folder_path
    protocol = service.local_protocol
    suffix = f" ({protocol.value.upper()})" if protocol != LocalProtocol.HTTP else ""
    return f"localhost:{service.port}{suffix}"


def error_message(exc: Exception) -> str:
    """User-facing text for an exception from a service operation."""
    message = getattr(exc, "user_message", None)
    return str(message) if message else str(exc)

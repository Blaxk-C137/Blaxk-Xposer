"""Compact status panel opened from the tray menu.

Shows the LAN IP, one row per service (status dot, name, URL, toggle,
QR, copy), and quick actions for exposing ports or serving folders.
Threading mirrors TrayManager: engine calls run off-thread via
ui_tasks; UI work stays on the GTK main loop.
"""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from models.service import Service, ServiceStatus
from network.network_detector import NetworkDetector
from services.service_manager import ServiceError, ServiceManager
from utils.logger import get_logger
from utils.ui_tasks import run_off_thread

PANEL_WIDTH_PX = 380
APP_HEADING = "Xposer"
EMPTY_TEXT = "Nothing shared yet.\nExpose a port or serve a folder to start."
EXPOSE_LABEL = "+ Expose Port"
SERVE_LABEL = "+ Serve Folder"
SETTINGS_LABEL = "⚙"
QUIT_LABEL = "Quit"
TOGGLE_STOP_LABEL = "⏸"
TOGGLE_START_LABEL = "▶"
COPY_LABEL = "✂"
COPIED_LABEL = "Copied!"
QR_LABEL = "QR"
STATUS_DOTS = {
    ServiceStatus.RUNNING: ("●", "#2ec27e"),
    ServiceStatus.STOPPED: ("●", "#9a9996"),
    ServiceStatus.ERROR: ("●", "#e01b24"),
}

logger = get_logger("windows")


class StatusPanel(Gtk.Window):
    """Popup card listing every service with quick controls."""

    def __init__(
        self,
        service_manager: ServiceManager,
        detector: NetworkDetector,
        window_manager: object,
        app_version: str,
    ) -> None:
        super().__init__(title="Xposer Status", decorated=False)
        self._service_manager = service_manager
        self._detector = detector
        self._window_manager = window_manager
        self._app_version = app_version
        self.set_default_size(PANEL_WIDTH_PX, -1)
        self.set_skip_taskbar_hint(True)
        self.connect("key-press-event", self._on_key_pressed)
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        self.add(outer)

        # Header: app name + version on one line, LAN IP beneath.
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        outer.pack_start(header, False, False, 0)
        heading = Gtk.Label()
        heading.set_markup(
            f"<b>{APP_HEADING}</b> {self._app_version}"
        )
        heading.set_halign(Gtk.Align.START)
        header.pack_start(heading, False, False, 0)
        self._ip_label = Gtk.Label()
        self._ip_label.get_style_context().add_class("xposer-mono")
        self._ip_label.set_halign(Gtk.Align.START)
        header.pack_start(self._ip_label, False, False, 0)

        outer.pack_start(Gtk.Separator(), False, False, 0)

        # Service rows, or the empty-state label.
        self._empty_label = Gtk.Label(label=EMPTY_TEXT)
        self._empty_label.set_justify(Gtk.Justification.CENTER)
        outer.pack_start(self._empty_label, False, False, 0)
        self._rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.pack_start(self._rows_box, False, False, 0)

        outer.pack_start(Gtk.Separator(), False, False, 0)

        # Footer: primary actions plus settings and quit.
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.pack_start(footer, False, False, 0)
        expose = Gtk.Button(label=EXPOSE_LABEL)
        expose.connect("clicked", self._on_expose_clicked)
        footer.pack_start(expose, False, False, 0)
        serve = Gtk.Button(label=SERVE_LABEL)
        serve.connect("clicked", self._on_serve_clicked)
        footer.pack_start(serve, False, False, 0)
        settings = Gtk.Button(label=SETTINGS_LABEL)
        settings.set_tooltip_text("Settings")
        settings.connect("clicked", self._on_settings_clicked)
        footer.pack_end(settings, False, False, 0)
        quit_button = Gtk.Button(label=QUIT_LABEL)
        quit_button.connect("clicked", self._on_quit_clicked)
        footer.pack_end(quit_button, False, False, 0)

        self.show_all()

    # ---- refresh ----

    def refresh(self) -> None:
        """Rebuild the service rows from current manager state."""
        for child in self._rows_box.get_children():
            self._rows_box.remove(child)
        self._ip_label.set_text(self._detector.get_lan_ip())
        services = self._service_manager.all_services()
        for service in services:
            self._rows_box.pack_start(ServiceRow(self, service), False, False, 0)
        self._rows_box.show_all()
        self._empty_label.set_visible(not services)

    # ---- handlers ----

    def _on_key_pressed(
        self, _window: Gtk.Window, event: Gdk.EventKey
    ) -> bool:
        """Close the panel on Escape."""
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def _on_expose_clicked(self, _button: Gtk.Button) -> None:
        """Open the Expose Port window."""
        if self._window_manager is not None:
            self._window_manager.show_expose_port()

    def _on_serve_clicked(self, _button: Gtk.Button) -> None:
        """Open the Serve Folder window."""
        if self._window_manager is not None:
            self._window_manager.show_serve_folder()

    def _on_settings_clicked(self, _button: Gtk.Button) -> None:
        """Open the Settings window."""
        if self._window_manager is not None:
            self._window_manager.show_settings()

    def _on_quit_clicked(self, _button: Gtk.Button) -> None:
        """Leave the GTK main loop; main.py's exit path shuts engines down."""
        Gtk.main_quit()

    # ---- service actions (shared by every row) ----

    def toggle_service(self, service: Service, turn_on: bool) -> None:
        """Start or stop a service off-thread, then refresh on failure.

        Successful changes notify observers and refresh the panel through
        WindowManager; failure logs, rebuilds rows so the toggle never
        shows state the manager does not have.
        """
        name = service.name

        def work() -> None:
            if turn_on:
                self._service_manager.restart_service(service.id)
            else:
                self._service_manager.stop_service(service.id)

        def failure(exc: Exception) -> None:
            message = exc.user_message if isinstance(exc, ServiceError) else str(exc)
            logger.warning(f"Panel toggle failed for '{name}': {message}")
            GLib.idle_add(self.refresh)

        run_off_thread(f"xposer-panel-{service.id[:8]}", work, None, failure)

    def copy_url(self, button: Gtk.Button, url: str) -> None:
        """Put a LAN URL on the clipboard and flash the button."""
        clipboard = Gtk.Clipboard.get_default(Gdk.Display.get_default())
        clipboard.set_text(url, -1)
        button.set_label(COPIED_LABEL)

    def show_qr(self, service: Service) -> None:
        """Open the QR window for one service."""
        if self._window_manager is not None:
            self._window_manager.open_qr_window(service)


class ServiceRow(Gtk.Box):
    """One service's card: dot, name, URL, and quick actions."""

    def __init__(self, panel: StatusPanel, service: Service) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._panel = panel
        self._service = service
        self.get_style_context().add_class("xposer-card")
        self.set_margin_start(4)
        self.set_margin_end(4)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.pack_start(top, False, False, 0)

        dot_text, dot_color = STATUS_DOTS.get(
            service.status, STATUS_DOTS[ServiceStatus.STOPPED]
        )
        self._dot_label = Gtk.Label()
        self._dot_label.set_markup(f'<span foreground="{dot_color}">{dot_text}</span>')
        top.pack_start(self._dot_label, False, False, 0)

        name = Gtk.Label(label=service.name)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(24)
        top.pack_start(name, False, False, 0)

        self._toggle_button = self._make_toggle_button()
        top.pack_end(self._toggle_button, False, False, 0)
        top.pack_end(self._make_qr_button(), False, False, 0)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.pack_start(bottom, False, False, 0)

        self._url_label = Gtk.Label(label=service.lan_url)
        self._url_label.get_style_context().add_class("xposer-mono")
        self._url_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._url_label.set_max_width_chars(28)
        bottom.pack_start(self._url_label, True, True, 0)

        copy = Gtk.Button(label=COPY_LABEL)
        copy.set_tooltip_text(f"Copy {service.lan_url}")
        copy.connect("clicked", panel.copy_url, service.lan_url)
        bottom.pack_end(copy, False, False, 0)

    def _make_toggle_button(self) -> Gtk.Button:
        """Stop a running service, or restart a stopped one."""
        running = self._service.status == ServiceStatus.RUNNING
        button = Gtk.Button(
            label=TOGGLE_STOP_LABEL if running else TOGGLE_START_LABEL
        )
        button.set_tooltip_text("Stop" if running else "Start")
        button.connect(
            "clicked",
            lambda _b: self._panel.toggle_service(self._service, not running),
        )
        return button

    def _make_qr_button(self) -> Gtk.Button:
        """Open the QR window for the service."""
        button = Gtk.Button(label=QR_LABEL)
        button.set_tooltip_text("Show QR code")
        button.set_sensitive(
            self._service.status == ServiceStatus.RUNNING
            and self._panel._window_manager is not None
        )
        button.connect(
            "clicked", lambda _b: self._panel.show_qr(self._service)
        )
        return button

"""Routes all Xposer windows and owns the shared GTK CSS identity."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from config.config_store import ConfigStore
from models.service import Service
from network.network_detector import NetworkDetector
from network.qr_generator import QRGenerator
from services.service_manager import EVENT_SERVICES_CHANGED, ServiceManager
from windows.all_qr_codes_window import AllQRCodesWindow
from windows.expose_port_window import ExposePortWindow
from windows.qr_code_window import QRCodeWindow
from windows.serve_folder_window import ServeFolderWindow
from windows.settings_window import SettingsWindow
from windows.status_panel import StatusPanel

EXPOSE_PORT_ROLE = "expose_port"
SERVE_FOLDER_ROLE = "serve_folder"
ALL_QR_ROLE = "all_qr"
SETTINGS_ROLE = "settings"
STATUS_PANEL_ROLE = "status_panel"

CSS_DATA = b"""
.xposer-mono { font-family: monospace; }
.xposer-card {
    border-radius: 8px;
    border: 1px solid alpha(@theme_fg_color, 0.18);
    background-color: alpha(@theme_bg_color, 0.85);
}
"""


class WindowManager:
    """Creates and routes every Xposer window; owns shared CSS."""

    def __init__(
        self,
        service_manager: ServiceManager,
        detector: NetworkDetector,
        qr_generator: QRGenerator,
        config_store: ConfigStore,
        app_version: str,
    ) -> None:
        self._service_manager = service_manager
        self._detector = detector
        self._qr_generator = qr_generator
        self._config_store = config_store
        self._app_version = app_version
        self._windows: dict[str, Gtk.Window] = {}
        self._qr_windows: dict[str, QRCodeWindow] = {}
        self._load_css()

    @staticmethod
    def _load_css() -> None:
        """Register the application-wide CSS provider exactly once per run."""
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS_DATA)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    # ---- public API used by TrayManager and form windows ----

    def show_expose_port(self) -> Gtk.Window:
        """Open (or raise) the Expose Port window."""
        window = self._singleton(EXPOSE_PORT_ROLE)
        if window is None:
            window = ExposePortWindow(self._service_manager, self._detector, self)
            window.connect("destroy", self._forget_singleton, EXPOSE_PORT_ROLE)
            self._windows[EXPOSE_PORT_ROLE] = window
        window.present()
        return window

    def show_serve_folder(self) -> Gtk.Window:
        """Open (or raise) the Serve Folder window."""
        window = self._singleton(SERVE_FOLDER_ROLE)
        if window is None:
            window = ServeFolderWindow(self._service_manager, self)
            window.connect("destroy", self._forget_singleton, SERVE_FOLDER_ROLE)
            self._windows[SERVE_FOLDER_ROLE] = window
        window.present()
        return window

    def show_all_qr(self) -> Gtk.Window:
        """Open (or raise) the All QR Codes window."""
        window = self._singleton(ALL_QR_ROLE)
        if window is None:
            window = AllQRCodesWindow(self._service_manager, self._qr_generator)
            window.connect("destroy", self._forget_singleton, ALL_QR_ROLE)
            self._windows[ALL_QR_ROLE] = window
        window.refresh()
        window.present()
        return window

    def show_settings(self) -> Gtk.Window:
        """Open (or raise) the Settings window."""
        window = self._singleton(SETTINGS_ROLE)
        if window is None:
            window = SettingsWindow(
                self._service_manager,
                self._detector,
                self._config_store,
                self._app_version,
            )
            window.connect("destroy", self._forget_singleton, SETTINGS_ROLE)
            self._windows[SETTINGS_ROLE] = window
        window.present()
        return window

    def show_status_panel(self) -> Gtk.Window:
        """Open (or raise) the tray status panel."""
        window = self._singleton(STATUS_PANEL_ROLE)
        if window is None:
            window = StatusPanel(
                self._service_manager,
                self._detector,
                self,
                app_version=self._app_version,
            )
            window.connect("destroy", self._forget_singleton, STATUS_PANEL_ROLE)
            self._windows[STATUS_PANEL_ROLE] = window
        window.refresh()
        window.present()
        return window

    def open_qr_window(self, service: Service) -> Gtk.Window:
        """Open (or raise) the QR window for one service."""
        existing = self._qr_windows.get(service.id)
        if existing is not None:
            existing.present()
            return existing
        window = QRCodeWindow(
            self._service_manager, self._qr_generator, service, self
        )
        self._qr_windows[service.id] = window
        window.present()
        return window

    def forget_qr_window(self, service_id: str) -> None:
        """Untrack a closed QR window (called from its destroy handler)."""
        self._qr_windows.pop(service_id, None)

    # ---- observation ----

    def handle_event(self, event: str) -> None:
        """Thread-safe observer hook; UI work happens on the main loop."""
        GLib.idle_add(self._handle_event_main, event)

    def _handle_event_main(self, event: str) -> bool:
        """Refresh every open window that shows live service state."""
        if event != EVENT_SERVICES_CHANGED:
            return GLib.SOURCE_REMOVE
        panel = self._windows.get(STATUS_PANEL_ROLE)
        if panel is not None:
            panel.refresh()
        gallery = self._windows.get(ALL_QR_ROLE)
        if gallery is not None:
            gallery.refresh()
        for window in list(self._qr_windows.values()):
            window.refresh()
        return GLib.SOURCE_REMOVE

    # ---- internals ----

    def _singleton(self, role: str) -> Gtk.Window | None:
        """Return the tracked window for role; destroy handlers keep it fresh."""
        return self._windows.get(role)

    def _forget_singleton(self, window: Gtk.Window, role: str) -> None:
        """Untrack a destroyed singleton window."""
        self._windows.pop(role, None)

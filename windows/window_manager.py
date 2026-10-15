"""Routes all Xposer windows; installs the shared CSS once."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

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
from windows.theme import install_css

EXPOSE_PORT_ROLE = "expose_port"
SERVE_FOLDER_ROLE = "serve_folder"
ALL_QR_ROLE = "all_qr"
SETTINGS_ROLE = "settings"
STATUS_PANEL_ROLE = "status_panel"


class WindowManager:
    """Creates and routes every Xposer window."""

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
        self._dashboard_is_main_window = False
        self._refresh_pending = False
        install_css()

    def use_dashboard_as_main_window(self) -> None:
        """Without a tray icon, closing the dashboard quits the app."""
        self._dashboard_is_main_window = True

    # ---- public API used by TrayManager and form windows ----

    def show_expose_port(self) -> Gtk.Window:
        """Open (or raise) the Expose Port window."""
        window = self._singleton(EXPOSE_PORT_ROLE)
        if window is None:
            window = ExposePortWindow(self._service_manager, self._detector, self)
            self._track(window, EXPOSE_PORT_ROLE)
        window.present()
        return window

    def show_serve_folder(self) -> Gtk.Window:
        """Open (or raise) the Serve Folder window."""
        window = self._singleton(SERVE_FOLDER_ROLE)
        if window is None:
            window = ServeFolderWindow(self._service_manager, self)
            self._track(window, SERVE_FOLDER_ROLE)
        window.present()
        return window

    def show_all_qr(self) -> Gtk.Window:
        """Open (or raise) the All QR Codes window."""
        window = self._singleton(ALL_QR_ROLE)
        if window is None:
            window = AllQRCodesWindow(self._service_manager, self._qr_generator)
            self._track(window, ALL_QR_ROLE)
        window.refresh()
        window.present()
        return window

    def show_settings(self) -> Gtk.Window:
        """Open (or raise) the Settings window."""
        window = self._singleton(SETTINGS_ROLE)
        if window is None:
            window = SettingsWindow(
                self._service_manager, self._detector, self._config_store, self._app_version
            )
            self._track(window, SETTINGS_ROLE)
        window.present()
        return window

    def show_status_panel(self) -> Gtk.Window:
        """Open (or raise) the dashboard."""
        window = self._singleton(STATUS_PANEL_ROLE)
        if window is None:
            window = StatusPanel(
                self._service_manager,
                self._detector,
                self,
                app_version=self._app_version,
                quit_on_close=self._dashboard_is_main_window,
            )
            self._track(window, STATUS_PANEL_ROLE)
        window.refresh()
        window.present()
        return window

    def show_dashboard(self) -> Gtk.Window:
        """Alias used by the tray and the single-instance handler."""
        return self.show_status_panel()

    def open_qr_window(self, service: Service) -> Gtk.Window:
        """Open (or raise) the QR window for one service."""
        existing = self._qr_windows.get(service.id)
        if existing is not None:
            existing.present()
            return existing
        window = QRCodeWindow(self._service_manager, self._qr_generator, service, self)
        self._qr_windows[service.id] = window
        window.present()
        return window

    def forget_qr_window(self, service_id: str) -> None:
        """Untrack a closed QR window (called from its destroy handler)."""
        self._qr_windows.pop(service_id, None)

    # ---- observation ----

    def handle_event(self, event: str) -> None:
        """Thread-safe observer hook; coalesces bursts into one refresh."""
        if event != EVENT_SERVICES_CHANGED or self._refresh_pending:
            return
        self._refresh_pending = True
        GLib.idle_add(self._handle_event_main)

    def _handle_event_main(self) -> bool:
        """Refresh every open window that shows live service state."""
        self._refresh_pending = False
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

    def _track(self, window: Gtk.Window, role: str) -> None:
        """Remember a singleton window until it is destroyed."""
        window.connect("destroy", self._forget_singleton, role)
        self._windows[role] = window

    def _forget_singleton(self, window: Gtk.Window, role: str) -> None:
        """Untrack a destroyed singleton window."""
        if self._windows.get(role) is window:
            self._windows.pop(role, None)

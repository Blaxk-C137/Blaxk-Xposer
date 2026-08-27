"""System tray icon and right-click menu for Xposer.

Threading safety: ServiceManager observers fire on arbitrary threads, so
handle_event only schedules a menu rebuild onto the GTK main loop with
GLib.idle_add. Toggle handlers read widget state on the main thread and
then run their blocking engine calls off-thread via ui_tasks. All
indicator and menu work happens on the main loop; the Pillow badge render
is sub-millisecond and stays there by design.
"""

from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator
    except (ValueError, ImportError):
        # Left as None so importing this module never explodes; TrayManager
        # raises TrayError on construction where callers can handle it.
        AppIndicator = None

from PIL import Image, ImageDraw, ImageFont

from models.service import Service, ServiceStatus
from services.service_manager import (
    EVENT_SERVICES_CHANGED,
    ServiceError,
    ServiceManager,
)
from utils.logger import get_logger
from utils.ui_tasks import run_off_thread

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICON_IDLE_PATH = PROJECT_ROOT / "assets" / "icon_idle.png"
ICON_ACTIVE_PATH = PROJECT_ROOT / "assets" / "icon_active.png"
BADGE_CACHE_DIR = Path.home() / ".cache" / "xposer" / "icons"
BADGE_FONT_SIZE = 20

APP_LABEL = "Xposer"
STATUS_PANEL_LABEL = "Status Panel"
NO_SERVICES_LABEL = "No services yet"
EXPOSE_LABEL = "Expose Running Port"
SERVE_FOLDER_LABEL = "Serve Folder"
GALLERY_LABEL = "Show All QR Codes"
SETTINGS_LABEL = "Settings"
QUIT_LABEL = "Quit"
STATUS_SUFFIXES = {
    ServiceStatus.STOPPED: " (stopped)",
    ServiceStatus.ERROR: " (problem)",
}

logger = get_logger("tray")


class TrayError(RuntimeError):
    """Raised when no system tray indicator backend is available."""


class TrayManager:
    """Owns the tray indicator and its context menu."""

    def __init__(
        self,
        service_manager: ServiceManager,
        window_manager: object,
        app_version: str,
    ) -> None:
        if AppIndicator is None:
            raise TrayError(
                "Neither libayatana-appindicator nor libappindicator was found. "
                "Install your distribution's appindicator package."
            )
        self._service_manager = service_manager
        self._window_manager = window_manager
        self._app_version = app_version
        self._indicator = AppIndicator.Indicator.new(
            "xposer",
            str(ICON_IDLE_PATH),
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self._indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._rebuild()

    def handle_event(self, event: str) -> None:
        """Observer hook; safe to call from any thread."""
        if event == EVENT_SERVICES_CHANGED:
            GLib.idle_add(self._rebuild)

    # ---- menu ----

    def _rebuild(self) -> bool:
        """Rebuild menu contents and refresh the state icon."""
        services = self._service_manager.all_services()
        menu = Gtk.Menu.new()

        header = Gtk.MenuItem(label=f"{APP_LABEL} {self._app_version}")
        header.set_sensitive(False)
        menu.append(header)
        menu.append(self._menu_item(STATUS_PANEL_LABEL, self._on_status_panel_clicked))
        menu.append(Gtk.SeparatorMenuItem())

        if not services:
            empty = Gtk.MenuItem(label=NO_SERVICES_LABEL)
            empty.set_sensitive(False)
            menu.append(empty)
        for service in services:
            menu.append(self._make_service_item(service))

        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._menu_item(EXPOSE_LABEL, self._on_expose_clicked))
        menu.append(self._menu_item(SERVE_FOLDER_LABEL, self._on_serve_clicked))

        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._menu_item(GALLERY_LABEL, self._on_gallery_clicked))
        menu.append(self._menu_item(SETTINGS_LABEL, self._on_settings_clicked))

        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._menu_item(QUIT_LABEL, self._on_quit_clicked))

        menu.show_all()
        self._indicator.set_menu(menu)
        self._update_icon(services)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _menu_item(label: str, handler: object) -> Gtk.MenuItem:
        """Create a plain menu item wired to an activate handler."""
        item = Gtk.MenuItem(label=label)
        item.connect("activate", handler)
        return item

    def _make_service_item(self, service: Service) -> Gtk.CheckMenuItem:
        """Build the toggle row for one service.

        Checked means running; toggling stops or restarts it through
        ServiceManager. Only 'toggled' is connected — GTK fires 'activate'
        on the same click, which would double-trigger the action.
        """
        suffix = STATUS_SUFFIXES.get(service.status, "")
        item = Gtk.CheckMenuItem(label=f"{service.name}{suffix}")
        item.set_active(service.status == ServiceStatus.RUNNING)
        item.connect("toggled", self._on_service_toggled, service.id)
        item.set_tooltip_text(service.lan_url)
        return item

    # ---- handlers ----

    def _on_service_toggled(self, item: Gtk.CheckMenuItem, service_id: str) -> None:
        """Start or stop the service through ServiceManager.

        Engine joins, QR rendering, and config writes block, so the call
        runs off-thread. On failure a rebuild is scheduled so the checkbox
        never shows state the manager does not have. State changes notify
        observers on success; the menu rebuilds itself.
        """
        service = self._service_manager.get_service(service_id)
        if service is None:
            return
        name = service.name
        turn_on = item.get_active()

        def work() -> None:
            if turn_on:
                self._service_manager.restart_service(service_id)
            else:
                self._service_manager.stop_service(service_id)

        def success(_result: None) -> None:
            pass

        def failure(exc: Exception) -> None:
            message = exc.user_message if isinstance(exc, ServiceError) else str(exc)
            logger.warning(f"Toggle failed for '{name}': {message}")
            GLib.idle_add(self._rebuild)

        run_off_thread(
            f"xposer-toggle-{service_id[:8]}", work, success, failure
        )

    def _on_status_panel_clicked(self, _item: Gtk.MenuItem) -> None:
        """Open the status panel."""
        self._window_manager.show_status_panel()

    def _on_expose_clicked(self, _item: Gtk.MenuItem) -> None:
        """Open the Expose Port window."""
        self._window_manager.show_expose_port()

    def _on_serve_clicked(self, _item: Gtk.MenuItem) -> None:
        """Open the Serve Folder window."""
        self._window_manager.show_serve_folder()

    def _on_gallery_clicked(self, _item: Gtk.MenuItem) -> None:
        """Open the All QR Codes window."""
        self._window_manager.show_all_qr()

    def _on_settings_clicked(self, _item: Gtk.MenuItem) -> None:
        """Open the Settings window."""
        self._window_manager.show_settings()

    def _on_quit_clicked(self, _item: Gtk.MenuItem) -> None:
        """Leave the GTK main loop; main.py's exit path shuts engines down."""
        Gtk.main_quit()

    # ---- icons ----

    def _update_icon(self, services: list[Service]) -> None:
        """Point the indicator at the idle icon or a count-badged active icon."""
        running = [
            service
            for service in services
            if service.status == ServiceStatus.RUNNING
        ]
        icon = ICON_IDLE_PATH if not running else self._badge_icon_path(len(running))
        self._indicator.set_icon_full(str(icon), APP_LABEL)

    @staticmethod
    def _badge_icon_path(count: int) -> Path:
        """Render (once) and cache an active-state icon carrying the count."""
        BADGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = BADGE_CACHE_DIR / f"active_{count}.png"
        if path.exists():
            return path
        base = Image.open(ICON_ACTIVE_PATH).convert("RGBA")
        draw = ImageDraw.Draw(base)
        try:
            font = ImageFont.load_default(size=BADGE_FONT_SIZE)
        except TypeError:
            font = ImageFont.load_default()
        draw.text(
            (base.width // 2, base.height // 2),
            str(count),
            font=font,
            fill=(255, 255, 255, 255),
            anchor="ms",
            stroke_width=2,
            stroke_fill=(30, 30, 30, 255),
        )
        base.save(path)
        logger.debug(f"Rendered badge icon {path}")
        return path

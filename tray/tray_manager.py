"""System tray icon and menu for Xposer.

Two backends:
* AppIndicator (StatusNotifierItem) — Linux desktops with a tray host.
* Gtk.StatusIcon — Windows, and X11 desktops without a StatusNotifier
  host. It is deprecated in GTK 3 but remains the only tray API GTK 3
  offers on Windows.

Threading safety: ServiceManager observers fire on arbitrary threads, so
handle_event only schedules work onto the GTK main loop with
GLib.idle_add. The menu is rebuilt only when its visible content changes
(names and statuses), not on every traffic-counter update.
"""

import warnings
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator
    except (ValueError, ImportError):
        AppIndicator = None

from PIL import Image, ImageDraw, ImageFont

from models.service import Service, ServiceStatus
from services.service_manager import EVENT_SERVICES_CHANGED, ServiceManager
from tray.tray_host import tray_host_available
from utils.logger import get_logger
from utils.platform_env import IS_WINDOWS, cache_dir
from utils.ui_tasks import run_off_thread
from windows.widgets import error_message

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICON_IDLE_PATH = PROJECT_ROOT / "assets" / "icon_idle.png"
ICON_ACTIVE_PATH = PROJECT_ROOT / "assets" / "icon_active.png"
BADGE_CACHE_DIR = cache_dir() / "icons"
BADGE_FONT_SIZE = 26
BADGE_FONT_SIZE_WIDE = 18
BADGE_RADIUS_RATIO = 0.25
BADGE_FILL = (192, 28, 40, 255)
BADGE_TEXT = (255, 255, 255, 255)
MAX_BADGE_COUNT = 9

APP_LABEL = "Xposer"
OPEN_LABEL = "Open Xposer"
NO_SERVICES_LABEL = "Nothing shared yet"
EXPOSE_LABEL = "Share a Running App…"
SERVE_FOLDER_LABEL = "Share a Folder…"
GALLERY_LABEL = "All QR Codes"
SETTINGS_LABEL = "Settings"
QUIT_LABEL = "Quit"
STATUS_SUFFIXES = {
    ServiceStatus.STOPPED: " (stopped)",
    ServiceStatus.ERROR: " (problem)",
}

logger = get_logger("tray")


class TrayError(RuntimeError):
    """Raised when no system tray backend is available."""


class _AppIndicatorBackend:
    """StatusNotifierItem tray icon (Linux)."""

    name = "appindicator"

    def __init__(self, on_activate: Callable[[], None]) -> None:
        self.indicator = AppIndicator.Indicator.new(
            "xposer", str(ICON_IDLE_PATH), AppIndicator.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title(APP_LABEL)
        self._on_activate = on_activate

    def set_menu(self, menu: Gtk.Menu, open_item: Gtk.MenuItem) -> None:
        """Install the menu; middle-click opens the dashboard."""
        self.indicator.set_menu(menu)
        self.indicator.set_secondary_activate_target(open_item)

    def set_icon(self, path: Path, tooltip: str) -> None:
        """Change the icon."""
        self.indicator.set_icon_full(str(path), tooltip)


class _StatusIconBackend:
    """Gtk.StatusIcon tray icon (Windows and X11)."""

    name = "statusicon"

    def __init__(self, on_activate: Callable[[], None]) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self.icon = Gtk.StatusIcon.new_from_file(str(ICON_IDLE_PATH))
            self.icon.set_title(APP_LABEL)
            self.icon.set_tooltip_text(APP_LABEL)
            self.icon.set_visible(True)
        self._menu: Gtk.Menu | None = None
        self.icon.connect("activate", lambda _icon: on_activate())
        self.icon.connect("popup-menu", self._on_popup)

    def set_menu(self, menu: Gtk.Menu, open_item: Gtk.MenuItem) -> None:
        """Remember the menu shown on right-click."""
        self._menu = menu

    def set_icon(self, path: Path, tooltip: str) -> None:
        """Change the icon and tooltip."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self.icon.set_from_file(str(path))
            self.icon.set_tooltip_text(tooltip)

    def _on_popup(self, icon: Gtk.StatusIcon, button: int, time: int) -> None:
        """Show the menu at the icon."""
        if self._menu is None:
            return
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self._menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, time)


def _x11_session() -> bool:
    """True when GDK runs on X11 (StatusIcon works there, not on Wayland)."""
    display = Gdk.Display.get_default()
    return display is not None and type(display).__name__.startswith("X11")


def choose_backend(on_activate: Callable[[], None]) -> _AppIndicatorBackend | _StatusIconBackend:
    """Best tray backend for this desktop, or TrayError when none will show."""
    if IS_WINDOWS:
        return _StatusIconBackend(on_activate)
    if AppIndicator is not None and tray_host_available():
        return _AppIndicatorBackend(on_activate)
    if _x11_session():
        return _StatusIconBackend(on_activate)
    if AppIndicator is not None:
        raise TrayError("No tray host is running (on GNOME, enable the AppIndicator extension).")
    raise TrayError("No tray support: install your distribution's ayatana-appindicator package.")


class TrayManager:
    """Owns the tray icon and its context menu."""

    def __init__(
        self,
        service_manager: ServiceManager,
        window_manager: object,
        app_version: str,
        backend: _AppIndicatorBackend | _StatusIconBackend | None = None,
    ) -> None:
        self._service_manager = service_manager
        self._window_manager = window_manager
        self._app_version = app_version
        self._backend = backend or choose_backend(self._open_dashboard)
        self._menu: Gtk.Menu | None = None
        self._signature: tuple[tuple[str, str, ServiceStatus], ...] | None = None
        self._icon_key: int | None = None
        self._rebuild()

    @property
    def backend_name(self) -> str:
        """'appindicator' or 'statusicon'."""
        return self._backend.name

    @property
    def menu(self) -> Gtk.Menu | None:
        """The current tray menu."""
        return self._menu

    @property
    def _indicator(self) -> object:
        """The AppIndicator object, or the StatusIcon (kept for tests/tools)."""
        backend = self._backend
        return backend.indicator if isinstance(backend, _AppIndicatorBackend) else backend.icon

    def handle_event(self, event: str) -> None:
        """Observer hook; safe to call from any thread."""
        if event == EVENT_SERVICES_CHANGED:
            GLib.idle_add(self._rebuild)

    # ---- menu ----

    def _rebuild(self) -> bool:
        """Rebuild the menu if its content changed; always refresh the icon."""
        services = self._service_manager.all_services()
        signature = tuple((s.id, s.name, s.status) for s in services)
        if signature != self._signature:
            self._signature = signature
            self._install_menu(services)
        self._update_icon(services)
        return GLib.SOURCE_REMOVE

    def _install_menu(self, services: list[Service]) -> None:
        """Create and install a fresh menu."""
        menu = Gtk.Menu.new()
        header = Gtk.MenuItem(label=f"{APP_LABEL} {self._app_version}")
        header.set_sensitive(False)
        menu.append(header)
        open_item = self._menu_item(OPEN_LABEL, lambda _item: self._open_dashboard())
        menu.append(open_item)
        menu.append(Gtk.SeparatorMenuItem())
        if not services:
            empty = Gtk.MenuItem(label=NO_SERVICES_LABEL)
            empty.set_sensitive(False)
            menu.append(empty)
        for service in services:
            menu.append(self._make_service_item(service))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(
            self._menu_item(EXPOSE_LABEL, lambda _i: self._window_manager.show_expose_port())
        )
        menu.append(
            self._menu_item(SERVE_FOLDER_LABEL, lambda _i: self._window_manager.show_serve_folder())
        )
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._menu_item(GALLERY_LABEL, lambda _i: self._window_manager.show_all_qr()))
        menu.append(
            self._menu_item(SETTINGS_LABEL, lambda _i: self._window_manager.show_settings())
        )
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._menu_item(QUIT_LABEL, lambda _i: Gtk.main_quit()))
        menu.show_all()
        self._menu = menu
        self._backend.set_menu(menu, open_item)

    @staticmethod
    def _menu_item(text: str, handler: Callable[[Gtk.MenuItem], None]) -> Gtk.MenuItem:
        """Create a plain menu item wired to an activate handler."""
        item = Gtk.MenuItem(label=text)
        item.connect("activate", handler)
        return item

    def _make_service_item(self, service: Service) -> Gtk.CheckMenuItem:
        """Checked means shared; toggling stops or restarts the service.

        Only 'toggled' is connected — GTK also fires 'activate' on the same
        click, which would double-trigger the action.
        """
        suffix = STATUS_SUFFIXES.get(service.status, "")
        item = Gtk.CheckMenuItem(label=f"{service.name}{suffix}")
        item.set_active(service.status != ServiceStatus.STOPPED)
        item.connect("toggled", self._on_service_toggled, service.id)
        item.set_tooltip_text(service.lan_url)
        return item

    # ---- handlers ----

    def _open_dashboard(self) -> None:
        """Show the dashboard."""
        self._window_manager.show_status_panel()

    def _on_service_toggled(self, item: Gtk.CheckMenuItem, service_id: str) -> None:
        """Start or stop the service off-thread; resync the menu on failure."""
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

        def failure(exc: Exception) -> None:
            logger.warning(f"Toggle failed for '{name}': {error_message(exc)}")
            self._signature = None
            self._rebuild()

        run_off_thread(f"xposer-toggle-{service_id[:8]}", work, None, failure)

    # ---- icons ----

    def _update_icon(self, services: list[Service]) -> None:
        """Idle icon, or the active icon with a count badge."""
        running = sum(1 for s in services if s.status == ServiceStatus.RUNNING)
        if running == self._icon_key:
            return
        self._icon_key = running
        icon = ICON_IDLE_PATH if not running else badge_icon_path(running)
        tooltip = f"{APP_LABEL} — {running} shared" if running else f"{APP_LABEL} — nothing shared"
        self._backend.set_icon(icon, tooltip)


def badge_icon_path(count: int) -> Path:
    """Render (once) and cache the active icon with a count badge."""
    shown = str(count) if count <= MAX_BADGE_COUNT else f"{MAX_BADGE_COUNT}+"
    BADGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = BADGE_CACHE_DIR / f"active_{shown.replace('+', 'plus')}.png"
    if path.exists():
        return path
    base = Image.open(ICON_ACTIVE_PATH).convert("RGBA")
    draw = ImageDraw.Draw(base)
    radius = int(base.width * BADGE_RADIUS_RATIO)
    center = (base.width - radius, base.height - radius)
    draw.ellipse(
        (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
        fill=BADGE_FILL,
    )
    try:
        font = ImageFont.load_default(
            size=BADGE_FONT_SIZE if len(shown) == 1 else BADGE_FONT_SIZE_WIDE
        )
    except TypeError:
        font = ImageFont.load_default()
    draw.text(center, shown, font=font, fill=BADGE_TEXT, anchor="mm")
    temporary = path.with_suffix(".tmp")
    base.save(temporary, format="PNG")
    temporary.replace(path)
    logger.debug(f"Rendered badge icon {path}")
    return path

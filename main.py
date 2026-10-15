"""Xposer entry point: wires components together and runs the GTK loop."""

import signal
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from config.config_store import ConfigStore
from network.network_detector import NetworkDetector
from network.qr_generator import QRGenerator
from services.service_manager import ServiceManager
from tray.tray_manager import TrayError, TrayManager
from utils.logger import get_logger, setup_logging
from utils.platform_env import IS_WINDOWS
from utils.single_instance import SingleInstance
from utils.ui_tasks import deliver_on_main_loop
from windows.window_manager import WindowManager

APP_NAME = "Xposer"
APP_VERSION = "1.1.0"
APP_ID = "io.github.xposer"
WINDOW_ICON_PATH = Path(__file__).resolve().parent / "assets" / "icon_active.png"
SIGNAL_POLL_MILLISECONDS = 500


def main() -> int:
    """Compose the application and block on the GTK main loop."""
    setup_logging()
    logger = get_logger("main")
    if IS_WINDOWS:
        _set_windows_app_id()
    GLib.set_prgname("xposer")
    GLib.set_application_name(APP_NAME)
    if not Gtk.init_check()[0]:
        logger.error("No graphical display available")
        return 1

    window_manager: WindowManager | None = None
    instance = SingleInstance(lambda: deliver_on_main_loop(_show_dashboard, window_manager))
    if not instance.acquire():
        return 0
    _set_window_icon()

    config_store = ConfigStore()
    detector = NetworkDetector()
    qr_generator = QRGenerator()
    service_manager = ServiceManager(config_store, detector, qr_generator)
    window_manager = WindowManager(
        service_manager, detector, qr_generator, config_store, APP_VERSION
    )
    service_manager.add_observer(window_manager.handle_event)
    service_manager.restore_saved_services()

    try:
        tray_manager = TrayManager(service_manager, window_manager, APP_VERSION)
    except TrayError as exc:
        logger.warning(f"{exc} Using the dashboard as the main window.")
        window_manager.use_dashboard_as_main_window()
        window_manager.show_dashboard()
    else:
        service_manager.add_observer(tray_manager.handle_event)
        logger.info(f"Tray icon via {tray_manager.backend_name}")

    _install_signal_handlers()
    service_manager.start_monitor()
    logger.info(f"{APP_NAME} {APP_VERSION} started")
    try:
        Gtk.main()
    finally:
        service_manager.shutdown()
        instance.release()
        logger.info(f"{APP_NAME} exited cleanly")
    return 0


def _show_dashboard(window_manager: WindowManager | None) -> None:
    """Raise the dashboard (second-launch request)."""
    if window_manager is not None:
        window_manager.show_dashboard()


def _set_window_icon() -> None:
    """Icon for every window and the Windows taskbar."""
    try:
        Gtk.Window.set_default_icon_from_file(str(WINDOW_ICON_PATH))
    except GLib.Error as exc:
        get_logger("main").debug(f"Window icon unavailable: {exc}")


def _set_windows_app_id() -> None:
    """Group Xposer's windows under its own taskbar entry instead of Python's."""
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError) as exc:
        get_logger("main").debug(f"AppUserModelID not set: {exc}")


def _install_signal_handlers() -> None:
    """Quit cleanly on Ctrl+C / SIGTERM so engines shut down."""
    if hasattr(GLib, "unix_signal_add") and not IS_WINDOWS:
        for signum in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, _quit_from_signal)
        return
    signal.signal(signal.SIGINT, lambda _signum, _frame: Gtk.main_quit())
    # Python only runs signal handlers between bytecodes; a periodic no-op
    # wakes the interpreter while GTK sits in its C main loop.
    GLib.timeout_add(SIGNAL_POLL_MILLISECONDS, lambda: GLib.SOURCE_CONTINUE)


def _quit_from_signal() -> bool:
    """GLib signal callback."""
    Gtk.main_quit()
    return GLib.SOURCE_REMOVE


if __name__ == "__main__":
    sys.exit(main())

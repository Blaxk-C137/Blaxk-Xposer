"""Xposer entry point: wires components together and runs the GTK loop."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from config.config_store import ConfigStore
from network.network_detector import NetworkDetector
from network.qr_generator import QRGenerator
from services.service_manager import ServiceManager
from tray.tray_host import tray_host_available
from tray.tray_manager import TrayError, TrayManager
from utils.logger import get_logger, setup_logging
from windows.dialogs import confirm
from windows.window_manager import WindowManager

APP_NAME = "Xposer"
APP_VERSION = "1.0.0"

NO_TRAY_HOST_TITLE = "No system tray available"
NO_TRAY_HOST_TEXT = (
    "Nothing on this desktop can display tray icons, so Xposer's icon "
    "would stay invisible. On GNOME, install and enable the 'AppIndicator "
    "& KStatusNotifierItem' extension (Fedora: sudo dnf install "
    "gnome-shell-extension-appindicator), then start Xposer again.\n\n"
    "Keep Xposer running anyway?"
)


def main() -> int:
    """Compose the application and block on the GTK main loop."""
    setup_logging()
    logger = get_logger("main")
    GLib.set_prgname("xposer")
    GLib.set_application_name(APP_NAME)

    config_store = ConfigStore()
    detector = NetworkDetector()
    qr_generator = QRGenerator()
    service_manager = ServiceManager(config_store, detector, qr_generator)
    window_manager = WindowManager(
        service_manager, detector, qr_generator, config_store, APP_VERSION
    )

    try:
        tray_manager = TrayManager(service_manager, window_manager, APP_VERSION)
    except TrayError as exc:
        logger.error(f"{exc}")
        show_startup_error(str(exc))
        return 1

    if not tray_host_available():
        logger.warning("No StatusNotifierWatcher on the session bus")
        if not confirm(None, NO_TRAY_HOST_TITLE, NO_TRAY_HOST_TEXT):
            service_manager.shutdown()
            return 1

    service_manager.add_observer(tray_manager.handle_event)
    service_manager.add_observer(window_manager.handle_event)
    service_manager.start_monitor()
    logger.info("Xposer started")

    try:
        Gtk.main()
    finally:
        service_manager.shutdown()
        logger.info("Xposer exited cleanly")
    return 0


def show_startup_error(message: str) -> None:
    """Report a fatal startup problem before any window exists."""
    dialog = Gtk.MessageDialog(
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.OK,
        text="Xposer cannot start",
    )
    dialog.format_secondary_text(message)
    dialog.run()
    dialog.destroy()


if __name__ == "__main__":
    raise SystemExit(main())

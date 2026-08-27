"""The QR code window shown when a service is selected."""

from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

from models.service import Service, ServiceStatus
from network.qr_generator import QRGenerator
from services.service_manager import ServiceError, ServiceManager
from utils.logger import get_logger
from utils.ui_tasks import run_off_thread
from windows.dialogs import confirm, show_error

logger = get_logger("windows")

REFRESH_INTERVAL_SECONDS = 2
QR_DISPLAY_SIZE_PX = 280
COPY_FEEDBACK_MILLISECONDS = 1500


class QRCodeWindow(Gtk.Window):
    """Shows one service's QR code, URL, and sharing controls."""

    def __init__(
        self,
        service_manager: ServiceManager,
        qr_generator: QRGenerator,
        service: Service,
        window_manager: object | None = None,
    ) -> None:
        super().__init__(title=service.name)
        self._service_manager = service_manager
        self._qr_generator = qr_generator
        self._service_id = service.id
        self._window_manager = window_manager
        self._timeout_id: int | None = None
        self._destroyed = False
        self.set_default_size(350, 450)
        self._build_ui()
        self.connect("destroy", self._on_destroyed)
        self.refresh()
        if self._service() is not None:
            self._timeout_id = GLib.timeout_add_seconds(
                REFRESH_INTERVAL_SECONDS, self._on_refresh_tick
            )

    def _build_ui(self) -> None:
        header = Gtk.HeaderBar(title=self._title_text(), show_close_button=True)
        header.set_subtitle("Scan to open on this WiFi")
        self.set_titlebar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_top(16)
        outer.set_margin_bottom(16)
        outer.set_margin_start(16)
        outer.set_margin_end(16)
        outer.set_halign(Gtk.Align.CENTER)
        self.add(outer)

        self._qr_image = Gtk.Image()
        outer.pack_start(self._qr_image, False, False, 0)

        self._url_label = Gtk.Label(selectable=True)
        self._url_label.set_justify(Gtk.Justification.CENTER)
        self._url_label.get_style_context().add_class("xposer-mono")
        self._url_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        outer.pack_start(self._url_label, False, False, 0)

        self._devices_label = Gtk.Label()
        outer.pack_start(self._devices_label, False, False, 0)

        link_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True
        )
        self._copy_button = Gtk.Button(label="Copy Link")
        self._copy_button.connect("clicked", self._on_copy_clicked)
        link_row.pack_start(self._copy_button, True, True, 0)
        save = Gtk.Button(label="Save QR as PNG")
        save.connect("clicked", self._on_save_clicked)
        link_row.pack_start(save, True, True, 0)
        outer.pack_start(link_row, False, False, 0)

        share_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        self._stop_button = Gtk.Button(label="Stop Sharing")
        self._stop_button.get_style_context().add_class("destructive-action")
        self._stop_button.connect("clicked", self._on_stop_clicked)
        share_row.pack_start(self._stop_button, True, True, 0)

        self._start_button = Gtk.Button(label="Start Sharing")
        self._start_button.get_style_context().add_class("suggested-action")
        self._start_button.connect("clicked", self._on_start_clicked)
        share_row.pack_start(self._start_button, True, True, 0)

        self._spinner = Gtk.Spinner()
        share_row.pack_start(self._spinner, False, False, 0)
        outer.pack_start(share_row, False, False, 0)

        self.show_all()

    def _service(self) -> Service | None:
        """Return the live record for this window's service, or None."""
        return self._service_manager.get_service(self._service_id)

    def _title_text(self) -> str:
        """Return the header title for the current service."""
        service = self._service()
        return service.name if service else "Service"

    def refresh(self) -> None:
        """Sync every widget to the service's live state.

        Destroys the window when the underlying service no longer exists.
        """
        if self._destroyed:
            return
        service = self._service()
        if service is None:
            self.destroy()
            return
        self._show_qr(service.qr_image_path)
        self._url_label.set_text(service.lan_url)
        self._devices_label.set_text(self._devices_text(service))
        running = service.status == ServiceStatus.RUNNING
        self._stop_button.set_visible(running)
        self._start_button.set_visible(not running)

    def _show_qr(self, qr_image_path: str | None) -> None:
        """Load the QR image at display size, keeping the old one on failure."""
        if not qr_image_path:
            return
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
                qr_image_path, QR_DISPLAY_SIZE_PX, QR_DISPLAY_SIZE_PX
            )
        except (OSError, GLib.Error) as exc:
            logger.debug(f"QR image unavailable: {exc}")
            return
        self._qr_image.set_from_pixbuf(pixbuf)

    @staticmethod
    def _devices_text(service: Service) -> str:
        """Describe connected devices plus any non-running status note."""
        if service.connected_devices == 1:
            devices = "1 device connected"
        else:
            devices = f"{service.connected_devices} devices connected"
        status_note = ""
        if service.status == ServiceStatus.STOPPED:
            status_note = " · stopped"
        elif service.status == ServiceStatus.ERROR:
            status_note = " · problem detected"
        return f"{devices}{status_note}"

    def _on_refresh_tick(self) -> bool:
        """Poll the service manager so state stays live."""
        self.refresh()
        return GLib.SOURCE_CONTINUE

    def _on_destroyed(self, _widget: Gtk.Widget) -> None:
        """Detach the poll timer, mark the window dead, forget it."""
        self._destroyed = True
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None
        if self._window_manager is not None:
            self._window_manager.forget_qr_window(self._service_id)

    def _on_copy_clicked(self, button: Gtk.Button) -> None:
        """Put the LAN URL on the clipboard and flash feedback."""
        service = self._service()
        if service is None:
            return
        clipboard = Gtk.Clipboard.get_default(Gdk.Display.get_default())
        clipboard.set_text(service.lan_url, -1)
        previous = button.get_label()
        button.set_label("Copied!")
        GLib.timeout_add(
            COPY_FEEDBACK_MILLISECONDS,
            self._restore_copy_label,
            button,
            previous,
        )

    def _restore_copy_label(self, button: Gtk.Button, label: str) -> bool:
        """Put the copy button's original label back after the flash."""
        if self._destroyed:
            return GLib.SOURCE_REMOVE
        button.set_label(label)
        return GLib.SOURCE_REMOVE

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        """Ask for a destination and copy the QR PNG there."""
        service = self._service()
        if service is None or not service.qr_image_path:
            return
        dialog = Gtk.FileChooserDialog(
            title="Save QR Code",
            transient_for=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name(f"{service.name}.png")
        response = dialog.run()
        destination = dialog.get_filename()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not destination:
            return
        try:
            self._qr_generator.save_copy(
                Path(service.qr_image_path), Path(destination)
            )
        except OSError as exc:
            show_error(self, f"Could not save image: {exc}")

    def _set_busy(self, busy: bool) -> None:
        """Toggle the sharing controls while a lifecycle call is in flight."""
        self._stop_button.set_sensitive(not busy)
        self._start_button.set_sensitive(not busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()

    def _run_lifecycle(self, operation: Callable[[], None], name: str) -> None:
        """Run a manager lifecycle call off-thread and finish on the main loop."""
        run_off_thread(
            name,
            operation,
            on_success=self._lifecycle_succeeded,
            on_error=self._lifecycle_failed,
        )

    def _lifecycle_failed(self, exc: Exception) -> None:
        """Report a failed stop/restart and restore the controls."""
        if self._destroyed:
            return
        self._set_busy(False)
        message = exc.user_message if isinstance(exc, ServiceError) else str(exc)
        show_error(self, message)

    def _lifecycle_succeeded(self, _result: None) -> None:
        """Refresh the window after a successful stop/restart."""
        if self._destroyed:
            return
        self._set_busy(False)
        self.refresh()

    def _on_stop_clicked(self, _button: Gtk.Button) -> None:
        """Confirm, then stop sharing this service without blocking the UI."""
        service = self._service()
        if service is None:
            return
        service_id = service.id
        approved = confirm(
            self,
            f"Stop sharing '{service.name}'?",
            "The link and QR code will stop working immediately.",
        )
        if not approved:
            return
        self._set_busy(True)
        self._run_lifecycle(
            lambda: self._service_manager.stop_service(service_id),
            "xposer-stop",
        )

    def _on_start_clicked(self, _button: Gtk.Button) -> None:
        """Restart a stopped service without blocking the UI."""
        self._set_busy(True)
        self._run_lifecycle(
            lambda: self._service_manager.restart_service(self._service_id),
            "xposer-start",
        )

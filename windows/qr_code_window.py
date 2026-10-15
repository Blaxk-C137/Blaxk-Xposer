"""The QR code window: one service's code, links and sharing controls.

The QR defaults to the IP link because every phone can open it; when a
custom .local link is live, a toggle shows its code instead. The window
refreshes on manager events (no polling) and only reloads the image when
its file changes.
"""

from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from models.service import LocalProtocol, Service, ServiceStatus, ShareScheme
from network.firewall import allow_through_windows_firewall, firewall_help
from network.qr_generator import QRGenerator
from services.service_manager import ServiceManager
from utils.logger import get_logger
from utils.ui_tasks import run_off_thread
from windows.dialogs import FIREWALL_RULE_FAILED_TEXT, confirm, show_error
from windows.theme import (
    QR_DISPLAY_SIZE,
    SPACING_M,
    SPACING_S,
    SPACING_XS,
    WINDOW_MARGIN,
    add_classes,
    padded,
)
from windows.widgets import (
    CopyButton,
    error_message,
    firewall_help_popover,
    icon_button,
    label,
    load_pixbuf,
    open_link,
    traffic_text,
    url_label,
)

logger = get_logger("windows")

QR_FRAME_PADDING = 12


class QRCodeWindow(Gtk.Window):
    """Shows one service's QR code, links, and sharing controls."""

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
        self._destroyed = False
        self._shown_qr: str | None = None
        self._busy = False
        self.set_default_size(-1, -1)
        self.set_resizable(False)
        self._build_ui()
        self.connect("destroy", self._on_destroyed)
        self.refresh()

    # ---- construction ----

    def _build_ui(self) -> None:
        """QR image, link toggle, link row, details and actions."""
        self._header = Gtk.HeaderBar(title=self._title_text(), show_close_button=True)
        self._header.set_subtitle("Scan with a phone on the same Wi-Fi")
        self.set_titlebar(self._header)

        outer = padded(
            Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACING_M), WINDOW_MARGIN
        )
        self.add(outer)

        self._link_switch = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        add_classes(self._link_switch, "linked")
        self._link_switch.set_halign(Gtk.Align.CENTER)
        self._link_switch.set_no_show_all(True)
        self._ip_radio = Gtk.RadioButton.new_with_label(None, "IP link")
        self._custom_radio = Gtk.RadioButton.new_with_label_from_widget(
            self._ip_radio, "Custom link"
        )
        for radio in (self._ip_radio, self._custom_radio):
            radio.set_mode(False)
            radio.connect("toggled", self._on_link_toggled)
            self._link_switch.pack_start(radio, False, False, 0)
            radio.show()
        outer.pack_start(self._link_switch, False, False, 0)

        frame = Gtk.EventBox()
        add_classes(frame, "xposer-qr-frame")
        frame.set_halign(Gtk.Align.CENTER)
        self._qr_image = padded(Gtk.Image(), QR_FRAME_PADDING)
        self._qr_image.set_size_request(QR_DISPLAY_SIZE, QR_DISPLAY_SIZE)
        frame.add(self._qr_image)
        outer.pack_start(frame, False, False, 0)

        link_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_XS)
        self._url_label = url_label()
        self._url_label.set_xalign(0.5)
        link_row.pack_start(self._url_label, True, True, 0)
        self._copy_button = CopyButton()
        link_row.pack_start(self._copy_button, False, False, 0)
        link_row.pack_start(
            icon_button("web-browser-symbolic", "Open in browser", self._on_open_clicked),
            False,
            False,
            0,
        )
        outer.pack_start(link_row, False, False, 0)

        self._scheme_label = label("", "xposer-dim", "xposer-small", xalign=0.5, wrap=True)
        self._scheme_label.set_justify(Gtk.Justification.CENTER)
        outer.pack_start(self._scheme_label, False, False, 0)
        self._devices_label = label("", xalign=0.5, wrap=True)
        self._devices_label.set_justify(Gtk.Justification.CENTER)
        outer.pack_start(self._devices_label, False, False, 0)

        self._hint_label = label("", "xposer-small", xalign=0.5, wrap=True)
        self._hint_label.set_justify(Gtk.Justification.CENTER)
        self._hint_label.set_no_show_all(True)
        outer.pack_start(self._hint_label, False, False, 0)
        self._hint_button = Gtk.Button(label="Share it too")
        self._hint_button.set_halign(Gtk.Align.CENTER)
        self._hint_button.set_no_show_all(True)
        self._hint_button.connect("clicked", self._on_hint_clicked)
        outer.pack_start(self._hint_button, False, False, 0)

        help_button = Gtk.Button(label="Can't connect?")
        help_button.set_relief(Gtk.ReliefStyle.NONE)
        help_button.set_halign(Gtk.Align.CENTER)
        help_button.connect("clicked", self._on_help_clicked)
        outer.pack_start(help_button, False, False, 0)

        actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_S, homogeneous=True
        )
        save = Gtk.Button(label="Save QR…")
        save.connect("clicked", self._on_save_clicked)
        actions.pack_start(save, True, True, 0)
        self._stop_button = Gtk.Button(label="Stop sharing")
        add_classes(self._stop_button, "destructive-action")
        self._stop_button.connect("clicked", self._on_stop_clicked)
        actions.pack_start(self._stop_button, True, True, 0)
        self._start_button = Gtk.Button(label="Start sharing")
        add_classes(self._start_button, "suggested-action")
        self._start_button.connect("clicked", self._on_start_clicked)
        actions.pack_start(self._start_button, True, True, 0)
        outer.pack_start(actions, False, False, 0)
        self._spinner = Gtk.Spinner()
        outer.pack_start(self._spinner, False, False, 0)
        self.show_all()

    def _service(self) -> Service | None:
        """Return the live record for this window's service, or None."""
        return self._service_manager.get_service(self._service_id)

    def _title_text(self) -> str:
        """Return the header title for the current service."""
        service = self._service()
        return service.name if service else "Service"

    # ---- refresh ----

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
        self._header.set_title(service.name)
        custom_available = service.hostname_active and bool(service.qr_custom_path)
        self._link_switch.set_visible(custom_available)
        use_custom = custom_available and self._custom_radio.get_active()
        url = service.lan_url if use_custom else (service.ip_url or service.lan_url)
        self._show_qr(service.qr_custom_path if use_custom else service.qr_image_path)
        live = service.status != ServiceStatus.STOPPED
        self._url_label.set_text(url or "Not shared right now")
        self._url_label.set_sensitive(live)
        self._copy_button.set_text(url if live else "")
        self._scheme_label.set_text(_scheme_text(service))
        self._devices_label.set_text(_status_text(service))
        hints = service.backend_hints if live else ()
        self._hint_label.set_visible(bool(hints))
        self._hint_button.set_visible(bool(hints))
        if hints:
            self._hint_label.set_text(
                f"This app calls {hints[0].description} on this computer, which isn't shared yet."
            )
        self._stop_button.set_visible(live)
        self._start_button.set_visible(not live)
        if not self._busy:
            self._stop_button.set_sensitive(True)
            self._start_button.set_sensitive(True)

    def _show_qr(self, qr_image_path: str | None) -> None:
        """Load the QR image only when it changed; keep the old one on failure."""
        if not qr_image_path or qr_image_path == self._shown_qr:
            return
        pixbuf = load_pixbuf(qr_image_path, QR_DISPLAY_SIZE)
        if pixbuf is None:
            return
        self._qr_image.set_from_pixbuf(pixbuf)
        self._shown_qr = qr_image_path

    # ---- handlers ----

    def _on_destroyed(self, _widget: Gtk.Widget) -> None:
        """Mark the window dead and forget it."""
        self._destroyed = True
        if self._window_manager is not None:
            self._window_manager.forget_qr_window(self._service_id)

    def _on_link_toggled(self, radio: Gtk.RadioButton) -> None:
        """Switch between IP and custom-link QR codes."""
        if radio.get_active():
            self.refresh()

    def _on_open_clicked(self, _button: Gtk.Button) -> None:
        """Open the shown link in the local browser."""
        open_link(self, self._url_label.get_text())

    def _on_help_clicked(self, button: Gtk.Button) -> None:
        """Explain firewall and Wi-Fi requirements."""
        service = self._service()
        port = service.assigned_port if service else None
        firewall_help_popover(button, firewall_help(port), self._allow_firewall).popup()

    def _allow_firewall(self) -> None:
        """Ask Windows to allow inbound connections (UAC prompt)."""
        if not allow_through_windows_firewall():
            show_error(
                self,
                FIREWALL_RULE_FAILED_TEXT,
            )

    def _on_hint_clicked(self, _button: Gtk.Button) -> None:
        """Share the backend this app calls."""
        service = self._service()
        if service is None or not service.backend_hints:
            return
        port = service.backend_hints[0].port
        self._hint_button.set_sensitive(False)
        self._run_lifecycle(
            lambda: self._service_manager.share_backend(self._service_id, port), "xposer-backend"
        )

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        """Ask for a destination and copy the shown QR PNG there."""
        service = self._service()
        if service is None or not self._shown_qr:
            return
        chooser = Gtk.FileChooserNative.new(
            "Save QR code", self, Gtk.FileChooserAction.SAVE, "Save", "Cancel"
        )
        chooser.set_do_overwrite_confirmation(True)
        chooser.set_current_name(f"{service.name}.png")
        accepted = chooser.run() == Gtk.ResponseType.ACCEPT
        destination = chooser.get_filename()
        chooser.destroy()
        if not accepted or not destination:
            return
        try:
            self._qr_generator.save_copy(Path(self._shown_qr), Path(destination))
        except OSError as exc:
            show_error(self, f"Could not save image: {exc}")

    def _set_busy(self, busy: bool) -> None:
        """Toggle the sharing controls while a lifecycle call is in flight."""
        self._busy = busy
        self._stop_button.set_sensitive(not busy)
        self._start_button.set_sensitive(not busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()

    def _run_lifecycle(self, operation: Callable[[], object], name: str) -> None:
        """Run a manager call off-thread and finish on the main loop."""
        self._set_busy(True)
        run_off_thread(name, operation, self._lifecycle_succeeded, self._lifecycle_failed)

    def _lifecycle_failed(self, exc: Exception) -> None:
        """Report a failed operation and restore the controls."""
        if self._destroyed:
            return
        self._set_busy(False)
        self._hint_button.set_sensitive(True)
        show_error(self, error_message(exc))

    def _lifecycle_succeeded(self, _result: object) -> None:
        """Refresh the window after a successful operation."""
        if self._destroyed:
            return
        self._set_busy(False)
        self._hint_button.set_sensitive(True)
        self.refresh()

    def _on_stop_clicked(self, _button: Gtk.Button) -> None:
        """Confirm, then stop sharing this service without blocking the UI."""
        service = self._service()
        if service is None:
            return
        if not confirm(
            self,
            f"Stop sharing '{service.name}'?",
            "The link and QR code stop working immediately.",
        ):
            return
        self._run_lifecycle(
            lambda: self._service_manager.stop_service(self._service_id), "xposer-stop"
        )

    def _on_start_clicked(self, _button: Gtk.Button) -> None:
        """Restart a stopped service without blocking the UI."""
        self._run_lifecycle(
            lambda: self._service_manager.restart_service(self._service_id), "xposer-start"
        )


def _scheme_text(service: Service) -> str:
    """Explain how devices reach the app."""
    if service.folder_path:
        return f"Folder: {service.folder_path}"
    if service.share_scheme == ShareScheme.TCP:
        return f"Raw TCP relay to localhost:{service.port} (for non-web apps)"
    if service.local_protocol == LocalProtocol.HTTPS and service.share_scheme == ShareScheme.HTTP:
        return (
            f"HTTPS app on localhost:{service.port}, opened with plain http:// — "
            "no certificate warning"
        )
    if service.share_scheme == ShareScheme.HTTPS:
        return (
            f"HTTPS end to end from localhost:{service.port} — devices warn about the "
            "certificate, and links inside the app are not adjusted"
        )
    return f"Web app on localhost:{service.port}"


def _status_text(service: Service) -> str:
    """Traffic for live services, or the reason they are not live."""
    if service.status == ServiceStatus.RUNNING:
        return traffic_text(service)
    if service.status == ServiceStatus.ERROR:
        return service.last_error or "Problem detected"
    return service.last_error or "Not shared right now"

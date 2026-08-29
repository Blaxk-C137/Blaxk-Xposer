"""The 'Expose a Port' window: share an already-running localhost server."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango

from models.service import Service
from network.network_detector import ListeningPort, NetworkDetector
from services.service_manager import ServiceError, ServiceManager
from utils.logger import get_logger
from utils.ui_tasks import run_off_thread
from windows.dialogs import FIREWALL_HINT, FIREWALL_WARNING_TITLE, show_error, show_warning

logger = get_logger("windows")

DEFAULT_PORT = 3000
MIN_PORT = 1
MAX_PORT = 65535
MAX_LISTED_PORTS = 50


class ExposePortWindow(Gtk.Window):
    """Form exposing a running localhost port to the LAN."""

    def __init__(
        self,
        service_manager: ServiceManager,
        detector: NetworkDetector,
        window_manager: object | None = None,
    ) -> None:
        super().__init__(title="Expose a Port")
        self._service_manager = service_manager
        self._detector = detector
        self._window_manager = window_manager
        self._destroyed = False
        self.set_default_size(400, 300)
        self.set_resizable(True)
        self.connect("destroy", self._on_destroyed)
        self._build_ui()
        self._load_detected_ports()

    def _on_destroyed(self, _widget: Gtk.Widget) -> None:
        """Mark the window dead so queued callbacks ignore it."""
        self._destroyed = True

    def _build_ui(self) -> None:
        header = Gtk.HeaderBar(
            title="Expose a Port",
            subtitle="Share a server that is already running",
            show_close_button=True,
        )
        self.set_titlebar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        self.add(outer)

        form = Gtk.Grid(column_spacing=8, row_spacing=8)
        outer.pack_start(form, False, False, 0)

        form.attach(Gtk.Label(label="Service Name", halign=Gtk.Align.START), 0, 0, 1, 1)
        self._name_entry = Gtk.Entry(hexpand=True)
        self._name_entry.set_placeholder_text("My React App")
        self._name_entry.set_activates_default(True)
        form.attach(self._name_entry, 1, 0, 1, 1)

        form.attach(Gtk.Label(label="Port", halign=Gtk.Align.START), 0, 1, 1, 1)
        adjustment = Gtk.Adjustment(
            value=DEFAULT_PORT,
            lower=MIN_PORT,
            upper=MAX_PORT,
            step_increment=1,
            page_increment=10,
        )
        self._port_spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=1.0)
        form.attach(self._port_spin, 1, 1, 1, 1)

        form.attach(Gtk.Label(label="Hostname", halign=Gtk.Align.START), 0, 2, 1, 1)
        self._hostname_entry = Gtk.Entry(hexpand=True)
        self._hostname_entry.set_placeholder_text("myapp  (becomes myapp.local)")
        form.attach(self._hostname_entry, 1, 2, 1, 1)

        ports_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ports_header.pack_start(
            Gtk.Label(label="Detected Running Ports", halign=Gtk.Align.START),
            True,
            True,
            0,
        )
        refresh = Gtk.Button(
            image=Gtk.Image.new_from_icon_name(
                "view-refresh-symbolic", Gtk.IconSize.BUTTON
            )
        )
        refresh.set_tooltip_text("Scan again")
        refresh.connect("clicked", lambda _button: self._load_detected_ports())
        ports_header.pack_end(refresh, False, False, 0)
        outer.pack_start(ports_header, False, False, 0)

        scrolled = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN)
        scrolled.set_vexpand(True)
        scrolled.set_min_content_height(110)
        self._ports_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._ports_list.set_activate_on_single_click(False)
        scrolled.add(self._ports_list)
        outer.pack_start(scrolled, True, True, 0)

        self._spinner = Gtk.Spinner()
        outer.pack_start(self._spinner, False, False, 0)

        actions = Gtk.ButtonBox(orientation=Gtk.Orientation.HORIZONTAL)
        actions.set_layout(Gtk.ButtonBoxStyle.END)
        actions.set_spacing(6)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _button: self.destroy())
        self._expose_button = Gtk.Button(label="Expose", can_default=True)
        self._expose_button.get_style_context().add_class("suggested-action")
        self._expose_button.connect("clicked", self._on_expose_clicked)
        actions.add(cancel)
        actions.add(self._expose_button)
        outer.pack_end(actions, False, False, 0)

        self.show_all()

    def _load_detected_ports(self) -> None:
        """Scan listening ports off-thread, excluding our own assigned ports."""
        self._clear_rows()
        placeholder = Gtk.ListBoxRow(sensitive=False)
        placeholder.add(
            Gtk.Label(label="Scanning…", halign=Gtk.Align.START, margin=8)
        )
        self._ports_list.add(placeholder)
        self._ports_list.show_all()

        def work() -> list[ListeningPort]:
            busy = self._service_manager.active_ports()
            return [
                entry
                for entry in self._detector.detect_listening_ports()
                if entry.port not in busy
            ]

        run_off_thread(
            "xposer-scan", work, self._populate_ports, self._scan_failed
        )

    def _scan_failed(self, exc: Exception) -> None:
        """Show the empty state when the port scan blows up."""
        logger.warning(f"Port scan failed: {exc}")
        self._populate_ports([])

    def _clear_rows(self) -> None:
        """Remove every row currently shown in the detected-ports list."""
        self._ports_list.foreach(lambda child: self._ports_list.remove(child))

    def _populate_ports(self, entries: list[ListeningPort]) -> None:
        """Replace the list contents with the scanned entries (main thread)."""
        if self._destroyed:
            return
        self._clear_rows()
        if not entries:
            row = Gtk.ListBoxRow(sensitive=False)
            row.add(
                Gtk.Label(
                    label="No running local servers found.",
                    halign=Gtk.Align.START,
                    margin=8,
                )
            )
            self._ports_list.add(row)
        for entry in entries[:MAX_LISTED_PORTS]:
            row = Gtk.ListBoxRow(selectable=False)
            row.add(self._make_port_row(entry))
            self._ports_list.add(row)
        self._ports_list.show_all()

    def _make_port_row(self, entry: ListeningPort) -> Gtk.Widget:
        """Build one row: port number, process name, and a Use button."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin=6)
        port_label = Gtk.Label(label=str(entry.port))
        port_label.get_style_context().add_class("xposer-mono")
        port_label.set_width_chars(6)
        box.pack_start(port_label, False, False, 0)
        process_label = Gtk.Label(
            label=entry.process_name,
            halign=Gtk.Align.START,
            hexpand=True,
        )
        process_label.set_ellipsize(Pango.EllipsizeMode.END)
        box.pack_start(process_label, True, True, 0)
        pick = Gtk.Button(label="Use", valign=Gtk.Align.CENTER)
        pick.connect("clicked", self._on_use_port_clicked, entry.port)
        box.pack_end(pick, False, False, 0)
        return box

    def _on_use_port_clicked(self, _button: Gtk.Button, port: int) -> None:
        """Copy the picked port into the form's spin button."""
        self._port_spin.set_value(port)

    def _on_expose_clicked(self, _button: Gtk.Button) -> None:
        """Validate the form and expose the port on a worker thread."""
        name = self._name_entry.get_text().strip()
        if not name:
            show_error(self, "Please enter a service name.")
            return
        port = int(self._port_spin.get_value())
        hostname = self._hostname_entry.get_text().strip() or None
        self._set_busy(True)

        def work() -> tuple[Service, bool]:
            service = self._service_manager.expose_port(name, port, hostname)
            reachable = self._service_manager.lan_reachable(service)
            return service, reachable

        run_off_thread(
            "xposer-expose",
            work,
            self._expose_succeeded,
            self._lifecycle_failed,
        )

    def _set_busy(self, busy: bool) -> None:
        """Toggle the action button and spinner while work is in flight."""
        self._expose_button.set_sensitive(not busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()

    def _lifecycle_failed(self, exc: Exception) -> None:
        """Report a failed expose attempt and re-enable the form."""
        if self._destroyed:
            return
        self._set_busy(False)
        message = exc.user_message if isinstance(exc, ServiceError) else str(exc)
        show_error(self, message)

    def _expose_succeeded(self, result: tuple[Service, bool]) -> None:
        """Close the form and hand the new service to the window manager."""
        if self._destroyed:
            return
        service, reachable = result
        self.destroy()
        if self._window_manager is not None:
            self._window_manager.open_qr_window(service)
        if not reachable:
            show_warning(None, FIREWALL_WARNING_TITLE, FIREWALL_HINT)

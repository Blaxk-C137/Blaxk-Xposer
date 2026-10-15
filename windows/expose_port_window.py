"""The 'Share a running app' window: expose a localhost port to the LAN.

The detected-ports list and the protocol probe both run off-thread; the
probe is debounced so typing a port number does not fire a connection
per keystroke, and stale results are discarded by a generation counter.
"""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango

from models.service import Service, ShareScheme
from network.network_detector import (
    UNKNOWN_PROCESS,
    ListeningPort,
    NetworkDetector,
    UpstreamKind,
    UpstreamProbe,
)
from services.service_manager import ServiceManager
from utils.logger import get_logger
from utils.ui_tasks import run_off_thread
from windows.dialogs import (
    UNREACHABLE_HINT,
    UNREACHABLE_WARNING_TITLE,
    show_error,
    show_warning,
)
from windows.theme import (
    FORM_WIDTH,
    SPACING_M,
    SPACING_S,
    WINDOW_MARGIN,
    add_classes,
    padded,
)
from windows.widgets import error_message, label

logger = get_logger("windows")

DEFAULT_PORT = 3000
MIN_PORT = 1
MAX_PORT = 65535
MAX_LISTED_PORTS = 50
PROBE_DEBOUNCE_MILLISECONDS = 350
PORTS_LIST_MIN_HEIGHT = 150
FIRST_UNPRIVILEGED_PORT = 1024
CUSTOM_LINK_HINT = (
    "Optional, e.g. myapp → myapp.local. Works on iPhone, Mac, Windows and "
    "Linux; many Android phones need the IP link, which is always shown too."
)
HTTPS_TRADEOFF = (
    "With https:// devices show a certificate warning, and Xposer cannot adjust "
    "links inside the app (backend URLs, host checks). Prefer http:// unless "
    "you need secure-only browser features."
)
CUSTOM_LINK_UNAVAILABLE = (
    "Custom links need Avahi (Linux) or the optional “zeroconf” Python package."
)


class ExposePortWindow(Gtk.Window):
    """Form exposing a running localhost port to the LAN."""

    def __init__(
        self,
        service_manager: ServiceManager,
        detector: NetworkDetector | None,
        window_manager: object | None = None,
    ) -> None:
        super().__init__(title="Share a running app")
        self._service_manager = service_manager
        self._detector = detector
        self._window_manager = window_manager
        self._destroyed = False
        self._probe_generation = 0
        self._probe_source: int | None = None
        self._probe: UpstreamProbe | None = None
        self._name_is_auto = True
        self._filling_name = False
        self._labels: dict[int, str] = {}
        self.set_default_size(FORM_WIDTH, -1)
        self.connect("destroy", self._on_destroyed)
        self._build_ui()
        self._load_detected_ports()
        self._schedule_probe()

    def _on_destroyed(self, _widget: Gtk.Widget) -> None:
        """Mark the window dead so queued callbacks ignore it."""
        self._destroyed = True
        if self._probe_source is not None:
            GLib.source_remove(self._probe_source)
            self._probe_source = None

    # ---- construction ----

    def _build_ui(self) -> None:
        """Detected apps list, form, protocol status and actions."""
        header = Gtk.HeaderBar(
            title="Share a running app",
            subtitle="Devices on your Wi-Fi can open it",
            show_close_button=True,
        )
        self.set_titlebar(header)
        outer = padded(
            Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACING_M), WINDOW_MARGIN
        )
        self.add(outer)

        ports_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_S)
        ports_header.pack_start(label("Running on this computer", "xposer-title"), True, True, 0)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Scan again")
        refresh.get_accessible().set_name("Scan again")
        refresh.connect("clicked", lambda _button: self._load_detected_ports())
        ports_header.pack_end(refresh, False, False, 0)
        outer.pack_start(ports_header, False, False, 0)

        scrolled = Gtk.ScrolledWindow(
            shadow_type=Gtk.ShadowType.IN, hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        scrolled.set_min_content_height(PORTS_LIST_MIN_HEIGHT)
        scrolled.set_vexpand(True)
        self._ports_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._ports_list.set_activate_on_single_click(True)
        self._ports_list.connect("row-activated", self._on_port_row_activated)
        scrolled.add(self._ports_list)
        outer.pack_start(scrolled, True, True, 0)

        form = Gtk.Grid(column_spacing=SPACING_M, row_spacing=SPACING_S)
        outer.pack_start(form, False, False, 0)
        form.attach(label("Name"), 0, 0, 1, 1)
        self._name_entry = Gtk.Entry(hexpand=True)
        self._name_entry.set_placeholder_text("My React App")
        self._name_entry.set_activates_default(True)
        self._name_entry.connect("changed", self._on_name_changed)
        form.attach(self._name_entry, 1, 0, 1, 1)

        form.attach(label("Port"), 0, 1, 1, 1)
        adjustment = Gtk.Adjustment(
            value=DEFAULT_PORT, lower=MIN_PORT, upper=MAX_PORT, step_increment=1, page_increment=10
        )
        self._port_spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=1.0, numeric=True)
        self._port_spin.set_halign(Gtk.Align.START)
        self._port_spin.connect("value-changed", lambda _spin: self._schedule_probe())
        form.attach(self._port_spin, 1, 1, 1, 1)

        form.attach(label("Custom link"), 0, 2, 1, 1)
        link_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_S)
        self._hostname_entry = Gtk.Entry(hexpand=True)
        self._hostname_entry.set_placeholder_text("myapp")
        link_box.pack_start(self._hostname_entry, True, True, 0)
        link_box.pack_start(label(".local", "xposer-dim", "xposer-mono"), False, False, 0)
        form.attach(link_box, 1, 2, 1, 1)
        self._hostname_hint = label(CUSTOM_LINK_HINT, "xposer-dim", "xposer-small", wrap=True)
        form.attach(self._hostname_hint, 1, 3, 1, 1)
        if not self._hostname_backends():
            self._hostname_entry.set_sensitive(False)
            self._hostname_hint.set_text(CUSTOM_LINK_UNAVAILABLE)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_S)
        self._probe_spinner = Gtk.Spinner()
        status_row.pack_start(self._probe_spinner, False, False, 0)
        self._probe_label = label("", "xposer-small", wrap=True)
        status_row.pack_start(self._probe_label, True, True, 0)
        outer.pack_start(status_row, False, False, 0)

        self._scheme_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACING_S)
        self._scheme_box.set_no_show_all(True)
        self._http_radio = Gtk.RadioButton.new_with_label(
            None, "Open with http:// — no certificate warning (recommended)"
        )
        self._https_radio = Gtk.RadioButton.new_with_label_from_widget(
            self._http_radio, "Keep https:// — for camera, mic and other secure-only features"
        )
        self._https_radio.set_tooltip_text(HTTPS_TRADEOFF)
        for radio in (self._http_radio, self._https_radio):
            self._scheme_box.pack_start(radio, False, False, 0)
            radio.show()
        tradeoff = label(HTTPS_TRADEOFF, "xposer-dim", "xposer-small", wrap=True)
        self._scheme_box.pack_start(tradeoff, False, False, 0)
        tradeoff.show()
        outer.pack_start(self._scheme_box, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_S)
        actions.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _button: self.destroy())
        self._busy_spinner = Gtk.Spinner()
        self._expose_button = Gtk.Button(label="Share", can_default=True)
        add_classes(self._expose_button, "suggested-action")
        self._expose_button.connect("clicked", self._on_expose_clicked)
        actions.pack_start(self._busy_spinner, False, False, 0)
        actions.pack_start(cancel, False, False, 0)
        actions.pack_start(self._expose_button, False, False, 0)
        outer.pack_end(actions, False, False, 0)
        self.show_all()
        self._expose_button.grab_default()

    def _hostname_backends(self) -> list[str]:
        """mDNS backends, tolerating managers that predate the query."""
        support = getattr(self._service_manager, "hostname_support", None)
        return support() if callable(support) else ["unknown"]

    # ---- detected ports ----

    def _load_detected_ports(self) -> None:
        """Scan listening ports off-thread."""
        self._set_list_message("Scanning…")
        run_off_thread(
            "xposer-scan",
            lambda: self._service_manager.detected_ports(),
            self._populate_ports,
            self._scan_failed,
        )

    def _scan_failed(self, exc: Exception) -> None:
        """Show the empty state when the port scan fails."""
        logger.warning(f"Port scan failed: {exc}")
        self._populate_ports([])

    def _set_list_message(self, text: str) -> None:
        """Replace the list with a single informational row."""
        for child in self._ports_list.get_children():
            self._ports_list.remove(child)
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        row.add(padded(label(text, "xposer-dim"), SPACING_S))
        self._ports_list.add(row)
        self._ports_list.show_all()

    def _populate_ports(self, entries: list[ListeningPort]) -> None:
        """Fill the list with scanned entries (main thread)."""
        if self._destroyed:
            return
        if not entries:
            self._set_list_message("No running servers found. Start your app, then scan again.")
            return
        for child in self._ports_list.get_children():
            self._ports_list.remove(child)
        shared = self._shared_ports()
        self._labels = {entry.port: entry.display_name for entry in entries}
        for entry in sorted(entries, key=_listing_rank)[:MAX_LISTED_PORTS]:
            row = Gtk.ListBoxRow()
            row.port = entry.port
            row.add(self._make_port_row(entry, entry.port in shared))
            self._ports_list.add(row)
        self._ports_list.show_all()
        if self._name_is_auto:
            self._auto_fill_name()

    def _shared_ports(self) -> set[int]:
        """Local ports already shared (tolerates minimal managers)."""
        query = getattr(self._service_manager, "shared_local_ports", None)
        return query() if callable(query) else set()

    @staticmethod
    def _make_port_row(entry: ListeningPort, already_shared: bool) -> Gtk.Widget:
        """Port number, app name and where it listens."""
        box = padded(Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_M), SPACING_S)
        port_label = label(str(entry.port), "xposer-mono", "xposer-title")
        port_label.set_width_chars(6)
        box.pack_start(port_label, False, False, 0)
        name = label(entry.display_name, ellipsize=Pango.EllipsizeMode.END)
        name.set_hexpand(True)
        box.pack_start(name, True, True, 0)
        if already_shared:
            note = "shared"
        elif entry.local_only:
            note = "this computer only"
        else:
            note = "on network"
        box.pack_end(label(note, "xposer-dim", "xposer-small"), False, False, 0)
        return box

    def _on_port_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        """Copy the picked port into the form."""
        port = getattr(row, "port", None)
        if port is not None:
            self._port_spin.set_value(port)
            if self._name_is_auto:
                self._auto_fill_name()
            self._schedule_probe()

    # ---- name ----

    def _on_name_changed(self, entry: Gtk.Entry) -> None:
        """Stop auto-filling once the user types a name of their own."""
        if not self._filling_name:
            self._name_is_auto = not entry.get_text().strip()

    def _auto_fill_name(self) -> None:
        """Suggest a name from the detected app, e.g. 'Vite (5173)'."""
        port = int(self._port_spin.get_value())
        detected = self._labels.get(port)
        if detected:
            self._filling_name = True
            self._name_entry.set_text(f"{detected.split(' · ')[0]} ({port})")
            self._filling_name = False

    def _default_name(self, port: int) -> str:
        """Name used when the field is left empty."""
        detected = self._labels.get(port)
        return f"{detected.split(' · ')[0]} ({port})" if detected else f"Port {port}"

    # ---- protocol probe ----

    def _schedule_probe(self) -> None:
        """Debounce probing while the port is being edited."""
        if self._probe_source is not None:
            GLib.source_remove(self._probe_source)
        self._probe_source = GLib.timeout_add(PROBE_DEBOUNCE_MILLISECONDS, self._start_probe)

    def _start_probe(self) -> bool:
        """Probe the selected port off-thread."""
        self._probe_source = None
        if self._destroyed:
            return GLib.SOURCE_REMOVE
        self._probe_generation += 1
        generation = self._probe_generation
        port = int(self._port_spin.get_value())
        self._probe = None
        self._probe_spinner.start()
        self._probe_label.set_text(f"Checking port {port}…")
        self._scheme_box.set_visible(False)
        run_off_thread(
            "xposer-probe",
            lambda: self._service_manager.probe_port(port),
            lambda probe: self._probe_finished(generation, probe),
            lambda exc: self._probe_failed(generation, exc),
        )
        return GLib.SOURCE_REMOVE

    def _probe_finished(self, generation: int, probe: UpstreamProbe) -> None:
        """Show what the port speaks and offer the scheme choice for HTTPS."""
        if self._destroyed or generation != self._probe_generation:
            return
        self._probe_spinner.stop()
        self._probe = probe
        context = self._probe_label.get_style_context()
        for css in ("xposer-success-text", "xposer-warning-text"):
            context.remove_class(css)
        if not probe.reachable:
            context.add_class("xposer-warning-text")
            self._probe_label.set_text(
                f"Nothing is running on port {probe.port} yet. Start your app first."
            )
            return
        context.add_class("xposer-success-text")
        if probe.kind == UpstreamKind.TLS:
            self._probe_label.set_text("HTTPS app found. Choose how devices open it:")
            self._scheme_box.set_visible(True)
        elif probe.kind.is_raw:
            self._probe_label.set_text("Non-web service found — it will be relayed as raw TCP.")
        else:
            where = "IPv6 localhost" if probe.host == "::1" else "localhost"
            self._probe_label.set_text(f"Web app found on {where}. Ready to share.")

    def _probe_failed(self, generation: int, exc: Exception) -> None:
        """Hide probe results when probing is impossible."""
        if self._destroyed or generation != self._probe_generation:
            return
        self._probe_spinner.stop()
        self._probe_label.set_text("")
        logger.debug(f"Probe failed: {exc}")

    # ---- submit ----

    def _on_expose_clicked(self, _button: Gtk.Button | None) -> None:
        """Validate the form and expose the port on a worker thread."""
        port = int(self._port_spin.get_value())
        name = self._name_entry.get_text().strip() or self._default_name(port)
        hostname = self._hostname_entry.get_text().strip() or None
        scheme = (
            ShareScheme.HTTPS
            if self._https_radio.get_active() and self._scheme_box.get_visible()
            else None
        )
        self._set_busy(True)

        def work() -> tuple[Service, bool]:
            service = self._service_manager.expose_port(name, port, hostname, share_scheme=scheme)
            return service, self._service_manager.lan_reachable(service)

        run_off_thread("xposer-expose", work, self._expose_succeeded, self._lifecycle_failed)

    def _set_busy(self, busy: bool) -> None:
        """Toggle the action button and spinner while work is in flight."""
        self._expose_button.set_sensitive(not busy)
        if busy:
            self._busy_spinner.start()
        else:
            self._busy_spinner.stop()

    def _lifecycle_failed(self, exc: Exception) -> None:
        """Report a failed expose attempt and re-enable the form."""
        if self._destroyed:
            return
        self._set_busy(False)
        show_error(self, error_message(exc))

    def _expose_succeeded(self, result: tuple[Service, bool]) -> None:
        """Close the form and show the new service's QR code."""
        if self._destroyed:
            return
        service, reachable = result
        self.destroy()
        if self._window_manager is not None:
            self._window_manager.open_qr_window(service)
        if not reachable:
            show_warning(None, UNREACHABLE_WARNING_TITLE, UNREACHABLE_HINT)


def _listing_rank(entry: ListeningPort) -> tuple[int, int]:
    """Recognised dev servers first, other user apps next, system daemons last."""
    if entry.app_label:
        group = 0
    elif entry.process_name != UNKNOWN_PROCESS and entry.port >= FIRST_UNPRIVILEGED_PORT:
        group = 1
    else:
        group = 2
    return group, entry.port

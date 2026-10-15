"""Dashboard window: every shared service with its link and quick controls.

Opened from the tray, or used as the main window on desktops without a
tray. Rows are updated in place on each refresh (traffic counters change
every few seconds), so nothing is rebuilt under the user's pointer.
Engine calls run off-thread via ui_tasks; UI work stays on the main loop.
"""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gtk, Pango

from models.service import Service, ServiceStatus, ServiceType
from network.firewall import allow_through_windows_firewall, firewall_help
from network.network_detector import NetworkDetector
from services.service_manager import ServiceManager
from utils.logger import get_logger
from utils.ui_tasks import run_off_thread
from windows.dialogs import FIREWALL_RULE_FAILED_TEXT, confirm, show_error
from windows.theme import (
    CARD_PADDING,
    DASHBOARD_HEIGHT,
    DASHBOARD_WIDTH,
    SPACING_M,
    SPACING_S,
    SPACING_XS,
    WINDOW_MARGIN,
    add_classes,
    padded,
)
from windows.widgets import (
    CopyButton,
    EmptyState,
    SchemeBadge,
    StatusPill,
    copy_to_clipboard,
    error_message,
    firewall_help_popover,
    icon_button,
    label,
    open_link,
    source_text,
    traffic_text,
    url_label,
)

APP_HEADING = "Xposer"
EMPTY_TITLE = "Nothing shared yet"
EMPTY_TEXT = "Share a running dev server or a folder with phones and laptops on your Wi-Fi."
NO_TRAY_NOTE = "No tray icon on this desktop — closing this window quits Xposer."
START_TOOLTIP = "Start sharing"
STOP_TOOLTIP = "Stop sharing"

logger = get_logger("windows")


class StatusPanel(Gtk.Window):
    """Dashboard listing every service with quick controls."""

    def __init__(
        self,
        service_manager: ServiceManager,
        detector: NetworkDetector | None,
        window_manager: object,
        app_version: str,
        quit_on_close: bool = False,
    ) -> None:
        super().__init__(title=APP_HEADING)
        self._service_manager = service_manager
        self._detector = detector
        self._window_manager = window_manager
        self._app_version = app_version
        self._quit_on_close = quit_on_close
        self._rows: dict[str, ServiceRow] = {}
        self._destroyed = False
        self.set_default_size(DASHBOARD_WIDTH, DASHBOARD_HEIGHT)
        self.connect("destroy", self._on_destroyed)
        self.connect("key-press-event", self._on_key_pressed)
        self._build_ui()
        self.refresh()

    # ---- construction ----

    def _build_ui(self) -> None:
        """Header bar, scrollable service list, footer with address and help."""
        self._header = Gtk.HeaderBar(title=APP_HEADING, show_close_button=True)
        self._header.pack_start(self._make_share_button())
        self._header.pack_end(self._make_main_menu())
        self._header.pack_end(
            icon_button("preferences-system-symbolic", "Settings", self._on_settings_clicked)
        )
        self.set_titlebar(self._header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_vexpand(True)
        outer.pack_start(scrolled, True, True, 0)
        content = padded(
            Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACING_M), WINDOW_MARGIN
        )
        scrolled.add(content)

        self._empty = EmptyState("network-wireless-symbolic", EMPTY_TITLE, EMPTY_TEXT)
        self._empty.set_vexpand(True)
        self._empty.set_no_show_all(True)
        self._empty_label = self._empty.title_label
        expose = Gtk.Button(label="Share a running app")
        add_classes(expose, "suggested-action")
        expose.connect("clicked", self._on_expose_clicked)
        serve = Gtk.Button(label="Share a folder")
        serve.connect("clicked", self._on_serve_clicked)
        self._empty.actions.pack_start(expose, False, False, 0)
        self._empty.actions.pack_start(serve, False, False, 0)
        for child in self._empty.get_children():
            child.show_all()
        content.pack_start(self._empty, True, True, 0)

        self._rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACING_M)
        content.pack_start(self._rows_box, False, False, 0)

        outer.pack_start(Gtk.Separator(), False, False, 0)
        footer = padded(
            Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_S), SPACING_S
        )
        footer.set_margin_start(WINDOW_MARGIN)
        self._ip_label = label("", "xposer-dim", "xposer-small")
        footer.pack_start(self._ip_label, True, True, 0)
        help_button = Gtk.Button(label="Can't connect?")
        help_button.set_relief(Gtk.ReliefStyle.NONE)
        help_button.connect("clicked", self._on_help_clicked)
        footer.pack_end(help_button, False, False, 0)
        outer.pack_start(footer, False, False, 0)
        if self._quit_on_close:
            note = label(NO_TRAY_NOTE, "xposer-dim", "xposer-small", wrap=True)
            note.set_margin_start(WINDOW_MARGIN)
            note.set_margin_bottom(SPACING_S)
            outer.pack_start(note, False, False, 0)
        self.show_all()

    def _make_share_button(self) -> Gtk.MenuButton:
        """'+' button offering the two ways to share."""
        button = Gtk.MenuButton()
        button.set_image(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON))
        button.set_tooltip_text("Share something")
        button.get_accessible().set_name("Share something")
        popover = Gtk.Popover.new(button)
        box = padded(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), SPACING_S)
        for text, handler in (
            ("Share a running app…", self._on_expose_clicked),
            ("Share a folder…", self._on_serve_clicked),
        ):
            item = Gtk.ModelButton(text=text)
            item.connect("clicked", handler)
            box.pack_start(item, False, False, 0)
        box.show_all()
        popover.add(box)
        button.set_popover(popover)
        return button

    def _make_main_menu(self) -> Gtk.MenuButton:
        """Hamburger menu: QR gallery, About, Quit."""
        button = Gtk.MenuButton()
        button.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        button.set_tooltip_text("Menu")
        button.get_accessible().set_name("Menu")
        popover = Gtk.Popover.new(button)
        box = padded(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), SPACING_S)
        for text, handler in (
            ("All QR codes", self._on_gallery_clicked),
            ("About Xposer", self._on_about_clicked),
            ("Quit", self._on_quit_clicked),
        ):
            item = Gtk.ModelButton(text=text)
            item.connect("clicked", handler)
            box.pack_start(item, False, False, 0)
        box.show_all()
        popover.add(box)
        button.set_popover(popover)
        return button

    # ---- refresh ----

    def refresh(self) -> None:
        """Sync rows with manager state, updating existing rows in place."""
        if self._destroyed:
            return
        services = self._service_manager.all_services()
        self._ip_label.set_text(f"Your address: {self._service_manager.cached_lan_ip()}")
        running = [s for s in services if s.status != ServiceStatus.STOPPED]
        devices = sum(s.connected_devices for s in running)
        subtitle = f"{len(running)} shared" if running else "Nothing shared"
        if devices:
            subtitle += f" · {devices} device{'s' if devices != 1 else ''}"
        self._header.set_subtitle(subtitle)
        seen: set[str] = set()
        for index, service in enumerate(services):
            row = self._rows.get(service.id)
            if row is None:
                row = ServiceRow(self, service)
                self._rows[service.id] = row
                self._rows_box.pack_start(row, False, False, 0)
            row.update(service)
            self._rows_box.reorder_child(row, index)
            seen.add(service.id)
        for service_id in [sid for sid in self._rows if sid not in seen]:
            self._rows.pop(service_id).destroy()
        self._empty.set_visible(not services)

    # ---- service actions (shared by every row) ----

    def toggle_service(self, service: Service, turn_on: bool) -> None:
        """Start or stop a service off-thread; errors are shown to the user."""
        service_id = service.id

        def work() -> None:
            if turn_on:
                self._service_manager.restart_service(service_id)
            else:
                self._service_manager.stop_service(service_id)

        run_off_thread(f"xposer-panel-{service_id[:8]}", work, None, self._operation_failed)

    def share_backend(self, service: Service, port: int) -> None:
        """Share a backend the service calls, under the same custom link."""
        service_id = service.id
        run_off_thread(
            f"xposer-backend-{port}",
            lambda: self._service_manager.share_backend(service_id, port),
            None,
            self._operation_failed,
        )

    def remove_service(self, service: Service) -> None:
        """Stop and forget a service after confirmation."""
        if not confirm(
            self, f"Remove '{service.name}'?", "Its link stops working and it leaves this list."
        ):
            return
        service_id = service.id
        run_off_thread(
            f"xposer-remove-{service_id[:8]}",
            lambda: self._service_manager.remove_service(service_id),
            None,
            self._operation_failed,
        )

    def show_qr(self, service: Service) -> None:
        """Open the QR window for one service."""
        if self._window_manager is not None:
            self._window_manager.open_qr_window(service)

    def _operation_failed(self, exc: Exception) -> None:
        """Report a failed operation and resync the rows."""
        if self._destroyed:
            return
        self.refresh()
        show_error(self, error_message(exc))

    # ---- handlers ----

    def _on_destroyed(self, _widget: Gtk.Widget) -> None:
        """Mark dead; in window mode closing the dashboard quits the app."""
        self._destroyed = True
        if self._quit_on_close:
            Gtk.main_quit()

    def _on_key_pressed(self, _window: Gtk.Window, event: Gdk.EventKey) -> bool:
        """Escape closes the dashboard when a tray icon can reopen it."""
        if event.keyval == Gdk.KEY_Escape and not self._quit_on_close:
            self.destroy()
            return True
        return False

    def _on_help_clicked(self, button: Gtk.Button) -> None:
        """Explain firewall and Wi-Fi requirements."""
        popover = firewall_help_popover(button, firewall_help(), self._allow_firewall)
        popover.popup()

    def _allow_firewall(self) -> None:
        """Ask Windows to allow inbound connections (UAC prompt)."""
        if not allow_through_windows_firewall():
            show_error(
                self,
                FIREWALL_RULE_FAILED_TEXT,
            )

    def _on_expose_clicked(self, _widget: Gtk.Widget) -> None:
        """Open the Expose Port window."""
        if self._window_manager is not None:
            self._window_manager.show_expose_port()

    def _on_serve_clicked(self, _widget: Gtk.Widget) -> None:
        """Open the Serve Folder window."""
        if self._window_manager is not None:
            self._window_manager.show_serve_folder()

    def _on_settings_clicked(self, _widget: Gtk.Widget) -> None:
        """Open the Settings window."""
        if self._window_manager is not None:
            self._window_manager.show_settings()

    def _on_gallery_clicked(self, _widget: Gtk.Widget) -> None:
        """Open the All QR Codes window."""
        if self._window_manager is not None:
            self._window_manager.show_all_qr()

    def _on_about_clicked(self, _widget: Gtk.Widget) -> None:
        """Show the standard About dialog."""
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name(APP_HEADING)
        dialog.set_version(self._app_version)
        dialog.set_comments("Share your local apps and folders with devices on your Wi-Fi.")
        dialog.set_logo_icon_name("network-wireless-symbolic")
        dialog.run()
        dialog.destroy()

    def _on_quit_clicked(self, _widget: Gtk.Widget) -> None:
        """Leave the GTK main loop; main.py's exit path shuts engines down."""
        Gtk.main_quit()


class ServiceRow(Gtk.Box):
    """One service card: status, name, link and quick actions."""

    def __init__(self, panel: StatusPanel, service: Service) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._panel = panel
        self._service = service
        add_classes(self, "xposer-card")
        inner = padded(
            Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACING_S), CARD_PADDING
        )
        self.pack_start(inner, True, True, 0)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_S)
        inner.pack_start(top, False, False, 0)
        self._pill = StatusPill()
        top.pack_start(self._pill, False, False, 0)
        self._name_label = label("", "xposer-title", ellipsize=Pango.EllipsizeMode.END)
        self._name_label.set_hexpand(True)
        top.pack_start(self._name_label, True, True, 0)
        self._badge = SchemeBadge()
        self._badge.set_no_show_all(True)
        top.pack_start(self._badge, False, False, 0)
        self._qr_button = icon_button(
            "view-grid-symbolic", "Show QR code", lambda _b: panel.show_qr(self._service)
        )
        top.pack_start(self._qr_button, False, False, 0)
        self._toggle_button = icon_button(
            "media-playback-start-symbolic", START_TOOLTIP, self._on_toggle_clicked
        )
        top.pack_start(self._toggle_button, False, False, 0)
        top.pack_start(self._make_more_menu(), False, False, 0)

        link_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_XS)
        inner.pack_start(link_row, False, False, 0)
        self._url_label = url_label()
        link_row.pack_start(self._url_label, True, True, 0)
        self._copy_button = CopyButton()
        link_row.pack_start(self._copy_button, False, False, 0)
        self._open_button = icon_button(
            "web-browser-symbolic", "Open in browser", self._on_open_clicked
        )
        link_row.pack_start(self._open_button, False, False, 0)

        self._detail_label = label("", "xposer-dim", "xposer-small", wrap=True)
        inner.pack_start(self._detail_label, False, False, 0)

        self._note_label = label("", "xposer-small", "xposer-warning-text", wrap=True)
        self._note_label.set_no_show_all(True)
        inner.pack_start(self._note_label, False, False, 0)

        self._hint_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_S)
        add_classes(self._hint_box, "xposer-hint")
        self._hint_box.set_no_show_all(True)
        hint_icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic", Gtk.IconSize.BUTTON)
        hint_icon.set_margin_start(SPACING_S)
        self._hint_box.pack_start(hint_icon, False, False, 0)
        self._hint_label = label("", "xposer-small", wrap=True)
        self._hint_label.set_margin_top(SPACING_S)
        self._hint_label.set_margin_bottom(SPACING_S)
        self._hint_box.pack_start(self._hint_label, True, True, 0)
        self._hint_button = Gtk.Button(label="Share it too")
        self._hint_button.set_valign(Gtk.Align.CENTER)
        self._hint_button.set_margin_end(SPACING_S)
        self._hint_button.connect("clicked", self._on_hint_clicked)
        self._hint_box.pack_start(self._hint_button, False, False, 0)
        for child in self._hint_box.get_children():
            child.show()
        inner.pack_start(self._hint_box, False, False, 0)
        self.show_all()

    def _make_more_menu(self) -> Gtk.MenuButton:
        """Per-service overflow menu."""
        button = Gtk.MenuButton()
        button.set_image(Gtk.Image.new_from_icon_name("view-more-symbolic", Gtk.IconSize.BUTTON))
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text("More")
        button.get_accessible().set_name("More actions")
        popover = Gtk.Popover.new(button)
        box = padded(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), SPACING_S)
        self._copy_ip_item = Gtk.ModelButton(text="Copy IP link")
        self._copy_ip_item.connect("clicked", self._on_copy_ip_clicked)
        box.pack_start(self._copy_ip_item, False, False, 0)
        remove = Gtk.ModelButton(text="Remove from list")
        remove.connect("clicked", lambda _b: self._panel.remove_service(self._service))
        box.pack_start(remove, False, False, 0)
        box.show_all()
        popover.add(box)
        button.set_popover(popover)
        return button

    def update(self, service: Service) -> None:
        """Reflect service state in every widget (cheap; no rebuild)."""
        self._service = service
        running = service.status == ServiceStatus.RUNNING
        live = service.status != ServiceStatus.STOPPED
        context = self.get_style_context()
        if live:
            context.remove_class("xposer-stopped")
        else:
            context.add_class("xposer-stopped")
        self._pill.set_status(service.status)
        self._name_label.set_text(service.name)
        self._badge.set_visible(service.type == ServiceType.PORT and live)
        self._badge.set_service(service)
        self._url_label.set_text(service.lan_url or "Not shared right now")
        self._url_label.set_sensitive(live)
        self._copy_button.set_text(service.lan_url if live else "")
        self._open_button.set_sensitive(live and bool(service.lan_url))
        self._qr_button.set_sensitive(live and self._panel._window_manager is not None)
        self._copy_ip_item.set_sensitive(live and service.ip_url != service.lan_url)
        icon, tooltip = (
            ("media-playback-stop-symbolic", STOP_TOOLTIP)
            if live
            else ("media-playback-start-symbolic", START_TOOLTIP)
        )
        image = self._toggle_button.get_image()
        if isinstance(image, Gtk.Image):
            image.set_from_icon_name(icon, Gtk.IconSize.BUTTON)
        self._toggle_button.set_tooltip_text(tooltip)
        self._toggle_button.get_accessible().set_name(tooltip)
        self._update_detail(service, running)
        self._update_note(service, live)
        self._update_hint(service, live)

    def _update_detail(self, service: Service, running: bool) -> None:
        """Source and traffic, or the reason something is wrong."""
        context = self._detail_label.get_style_context()
        problem = service.last_error if service.status != ServiceStatus.RUNNING else ""
        if problem:
            context.add_class("xposer-error-text")
            self._detail_label.set_text(problem)
            return
        context.remove_class("xposer-error-text")
        state = traffic_text(service) if running else "stopped"
        self._detail_label.set_text(f"{source_text(service)} · {state}")

    def _update_note(self, service: Service, live: bool) -> None:
        """Explain a custom link that could not be published."""
        show = live and service.hostname is not None and not service.hostname_active
        self._note_label.set_visible(show)
        if show:
            self._note_label.set_text(
                f"Custom link {service.hostname} isn't available — sharing the IP link instead."
            )

    def _update_hint(self, service: Service, live: bool) -> None:
        """Offer to share a backend the app calls on localhost."""
        hints = service.backend_hints if live else ()
        self._hint_box.set_visible(bool(hints))
        self._hint_button.set_sensitive(True)
        if hints:
            hint = hints[0]
            self._hint_label.set_text(
                f"This app calls {hint.description} on this computer. "
                "Share it too so phones can reach it, then reload."
            )

    def _on_toggle_clicked(self, _button: Gtk.Button) -> None:
        """Stop a live service or start a stopped one."""
        self._panel.toggle_service(self._service, self._service.status == ServiceStatus.STOPPED)

    def _on_open_clicked(self, _button: Gtk.Button) -> None:
        """Open the link locally (quick check that it works)."""
        open_link(self._panel, self._service.lan_url)

    def _on_copy_ip_clicked(self, _button: Gtk.Button) -> None:
        """Copy the IP form of the link."""
        copy_to_clipboard(self._service.ip_url)

    def _on_hint_clicked(self, _button: Gtk.Button) -> None:
        """Share the first suggested backend."""
        if self._service.backend_hints:
            self._hint_button.set_sensitive(False)
            self._panel.share_backend(self._service, self._service.backend_hints[0].port)

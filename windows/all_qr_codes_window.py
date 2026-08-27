"""Grid window showing every active service's QR code at once."""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

from models.service import Service
from network.qr_generator import QRGenerator
from services.service_manager import ServiceManager
from utils.logger import get_logger

logger = get_logger("windows")

CARD_QR_SIZE_PX = 160


class AllQRCodesWindow(Gtk.Window):
    """Responsive grid of cards for all active services."""

    def __init__(
        self, service_manager: ServiceManager, qr_generator: QRGenerator
    ) -> None:
        super().__init__(title="All Active Services")
        self._service_manager = service_manager
        self._qr_generator = qr_generator
        self._pending_page: str | None = None
        self.set_default_size(600, 500)
        self._build_ui()
        self._stack.connect("map", self._on_stack_mapped)
        self.refresh()

    def _build_ui(self) -> None:
        header = Gtk.HeaderBar(title="All Active Services", show_close_button=True)
        header.set_subtitle("Everything currently shared on this WiFi")
        self.set_titlebar(header)

        scrolled = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN)
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        self.add(scrolled)

        self._stack = Gtk.Stack()
        scrolled.add(self._stack)

        empty = Gtk.Label(
            label="No active services.\nUse the tray menu to expose a port "
            "or serve a folder."
        )
        empty.set_justify(Gtk.Justification.CENTER)
        self._stack.add_named(empty, "empty")

        flow_scrolled = Gtk.ScrolledWindow()
        self._flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            margin=12,
            column_spacing=12,
            row_spacing=12,
            min_children_per_line=1,
            max_children_per_line=4,
        )
        flow_scrolled.add(self._flow)
        self._stack.add_named(flow_scrolled, "grid")

        self.show_all()

    def refresh(self) -> None:
        """Rebuild the card grid from current running services."""
        while self._flow.get_child_at_index(0) is not None:
            child = self._flow.get_child_at_index(0)
            self._flow.remove(child)
        services = self._service_manager.active_services()
        for service in services:
            self._flow.add(self._make_card(service))
        self._flow.show_all()
        page = "grid" if services else "empty"
        if self._stack.get_mapped():
            self._stack.set_visible_child_name(page)
            self._pending_page = None
        else:
            self._pending_page = page

    def _on_stack_mapped(self, _stack: Gtk.Stack) -> None:
        """Apply the pending page once GTK actually maps the stack."""
        if self._pending_page is not None:
            self._stack.set_visible_child_name(self._pending_page)
            self._pending_page = None

    def _make_card(self, service: Service) -> Gtk.Widget:
        """Build one card: name, QR image, URL, and a copy button."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        card.get_style_context().add_class("xposer-card")
        card.set_halign(Gtk.Align.FILL)

        name = Gtk.Label(label=service.name)
        name.get_style_context().add_class("heading")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(22)
        card.pack_start(name, False, False, 0)

        self._pack_qr(card, service.qr_image_path)

        url = Gtk.Label(label=service.lan_url)
        url.get_style_context().add_class("xposer-mono")
        url.set_selectable(True)
        url.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        url.set_max_width_chars(24)
        card.pack_start(url, False, False, 0)

        copy = Gtk.Button(label="Copy Link")
        copy.connect("clicked", self._on_copy_clicked, service.lan_url)
        copy.set_halign(Gtk.Align.CENTER)
        card.pack_start(copy, False, False, 0)
        return card

    @staticmethod
    def _pack_qr(card: Gtk.Box, qr_image_path: str | None) -> None:
        """Add the card's QR image, or nothing when it cannot be loaded."""
        if not qr_image_path:
            return
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
                qr_image_path, CARD_QR_SIZE_PX, CARD_QR_SIZE_PX
            )
        except (OSError, GLib.Error) as exc:
            logger.debug(f"QR image unavailable: {exc}")
            return
        card.pack_start(Gtk.Image.new_from_pixbuf(pixbuf), False, False, 0)

    @staticmethod
    def _on_copy_clicked(_button: Gtk.Button, url: str) -> None:
        """Put this card's LAN URL on the clipboard."""
        clipboard = Gtk.Clipboard.get_default(Gdk.Display.get_default())
        clipboard.set_text(url, -1)
        _button.set_label("Copied!")

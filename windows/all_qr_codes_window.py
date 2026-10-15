"""Grid window showing every active service's QR code at once.

The grid is rebuilt only when something visible changes (a service's
name, link or QR image); traffic-counter updates do not redraw it.
"""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Pango

from models.service import Service
from network.qr_generator import QRGenerator
from services.service_manager import ServiceManager
from windows.theme import QR_CARD_SIZE, SPACING_M, SPACING_S, add_classes, padded
from windows.widgets import CopyButton, EmptyState, label, load_pixbuf

GALLERY_WIDTH = 640
GALLERY_HEIGHT = 520
MAX_CARDS_PER_LINE = 4


class AllQRCodesWindow(Gtk.Window):
    """Responsive grid of cards for all active services."""

    def __init__(self, service_manager: ServiceManager, qr_generator: QRGenerator) -> None:
        super().__init__(title="All shared links")
        self._service_manager = service_manager
        self._qr_generator = qr_generator
        self._pending_page: str | None = None
        self._signature: tuple[tuple[str, str, str | None, str], ...] | None = None
        self.set_default_size(GALLERY_WIDTH, GALLERY_HEIGHT)
        self._build_ui()
        self._stack.connect("map", self._on_stack_mapped)
        self.refresh()

    def _build_ui(self) -> None:
        """Header, empty state and the card grid."""
        header = Gtk.HeaderBar(title="All shared links", show_close_button=True)
        header.set_subtitle("Everything currently shared on this Wi-Fi")
        self.set_titlebar(header)

        self._stack = Gtk.Stack()
        self.add(self._stack)
        empty = EmptyState(
            "view-grid-symbolic",
            "No active links",
            "Share a running app or a folder and its QR code appears here.",
        )
        self._stack.add_named(empty, "empty")

        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            column_spacing=SPACING_M,
            row_spacing=SPACING_M,
            min_children_per_line=1,
            max_children_per_line=MAX_CARDS_PER_LINE,
            homogeneous=True,
        )
        padded(self._flow, SPACING_M)
        self._flow.set_valign(Gtk.Align.START)
        scrolled.add(self._flow)
        self._stack.add_named(scrolled, "grid")
        self.show_all()

    def refresh(self) -> None:
        """Rebuild the card grid when visible content changed."""
        services = self._service_manager.active_services()
        signature = tuple((s.id, s.name, s.qr_image_path, s.ip_url or s.lan_url) for s in services)
        if signature != self._signature:
            self._signature = signature
            for child in self._flow.get_children():
                self._flow.remove(child)
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

    @staticmethod
    def _make_card(service: Service) -> Gtk.Widget:
        """One card: name, QR image (IP link), link and copy button."""
        link = service.ip_url or service.lan_url
        card = padded(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACING_S), SPACING_M)
        frame = Gtk.EventBox()
        add_classes(frame, "xposer-card")
        frame.add(card)
        name = label(service.name, "xposer-title", xalign=0.5, ellipsize=Pango.EllipsizeMode.END)
        card.pack_start(name, False, False, 0)
        pixbuf = load_pixbuf(service.qr_image_path, QR_CARD_SIZE)
        if pixbuf is not None:
            qr_box = Gtk.EventBox()
            add_classes(qr_box, "xposer-qr-frame")
            qr_box.set_halign(Gtk.Align.CENTER)
            qr_box.add(padded(Gtk.Image.new_from_pixbuf(pixbuf), SPACING_S))
            card.pack_start(qr_box, False, False, 0)
        link_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_S)
        url = label(
            link, "xposer-mono", "xposer-small", xalign=0.5, ellipsize=Pango.EllipsizeMode.MIDDLE
        )
        url.set_selectable(True)
        url.set_hexpand(True)
        link_row.pack_start(url, True, True, 0)
        copy = CopyButton()
        copy.set_text(link)
        link_row.pack_start(copy, False, False, 0)
        card.pack_start(link_row, False, False, 0)
        return frame

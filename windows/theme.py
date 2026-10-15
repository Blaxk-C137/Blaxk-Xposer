"""Central look-and-feel: spacing constants and the single CSS provider.

Colours derive from the active GTK theme (@theme_fg_color and friends) so
light and dark themes both work; only status accents are fixed.
"""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk

SPACING_XS = 4
SPACING_S = 6
SPACING_M = 12
SPACING_L = 18
SPACING_XL = 24
CARD_PADDING = 12
WINDOW_MARGIN = 18
DASHBOARD_WIDTH = 460
DASHBOARD_HEIGHT = 560
FORM_WIDTH = 480
QR_DISPLAY_SIZE = 260
QR_CARD_SIZE = 168
EMPTY_ICON_SIZE = 64

CSS_DATA = b"""
@define-color xposer_success #26a269;
@define-color xposer_warning #c88800;
@define-color xposer_error #c01c28;
@define-color xposer_accent #3584e4;

.xposer-mono { font-family: monospace; }
.xposer-dim { opacity: 0.68; }
.xposer-small { font-size: smaller; }
.xposer-title { font-weight: bold; }
.xposer-heading { font-weight: bold; font-size: larger; }

.xposer-card {
    border-radius: 12px;
    border: 1px solid alpha(@theme_fg_color, 0.12);
    background-color: @theme_base_color;
}
.xposer-card.xposer-stopped { background-color: alpha(@theme_base_color, 0.6); }

.xposer-pill {
    border-radius: 999px;
    padding: 1px 8px;
    font-size: smaller;
    font-weight: bold;
}
.xposer-pill-running { background-color: alpha(@xposer_success, 0.16); color: @xposer_success; }
.xposer-pill-stopped {
    background-color: alpha(@theme_fg_color, 0.08);
    color: alpha(@theme_fg_color, 0.7);
}
.xposer-pill-error { background-color: alpha(@xposer_error, 0.14); color: @xposer_error; }

.xposer-badge {
    border-radius: 6px;
    padding: 0 6px;
    font-size: smaller;
    font-family: monospace;
    border: 1px solid alpha(@theme_fg_color, 0.22);
}

.xposer-url {
    font-family: monospace;
    padding: 4px 8px;
    border-radius: 8px;
    background-color: alpha(@theme_fg_color, 0.06);
}

.xposer-hint {
    border-radius: 8px;
    background-color: alpha(@xposer_accent, 0.10);
}
.xposer-warning-text { color: @xposer_warning; }
.xposer-error-text { color: @xposer_error; }
.xposer-success-text { color: @xposer_success; }

.xposer-qr-frame {
    background-color: #ffffff;
    border-radius: 12px;
    border: 1px solid alpha(@theme_fg_color, 0.12);
}
"""

_installed = False


def install_css() -> None:
    """Register the application-wide CSS provider once per process."""
    global _installed
    if _installed:
        return
    screen = Gdk.Screen.get_default()
    if screen is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS_DATA)
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _installed = True


def add_classes(widget: Gtk.Widget, *classes: str) -> Gtk.Widget:
    """Attach CSS classes and return the widget (for inline construction)."""
    context = widget.get_style_context()
    for css_class in classes:
        context.add_class(css_class)
    return widget


def padded(widget: Gtk.Widget, padding: int) -> Gtk.Widget:
    """Set equal margins on every side and return the widget."""
    widget.set_margin_top(padding)
    widget.set_margin_bottom(padding)
    widget.set_margin_start(padding)
    widget.set_margin_end(padding)
    return widget

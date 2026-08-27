"""Reusable GTK dialog helpers shared by all Xposer windows."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

FIREWALL_WARNING_TITLE = "Possibly blocked by firewall"
FIREWALL_HINT = (
    "The service is running, but nothing answered on your LAN IP. A firewall "
    "is probably blocking it. Allow inbound TCP traffic for the port in your "
    "firewall settings (e.g. 'firewall-config' or 'sudo ufw allow <port>')."
)


def show_error(parent: Gtk.Window | None, message: str) -> None:
    """Show a modal error dialog."""
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.OK,
        text=message,
    )
    dialog.run()
    dialog.destroy()


def show_warning(parent: Gtk.Window | None, title: str, message: str) -> None:
    """Show a modal warning dialog."""
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        message_type=Gtk.MessageType.WARNING,
        buttons=Gtk.ButtonsType.OK,
        text=title,
    )
    dialog.format_secondary_text(message)
    dialog.run()
    dialog.destroy()


def confirm(parent: Gtk.Window | None, title: str, message: str) -> bool:
    """Ask a yes/no question; True only when the user answers Yes."""
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.YES_NO,
        text=title,
    )
    dialog.format_secondary_text(message)
    response = dialog.run()
    dialog.destroy()
    return response == Gtk.ResponseType.YES

"""Reusable GTK dialog helpers shared by all Xposer windows."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

FIREWALL_RULE_FAILED_TEXT = (
    "Windows did not add the firewall rule. You can allow Python manually in Windows Security."
)
UNREACHABLE_WARNING_TITLE = "Shared, but not answering on your network address"
UNREACHABLE_HINT = (
    "Xposer started the link, but a test connection to your network address "
    "failed. If you use a VPN or several networks, pick the right network "
    "interface in Settings. Use “Can't connect?” in the dashboard for firewall help."
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

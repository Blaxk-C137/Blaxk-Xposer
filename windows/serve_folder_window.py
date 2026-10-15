"""The 'Share a folder' window: serve a directory over HTTP."""

from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from models.service import FolderOptions, Service
from services.service_manager import ServiceManager
from utils.ui_tasks import run_off_thread
from windows.dialogs import (
    UNREACHABLE_HINT,
    UNREACHABLE_WARNING_TITLE,
    confirm,
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

HOME_WARNING_TEXT = (
    "This folder contains personal or system files. Everything inside it "
    "will be visible to everyone on this Wi-Fi network."
)
SPA_LABEL = "Single-page app: answer unknown paths with index.html"
HIDDEN_LABEL = "Include hidden files (.env, .git, …)"


class ServeFolderWindow(Gtk.Window):
    """Form serving a local folder as a static HTTP site."""

    def __init__(
        self,
        service_manager: ServiceManager,
        window_manager: object | None = None,
    ) -> None:
        super().__init__(title="Share a folder")
        self._service_manager = service_manager
        self._window_manager = window_manager
        self._destroyed = False
        self.set_default_size(FORM_WIDTH, -1)
        self.connect("destroy", self._on_destroyed)
        self._build_ui()

    def _on_destroyed(self, _widget: Gtk.Widget) -> None:
        """Mark the window dead so queued callbacks ignore it."""
        self._destroyed = True

    def _build_ui(self) -> None:
        """Form, options and actions."""
        header = Gtk.HeaderBar(
            title="Share a folder",
            subtitle="Serve files as a website on your Wi-Fi",
            show_close_button=True,
        )
        self.set_titlebar(header)
        outer = padded(
            Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACING_M), WINDOW_MARGIN
        )
        self.add(outer)

        form = Gtk.Grid(column_spacing=SPACING_M, row_spacing=SPACING_S)
        outer.pack_start(form, False, False, 0)
        form.attach(label("Name"), 0, 0, 1, 1)
        self._name_entry = Gtk.Entry(hexpand=True)
        self._name_entry.set_placeholder_text("My Portfolio")
        self._name_entry.set_activates_default(True)
        form.attach(self._name_entry, 1, 0, 1, 1)

        form.attach(label("Folder"), 0, 1, 1, 1)
        path_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_S)
        self._path_entry = Gtk.Entry(hexpand=True)
        self._path_entry.set_placeholder_text(str(Path.home() / "my-site"))
        path_box.pack_start(self._path_entry, True, True, 0)
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", self._on_browse_clicked)
        path_box.pack_end(browse, False, False, 0)
        form.attach(path_box, 1, 1, 1, 1)

        form.attach(label("Custom link"), 0, 2, 1, 1)
        link_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_S)
        self._hostname_entry = Gtk.Entry(hexpand=True)
        self._hostname_entry.set_placeholder_text("mysite")
        link_box.pack_start(self._hostname_entry, True, True, 0)
        link_box.pack_start(label(".local", "xposer-dim", "xposer-mono"), False, False, 0)
        form.attach(link_box, 1, 2, 1, 1)

        self._spa_check = Gtk.CheckButton(label=SPA_LABEL)
        self._hidden_check = Gtk.CheckButton(label=HIDDEN_LABEL)
        outer.pack_start(self._spa_check, False, False, 0)
        outer.pack_start(self._hidden_check, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_S)
        actions.set_halign(Gtk.Align.END)
        self._spinner = Gtk.Spinner()
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _button: self.destroy())
        self._serve_button = Gtk.Button(label="Share", can_default=True)
        add_classes(self._serve_button, "suggested-action")
        self._serve_button.connect("clicked", self._on_serve_clicked)
        actions.pack_start(self._spinner, False, False, 0)
        actions.pack_start(cancel, False, False, 0)
        actions.pack_start(self._serve_button, False, False, 0)
        outer.pack_end(actions, False, False, 0)
        self.show_all()
        self._serve_button.grab_default()

    def _on_browse_clicked(self, _button: Gtk.Button) -> None:
        """Pick a folder with the platform's native chooser."""
        chooser = Gtk.FileChooserNative.new(
            "Select a folder", self, Gtk.FileChooserAction.SELECT_FOLDER, "Select", "Cancel"
        )
        chooser.set_local_only(True)
        if chooser.run() == Gtk.ResponseType.ACCEPT:
            filename = chooser.get_filename()
            if filename:
                self._path_entry.set_text(filename)
                if not self._name_entry.get_text().strip():
                    self._name_entry.set_text(Path(filename).name)
        chooser.destroy()

    def _on_serve_clicked(self, _button: Gtk.Button | None) -> None:
        """Validate the form, warn on sensitive folders, then serve."""
        raw_path = self._path_entry.get_text().strip()
        folder = Path(raw_path).expanduser() if raw_path else None
        if folder is None or not folder.is_dir():
            show_error(self, "That folder does not exist.")
            return
        name = self._name_entry.get_text().strip() or folder.name or "Folder"
        if self._service_manager.is_sensitive_folder(folder):
            if not confirm(self, "Share a sensitive folder?", HOME_WARNING_TEXT):
                return
        hostname = self._hostname_entry.get_text().strip() or None
        options = FolderOptions(
            spa_fallback=self._spa_check.get_active(),
            show_hidden=self._hidden_check.get_active(),
        )
        self._set_busy(True)

        def work() -> tuple[Service, bool]:
            service = self._service_manager.serve_folder(name, raw_path, hostname, options=options)
            return service, self._service_manager.lan_reachable(service)

        run_off_thread("xposer-serve", work, self._serve_succeeded, self._lifecycle_failed)

    def _set_busy(self, busy: bool) -> None:
        """Toggle the action button and spinner while work is in flight."""
        self._serve_button.set_sensitive(not busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()

    def _lifecycle_failed(self, exc: Exception) -> None:
        """Report a failed serve attempt and re-enable the form."""
        if self._destroyed:
            return
        self._set_busy(False)
        show_error(self, error_message(exc))

    def _serve_succeeded(self, result: tuple[Service, bool]) -> None:
        """Close the form and show the new service's QR code."""
        if self._destroyed:
            return
        service, reachable = result
        self.destroy()
        if self._window_manager is not None:
            self._window_manager.open_qr_window(service)
        if not reachable:
            show_warning(None, UNREACHABLE_WARNING_TITLE, UNREACHABLE_HINT)

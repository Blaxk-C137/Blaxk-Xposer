"""The 'Serve a Folder' window: share a directory over HTTP."""

from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from models.service import Service
from services.service_manager import ServiceError, ServiceManager
from utils.ui_tasks import run_off_thread
from windows.dialogs import FIREWALL_HINT, FIREWALL_WARNING_TITLE, confirm, show_error, show_warning

HOME_WARNING_TEXT = (
    "This folder contains personal or system files. Everything inside it "
    "will be visible to everyone on this WiFi network."
)


class ServeFolderWindow(Gtk.Window):
    """Form serving a local folder as a static HTTP site."""

    def __init__(
        self,
        service_manager: ServiceManager,
        window_manager: object | None = None,
    ) -> None:
        super().__init__(title="Serve a Folder")
        self._service_manager = service_manager
        self._window_manager = window_manager
        self._destroyed = False
        self.set_default_size(400, 250)
        self.connect("destroy", self._on_destroyed)
        self._build_ui()

    def _on_destroyed(self, _widget: Gtk.Widget) -> None:
        """Mark the window dead so queued callbacks ignore it."""
        self._destroyed = True

    def _build_ui(self) -> None:
        header = Gtk.HeaderBar(
            title="Serve a Folder",
            subtitle="Share files as a website on your WiFi",
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
        outer.pack_start(form, True, True, 0)

        form.attach(Gtk.Label(label="Service Name", halign=Gtk.Align.START), 0, 0, 1, 1)
        self._name_entry = Gtk.Entry(hexpand=True)
        self._name_entry.set_placeholder_text("My Portfolio")
        self._name_entry.set_activates_default(True)
        form.attach(self._name_entry, 1, 0, 1, 1)

        form.attach(Gtk.Label(label="Folder Path", halign=Gtk.Align.START), 0, 1, 1, 1)
        path_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._path_entry = Gtk.Entry(hexpand=True)
        self._path_entry.set_placeholder_text("/home/user/my-site")
        path_box.pack_start(self._path_entry, True, True, 0)
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", self._on_browse_clicked)
        path_box.pack_end(browse, False, False, 0)
        form.attach(path_box, 1, 1, 1, 1)

        form.attach(Gtk.Label(label="Hostname", halign=Gtk.Align.START), 0, 2, 1, 1)
        self._hostname_entry = Gtk.Entry(hexpand=True)
        self._hostname_entry.set_placeholder_text("mysite  (becomes mysite.local)")
        form.attach(self._hostname_entry, 1, 2, 1, 1)

        self._spinner = Gtk.Spinner()
        outer.pack_start(self._spinner, False, False, 0)

        actions = Gtk.ButtonBox(orientation=Gtk.Orientation.HORIZONTAL)
        actions.set_layout(Gtk.ButtonBoxStyle.END)
        actions.set_spacing(6)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _button: self.destroy())
        self._serve_button = Gtk.Button(label="Serve", can_default=True)
        self._serve_button.get_style_context().add_class("suggested-action")
        self._serve_button.connect("clicked", self._on_serve_clicked)
        actions.add(cancel)
        actions.add(self._serve_button)
        outer.pack_end(actions, False, False, 0)

        self.show_all()

    def _on_browse_clicked(self, _button: Gtk.Button) -> None:
        """Let the user pick a folder and fill the path entry with it."""
        dialog = Gtk.FileChooserDialog(
            title="Select a Folder",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Select", Gtk.ResponseType.OK)
        dialog.set_local_only(True)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            filename = dialog.get_filename()
            if filename:
                self._path_entry.set_text(filename)
        dialog.destroy()

    def _on_serve_clicked(self, _button: Gtk.Button) -> None:
        """Validate the form, warn on sensitive folders, then serve."""
        name = self._name_entry.get_text().strip()
        raw_path = self._path_entry.get_text().strip()
        if not name:
            show_error(self, "Please enter a service name.")
            return
        folder = Path(raw_path).expanduser() if raw_path else None
        if folder is None or not folder.is_dir():
            show_error(self, "That folder does not exist.")
            return
        if self._service_manager.is_sensitive_folder(folder):
            approved = confirm(
                self,
                "Share a sensitive folder?",
                HOME_WARNING_TEXT,
            )
            if not approved:
                return
        self._set_busy(True)
        hostname = self._hostname_entry.get_text().strip() or None

        def work() -> tuple[Service, bool]:
            service = self._service_manager.serve_folder(name, raw_path, hostname)
            reachable = self._service_manager.lan_reachable(service)
            return service, reachable

        run_off_thread(
            "xposer-serve",
            work,
            self._serve_succeeded,
            self._lifecycle_failed,
        )

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
        message = exc.user_message if isinstance(exc, ServiceError) else str(exc)
        show_error(self, message)

    def _serve_succeeded(self, result: tuple[Service, bool]) -> None:
        """Close the form and hand the new service to the window manager."""
        if self._destroyed:
            return
        service, reachable = result
        self.destroy()
        if self._window_manager is not None:
            self._window_manager.open_qr_window(service)
        if not reachable:
            show_warning(None, FIREWALL_WARNING_TITLE, FIREWALL_HINT)

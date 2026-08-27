"""Settings window: interface choice, autostart, saved-service cleanup."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from config.config_store import ConfigStore
from network.network_detector import NetworkDetector
from services.service_manager import ServiceManager
from windows.dialogs import confirm

AUTOMATIC_INTERFACE_LABEL = "Automatic (recommended)"
CLEAR_SAVED_CONFIRM_TITLE = "Clear all saved services?"
CLEAR_SAVED_CONFIRM_TEXT = (
    "Stored service configurations will be removed from disk."
)
ABOUT_COMMENTS = "Expose your local work to anyone on your WiFi."


class SettingsWindow(Gtk.Window):
    """App preferences backed by ConfigStore."""

    def __init__(
        self,
        service_manager: ServiceManager,
        detector: NetworkDetector,
        config_store: ConfigStore,
        app_version: str,
    ) -> None:
        super().__init__(title="Settings")
        self._service_manager = service_manager
        self._detector = detector
        self._config_store = config_store
        self._app_version = app_version
        self.set_default_size(400, 300)
        self._build_ui()
        self._load_current()

    def _build_ui(self) -> None:
        header = Gtk.HeaderBar(title="Settings", show_close_button=True)
        self.set_titlebar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        self.add(outer)

        form = Gtk.Grid(column_spacing=8, row_spacing=12)
        outer.pack_start(form, True, True, 0)

        form.attach(
            Gtk.Label(label="Network Interface", halign=Gtk.Align.START),
            0,
            0,
            1,
            1,
        )
        self._interface_combo = Gtk.ComboBoxText()
        self._interface_combo.set_hexpand(True)
        form.attach(self._interface_combo, 1, 0, 1, 1)

        form.attach(
            Gtk.Label(label="Start on Login", halign=Gtk.Align.START), 0, 1, 1, 1
        )
        self._autostart_switch = Gtk.Switch(halign=Gtk.Align.START)
        form.attach(self._autostart_switch, 1, 1, 1, 1)

        clear_button = Gtk.Button(label="Clear Saved Services")
        clear_button.get_style_context().add_class("destructive-action")
        clear_button.connect("clicked", self._on_clear_saved_clicked)
        form.attach(clear_button, 0, 2, 2, 1)

        about_button = Gtk.Button(label="About Xposer")
        about_button.connect("clicked", self._on_about_clicked)
        form.attach(about_button, 0, 3, 2, 1)

        save = Gtk.Button(label="Save", can_default=True)
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", self._on_save_clicked)
        actions = Gtk.ButtonBox(orientation=Gtk.Orientation.HORIZONTAL)
        actions.set_layout(Gtk.ButtonBoxStyle.END)
        actions.add(save)
        outer.pack_end(actions, False, False, 0)

        self.show_all()

    def _load_current(self) -> None:
        """Fill the controls from the persisted configuration."""
        config = self._config_store.load()
        self._interface_combo.append(None, AUTOMATIC_INTERFACE_LABEL)
        interface_names: list[str | None] = [None]
        for info in self._detector.list_interfaces():
            self._interface_combo.append(info.name, f"{info.name} ({info.ip})")
            interface_names.append(info.name)
        active_index = 0
        if config.network_interface is not None:
            for index, name in enumerate(interface_names):
                if name == config.network_interface:
                    active_index = index
                    break
        self._interface_combo.set_active(active_index)
        self._autostart_switch.set_active(config.start_on_login)

    def _on_clear_saved_clicked(self, _button: Gtk.Button) -> None:
        """Drop every persisted service record after confirmation."""
        approved = confirm(
            self, CLEAR_SAVED_CONFIRM_TITLE, CLEAR_SAVED_CONFIRM_TEXT
        )
        if approved:
            self._service_manager.clear_saved_services()

    def _on_about_clicked(self, _button: Gtk.Button) -> None:
        """Show the standard About dialog."""
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name("Xposer")
        dialog.set_version(self._app_version)
        dialog.set_comments(ABOUT_COMMENTS)
        dialog.run()
        dialog.destroy()

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        """Persist the chosen interface and autostart preference."""
        config = self._config_store.load()
        selected = self._interface_combo.get_active_id()
        config.network_interface = selected if selected else None
        config.start_on_login = self._autostart_switch.get_active()
        self._config_store.save(config)
        self._config_store.set_start_on_login(config.start_on_login)
        self.destroy()

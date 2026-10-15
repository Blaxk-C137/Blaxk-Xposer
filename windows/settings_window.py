"""Settings window: network, smart sharing, custom links, startup, data.

Settings apply immediately (GNOME HIG); there is no Save button.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from config.config_store import AutostartError, ConfigStore, autostart_supported
from network.firewall import allow_through_windows_firewall
from network.network_detector import NetworkDetector
from services.service_manager import ServiceManager
from utils.logger import log_file_path
from utils.platform_env import IS_WINDOWS
from windows.dialogs import FIREWALL_RULE_FAILED_TEXT, confirm, show_error
from windows.theme import (
    FORM_WIDTH,
    SPACING_M,
    SPACING_XS,
    WINDOW_MARGIN,
    add_classes,
    padded,
)
from windows.widgets import label, open_link

AUTOMATIC_INTERFACE_LABEL = "Automatic (recommended)"
CLEAR_SAVED_CONFIRM_TITLE = "Clear saved services?"
CLEAR_SAVED_CONFIRM_TEXT = (
    "Stopped services leave the list and stored settings are removed from disk."
)
ABOUT_COMMENTS = "Share your local apps and folders with devices on your Wi-Fi."
SMART_DESCRIPTION = (
    "Makes dev servers accept phones: fixes host checks on custom links, "
    "points localhost API calls at shared backends, and handles CORS and "
    "HTTPS. Turn off to relay raw bytes only."
)
BACKEND_DESCRIPTIONS = {
    "avahi": "Custom .local links are published by Avahi.",
    "zeroconf": "Custom .local links are published by Xposer (zeroconf).",
}
AVAILABLE_TEXT = "Custom .local links are available."
NO_BACKEND_TEXT = (
    "Custom .local links are unavailable. Install avahi-daemon (Linux) or run: pip install zeroconf"
)


class SettingsWindow(Gtk.Window):
    """App preferences, applied immediately."""

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
        self._loading = True
        self.set_default_size(FORM_WIDTH, -1)
        self._build_ui()
        self._load_current()
        self._loading = False

    # ---- construction ----

    def _build_ui(self) -> None:
        """Sections of boxed rows."""
        header = Gtk.HeaderBar(title="Settings", show_close_button=True)
        self.set_titlebar(header)
        outer = padded(
            Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACING_M), WINDOW_MARGIN
        )
        self.add(outer)

        network = self._section(outer, "Network")
        self._interface_combo = Gtk.ComboBoxText()
        self._interface_combo.connect("changed", self._on_interface_changed)
        self._row(
            network, "Network interface", "Which address phones should use.", self._interface_combo
        )
        self._smart_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._smart_switch.connect("notify::active", self._on_smart_toggled)
        self._row(network, "Smart sharing", SMART_DESCRIPTION, self._smart_switch)
        backends = self._service_manager.hostname_support()
        backend_text = (
            BACKEND_DESCRIPTIONS.get(backends[0], AVAILABLE_TEXT) if backends else NO_BACKEND_TEXT
        )
        self._row(network, "Custom links", backend_text, None)

        system = self._section(outer, "System")
        self._autostart_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._autostart_switch.set_sensitive(autostart_supported())
        self._autostart_switch.connect("notify::active", self._on_autostart_toggled)
        self._row(
            system,
            "Start on login",
            "Open Xposer in the tray when you sign in.",
            self._autostart_switch,
        )
        if IS_WINDOWS:
            allow = Gtk.Button(label="Allow…", valign=Gtk.Align.CENTER)
            allow.connect("clicked", self._on_allow_firewall_clicked)
            self._row(system, "Windows Firewall", "Let phones connect on private networks.", allow)
        logs = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        logs.connect("clicked", self._on_logs_clicked)
        self._row(system, "Log file", str(log_file_path()), logs)

        data = self._section(outer, "Data")
        clear_button = Gtk.Button(label="Clear…", valign=Gtk.Align.CENTER)
        add_classes(clear_button, "destructive-action")
        clear_button.connect("clicked", self._on_clear_saved_clicked)
        self._row(
            data, "Saved services", "Forget stopped services and stored settings.", clear_button
        )
        about = Gtk.Button(label="About", valign=Gtk.Align.CENTER)
        about.connect("clicked", self._on_about_clicked)
        self._row(data, f"Xposer {self._app_version}", ABOUT_COMMENTS, about)
        self.show_all()

    @staticmethod
    def _section(parent: Gtk.Box, title: str) -> Gtk.ListBox:
        """Heading plus a framed list for rows."""
        parent.pack_start(label(title, "xposer-title"), False, False, 0)
        frame = Gtk.Frame()
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        frame.add(listbox)
        parent.pack_start(frame, False, False, 0)
        return listbox

    @staticmethod
    def _row(
        listbox: Gtk.ListBox, title: str, description: str, control: Gtk.Widget | None
    ) -> None:
        """Title and description on the left, control on the right."""
        row = Gtk.ListBoxRow(activatable=False)
        box = padded(Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING_M), SPACING_M)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACING_XS)
        text.pack_start(label(title), False, False, 0)
        text.pack_start(
            label(description, "xposer-dim", "xposer-small", wrap=True), False, False, 0
        )
        box.pack_start(text, True, True, 0)
        if control is not None:
            box.pack_end(control, False, False, 0)
        row.add(box)
        listbox.add(row)

    def _load_current(self) -> None:
        """Fill the controls from the persisted configuration."""
        config = self._config_store.load()
        self._interface_combo.append("", AUTOMATIC_INTERFACE_LABEL)
        active_id = ""
        for info in self._detector.list_interfaces():
            if not info.is_up and info.name != config.network_interface:
                continue
            note = " · virtual" if info.is_virtual else ""
            self._interface_combo.append(info.name, f"{info.name} ({info.ip}){note}")
            if info.name == config.network_interface:
                active_id = info.name
        self._interface_combo.set_active_id(active_id)
        self._smart_switch.set_active(config.smart_proxy)
        self._autostart_switch.set_active(config.start_on_login)

    # ---- handlers ----

    def _on_interface_changed(self, combo: Gtk.ComboBoxText) -> None:
        """Persist the preferred interface; links follow within seconds."""
        if self._loading:
            return
        selected = combo.get_active_id()
        self._service_manager.set_network_interface(selected or None)

    def _on_smart_toggled(self, switch: Gtk.Switch, _param: object) -> None:
        """Toggle HTTP-aware rewriting for new connections."""
        if not self._loading:
            self._service_manager.set_smart_proxy(switch.get_active())

    def _on_autostart_toggled(self, switch: Gtk.Switch, _param: object) -> None:
        """Register or unregister start-on-login."""
        if self._loading:
            return
        enabled = switch.get_active()
        try:
            self._config_store.set_start_on_login(enabled)
        except AutostartError as exc:
            self._loading = True
            switch.set_active(not enabled)
            self._loading = False
            show_error(self, f"Could not change start on login: {exc}")
            return
        config = self._config_store.load()
        config.start_on_login = enabled
        self._config_store.save(config)

    def _on_allow_firewall_clicked(self, _button: Gtk.Button) -> None:
        """Ask Windows to add the inbound rule (UAC prompt)."""
        if not allow_through_windows_firewall():
            show_error(
                self,
                FIREWALL_RULE_FAILED_TEXT,
            )

    def _on_logs_clicked(self, _button: Gtk.Button) -> None:
        """Open the folder holding the log file."""
        folder = log_file_path().parent
        folder.mkdir(parents=True, exist_ok=True)
        open_link(self, folder.as_uri())

    def _on_clear_saved_clicked(self, _button: Gtk.Button) -> None:
        """Drop persisted service records after confirmation."""
        if confirm(self, CLEAR_SAVED_CONFIRM_TITLE, CLEAR_SAVED_CONFIRM_TEXT):
            self._service_manager.clear_saved_services()

    def _on_about_clicked(self, _button: Gtk.Button) -> None:
        """Show the standard About dialog."""
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name("Xposer")
        dialog.set_version(self._app_version)
        dialog.set_comments(ABOUT_COMMENTS)
        dialog.set_logo_icon_name("network-wireless-symbolic")
        dialog.run()
        dialog.destroy()

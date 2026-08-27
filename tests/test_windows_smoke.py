"""Construction smoke tests for GTK windows (skipped without a display)."""

import pytest

pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402


def gtk_ready() -> bool:
    try:
        result = Gtk.init_check()
        return bool(result[0] if isinstance(result, tuple) else result)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not gtk_ready(), reason="no display available")


def pump_gtk_events() -> None:
    """Run pending GTK events so scheduled idle callbacks execute."""
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)


@pytest.fixture()
def running_service(components):
    site = components["tmp_path"] / "smoke-site"
    site.mkdir()
    (site / "index.html").write_text("smoke", encoding="utf-8")
    return components["manager"].serve_folder("Smoke", str(site))


def test_dialog_helpers_exist():
    from windows import dialogs

    assert callable(dialogs.show_error)
    assert callable(dialogs.confirm)


def test_expose_and_serve_windows_construct(components):
    from windows.expose_port_window import ExposePortWindow
    from windows.serve_folder_window import ServeFolderWindow

    expose = ExposePortWindow(components["manager"], components["detector"], None)
    expose.destroy()
    serve = ServeFolderWindow(components["manager"], None)
    serve.destroy()


def test_qr_windows_construct(components, running_service):
    from windows.all_qr_codes_window import AllQRCodesWindow
    from windows.qr_code_window import QRCodeWindow

    qr = QRCodeWindow(components["manager"], components["qr"], running_service, None)
    qr.destroy()
    gallery = AllQRCodesWindow(components["manager"], components["qr"])
    gallery.refresh()
    gallery.destroy()


def test_settings_and_window_manager_construct(components):
    from windows.settings_window import SettingsWindow
    from windows.window_manager import WindowManager

    settings = SettingsWindow(
        components["manager"],
        components["detector"],
        components["config"],
        app_version="test",
    )
    settings.destroy()

    window_manager = WindowManager(
        components["manager"],
        components["detector"],
        components["qr"],
        components["config"],
        app_version="test",
    )
    gallery = window_manager.show_all_qr()
    window_manager.handle_event("services_changed")
    pump_gtk_events()
    gallery.destroy()
    pump_gtk_events()
    window_manager.forget_qr_window("nonexistent")


def test_tray_manager_builds_menu(components):
    from tray.tray_manager import TrayError, TrayManager
    from windows.window_manager import WindowManager

    window_manager = WindowManager(
        components["manager"],
        components["detector"],
        components["qr"],
        components["config"],
        app_version="test",
    )
    try:
        tray = TrayManager(components["manager"], window_manager, app_version="test")
    except TrayError as exc:  # no indicator backend on this host
        pytest.skip(f"tray backend unavailable: {exc}")
    components["manager"].add_observer(tray.handle_event)
    site = components["tmp_path"] / "tray-site"
    site.mkdir()
    (site / "index.html").write_text("tray", encoding="utf-8")
    components["manager"].serve_folder("Trayed", str(site))
    tray.handle_event("services_changed")
    pump_gtk_events()
    assert components["manager"].active_services()


def test_form_windows_map_children_when_presented(components):
    from windows.expose_port_window import ExposePortWindow
    from windows.serve_folder_window import ServeFolderWindow
    from windows.settings_window import SettingsWindow

    expose = ExposePortWindow(components["manager"], components["detector"], None)
    expose.present()
    pump_gtk_events()
    assert expose._name_entry.get_mapped(), "Expose form rendered empty"
    expose.destroy()

    serve = ServeFolderWindow(components["manager"], None)
    serve.present()
    pump_gtk_events()
    assert serve._path_entry.get_mapped(), "Serve form rendered empty"
    serve.destroy()

    settings = SettingsWindow(
        components["manager"],
        components["detector"],
        components["config"],
        app_version="test",
    )
    settings.present()
    pump_gtk_events()
    assert settings._autostart_switch.get_mapped(), "Settings rendered empty"
    settings.destroy()


def test_qr_windows_map_children_when_presented(components, running_service):
    from windows.all_qr_codes_window import AllQRCodesWindow
    from windows.qr_code_window import QRCodeWindow

    qr = QRCodeWindow(components["manager"], components["qr"], running_service, None)
    qr.present()
    pump_gtk_events()
    assert qr._url_label.get_mapped(), "QR window rendered empty"
    qr.destroy()

    gallery = AllQRCodesWindow(components["manager"], components["qr"])
    gallery.present()
    pump_gtk_events()
    assert gallery._stack.get_mapped(), "Gallery rendered empty"
    gallery.destroy()

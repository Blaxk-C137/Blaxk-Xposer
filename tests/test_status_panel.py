"""Tests for the tray status panel: structure, refresh, wiring."""

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
    site = components["tmp_path"] / "panel-site"
    site.mkdir()
    (site / "index.html").write_text("panel", encoding="utf-8")
    return components["manager"].serve_folder("PanelSite", str(site))


def make_panel(components, window_manager=None):
    from windows.status_panel import StatusPanel

    panel = StatusPanel(
        components["manager"],
        components["detector"],
        window_manager,
        app_version="test",
    )
    panel.present()
    pump_gtk_events()
    return panel


def row_urls(panel) -> list[str]:
    """Collect the URL label text from every service row."""
    return [child._url_label.get_text() for child in panel._rows_box.get_children()]


def test_panel_shows_header_ip_and_empty_state(components):
    panel = make_panel(components)
    lan_ip = components["detector"].get_lan_ip()
    assert lan_ip in panel._ip_label.get_text()
    assert panel._empty_label.get_mapped(), "empty state not shown"
    assert row_urls(panel) == []
    panel.destroy()
    pump_gtk_events()


def test_panel_lists_running_service_with_url(components, running_service):
    panel = make_panel(components)
    assert not panel._empty_label.get_mapped(), "empty state shown despite service"
    assert running_service.lan_url in row_urls(panel)
    panel.destroy()
    pump_gtk_events()


def test_panel_refresh_reflects_stopped_service(components, running_service):
    panel = make_panel(components)
    components["manager"].stop_service(running_service.id)
    panel.refresh()
    pump_gtk_events()
    # Stopped services stay listed (restartable), just visually dimmed.
    assert not panel._empty_label.get_mapped(), "stopped row disappeared"
    row = panel._rows_box.get_children()[0]
    assert row._toggle_button.get_tooltip_text() == "Start sharing", "stop did not refresh row"
    panel.destroy()
    pump_gtk_events()


def test_window_manager_routes_status_panel_singleton(components):
    from windows.window_manager import WindowManager

    wm = WindowManager(
        components["manager"],
        components["detector"],
        components["qr"],
        components["config"],
        app_version="test",
    )
    panel = wm.show_status_panel()
    same = wm.show_status_panel()
    assert panel is same, "status panel is not a singleton"
    panel.destroy()
    pump_gtk_events()
    again = wm.show_status_panel()
    assert again is not panel, "destroyed panel was not recreated"
    again.destroy()
    pump_gtk_events()


def test_window_manager_refreshes_open_status_panel(components, running_service):
    from windows.window_manager import WindowManager

    wm = WindowManager(
        components["manager"],
        components["detector"],
        components["qr"],
        components["config"],
        app_version="test",
    )
    panel = wm.show_status_panel()
    pump_gtk_events()
    refresh_calls = []
    orig_refresh = panel.refresh
    panel.refresh = lambda: (refresh_calls.append(True), orig_refresh())
    wm.handle_event("services_changed")
    pump_gtk_events()
    assert refresh_calls, "panel not refreshed on services_changed"
    panel.destroy()
    pump_gtk_events()


def test_tray_menu_contains_status_panel_item(components):
    from tray.tray_manager import TrayError, TrayManager
    from windows.window_manager import WindowManager

    wm = WindowManager(
        components["manager"],
        components["detector"],
        components["qr"],
        components["config"],
        app_version="test",
    )
    try:
        tray = TrayManager(components["manager"], wm, app_version="test")
    except TrayError as exc:
        pytest.skip(f"tray backend unavailable: {exc}")
    tray.handle_event("services_changed")
    pump_gtk_events()
    labels = [
        item.get_label()
        for item in tray.menu.get_children()
        if hasattr(item, "get_label") and item.get_label()
    ]
    assert any("Open Xposer" in label for label in labels), (
        "dashboard entry missing from tray menu"
    )

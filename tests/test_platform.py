"""Platform-specific behaviour, exercised on any OS by simulating the platform."""

import socket
import sys
import types
from pathlib import Path

import pytest

import config.config_store as config_module
import network.firewall as firewall
import network.socket_utils as socket_utils
import services.file_server as file_server
import services.service_manager as service_manager
import utils.platform_env as platform_env
from config.config_store import AutostartError, ConfigStore


@pytest.fixture()
def as_windows(monkeypatch, tmp_path):
    """Pretend to be Windows with typical known-folder variables."""
    monkeypatch.setattr(platform_env, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_env, "IS_LINUX", False)
    monkeypatch.setattr(platform_env, "IS_MACOS", False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    return tmp_path


def test_windows_directories(as_windows):
    assert platform_env.config_dir() == as_windows / "Roaming" / "Xposer"
    assert platform_env.cache_dir() == as_windows / "Local" / "Xposer" / "Cache"


def test_linux_directories_follow_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_env, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_env, "IS_MACOS", False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/is/ignored")
    assert platform_env.config_dir() == tmp_path / "cfg" / "xposer"
    assert platform_env.cache_dir() == Path.home() / ".cache" / "xposer"


def test_gui_interpreter_prefers_pythonw(monkeypatch, tmp_path):
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    python.write_text("", encoding="utf-8")
    pythonw.write_text("", encoding="utf-8")
    monkeypatch.setattr(platform_env, "IS_WINDOWS", True)
    monkeypatch.setattr(sys, "executable", str(python))
    assert platform_env.gui_python_executable() == str(pythonw)


class FakeRegistryKey:
    """Context-manager stand-in for a winreg key handle."""

    def __init__(self, store: dict[str, str]) -> None:
        self.store = store

    def __enter__(self) -> "FakeRegistryKey":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def fake_winreg(store: dict[str, str]) -> types.ModuleType:
    module = types.ModuleType("winreg")
    module.HKEY_CURRENT_USER = "HKCU"
    module.KEY_SET_VALUE = 2
    module.REG_SZ = 1
    module.OpenKey = lambda root, path, reserved, access: FakeRegistryKey(store)

    def set_value(key: FakeRegistryKey, name: str, reserved: int, kind: int, value: str) -> None:
        key.store[name] = value

    def delete_value(key: FakeRegistryKey, name: str) -> None:
        if name not in key.store:
            raise FileNotFoundError(name)
        del key.store[name]

    module.SetValueEx = set_value
    module.DeleteValue = delete_value
    return module


def test_windows_autostart_uses_run_key(monkeypatch, tmp_path):
    store: dict[str, str] = {}
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg(store))
    monkeypatch.setattr(config_module, "IS_WINDOWS", True)
    monkeypatch.setattr(config_module, "gui_python_executable", lambda: r"C:\Py\pythonw.exe")
    config = ConfigStore(path=tmp_path / "config.json")
    config.set_start_on_login(True)
    assert store["Xposer"] == f'"C:\\Py\\pythonw.exe" "{config_module.MAIN_ENTRY_PATH}"'
    config.set_start_on_login(False)
    assert "Xposer" not in store
    config.set_start_on_login(False)


def test_autostart_unsupported_platform_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "IS_WINDOWS", False)
    monkeypatch.setattr(config_module, "IS_LINUX", False)
    with pytest.raises(AutostartError):
        ConfigStore(path=tmp_path / "config.json").set_start_on_login(True)


class RecordingSocket:
    """Captures setsockopt calls."""

    def __init__(self) -> None:
        self.options: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))


def test_windows_bind_policy_is_exclusive(monkeypatch):
    exclusive = -5
    monkeypatch.setattr(socket_utils, "IS_WINDOWS", True)
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", exclusive, raising=False)
    recorder = RecordingSocket()
    socket_utils.apply_bind_policy(recorder)
    assert recorder.options == [(socket.SOL_SOCKET, exclusive, 1)]


def test_posix_bind_policy_reuses_address(monkeypatch):
    monkeypatch.setattr(socket_utils, "IS_WINDOWS", False)
    recorder = RecordingSocket()
    socket_utils.apply_bind_policy(recorder)
    assert recorder.options == [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]


def test_listener_refuses_a_taken_port():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        assert socket_utils.can_bind("127.0.0.1", port) is False
        with pytest.raises(OSError):
            socket_utils.open_listener("127.0.0.1", port)
    finally:
        blocker.close()


def test_windows_firewall_command_targets_both_interpreters():
    command = firewall.windows_rule_commands(
        [Path(r"C:\Py\python.exe"), Path(r"C:\Py\pythonw.exe")]
    )
    assert command.count("netsh advfirewall firewall add rule") == 2
    assert 'program="C:\\Py\\pythonw.exe"' in command
    assert "profile=private,domain" in command
    assert command.index("delete rule") < command.index("add rule")


def test_firewall_help_per_platform(monkeypatch):
    monkeypatch.setattr(firewall, "IS_WINDOWS", True)
    windows_help = firewall.firewall_help(5173)
    assert windows_help.can_auto_allow is True
    assert any("Private network" in step for step in windows_help.steps)
    monkeypatch.setattr(firewall, "IS_WINDOWS", False)
    monkeypatch.setattr(firewall, "IS_LINUX", True)
    monkeypatch.setattr(
        firewall.shutil, "which", lambda name: "/usr/sbin/ufw" if name == "ufw" else None
    )
    linux_help = firewall.firewall_help(5173)
    assert linux_help.can_auto_allow is False
    assert "sudo ufw allow 5173/tcp" in linux_help.steps[0]


def test_firewall_auto_allow_is_windows_only(monkeypatch):
    monkeypatch.setattr(firewall, "IS_WINDOWS", False)
    assert firewall.allow_through_windows_firewall() is False


def test_windows_hidden_attribute_hides_files(monkeypatch, tmp_path):
    hidden_flag = 2
    target = tmp_path / "desktop.ini"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(file_server, "IS_WINDOWS", True)
    monkeypatch.setattr(file_server.stat, "FILE_ATTRIBUTE_HIDDEN", hidden_flag, raising=False)
    monkeypatch.setattr(file_server.stat, "FILE_ATTRIBUTE_SYSTEM", 4, raising=False)
    monkeypatch.setattr(
        file_server.os, "stat", lambda path: types.SimpleNamespace(st_file_attributes=hidden_flag)
    )
    assert file_server._has_hidden_attribute(str(target)) is True


def test_windows_sensitive_folders(monkeypatch):
    monkeypatch.setattr(service_manager, "IS_WINDOWS", True)
    monkeypatch.setenv("SystemDrive", "D:")
    monkeypatch.setenv("WINDIR", r"D:\Windows")
    folders = {str(path) for path in service_manager._sensitive_folders()}
    assert "D:\\" in folders
    assert r"D:\Windows" in folders


def test_tray_host_check_is_skipped_on_windows(monkeypatch):
    gi = pytest.importorskip("gi")
    assert gi is not None
    import tray.tray_host as tray_host

    monkeypatch.setattr(tray_host, "IS_WINDOWS", True)
    assert tray_host.tray_host_available() is False

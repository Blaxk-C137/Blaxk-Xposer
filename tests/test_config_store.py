"""Tests for configuration persistence."""

import json
import threading

import config.config_store as config_module
from config.config_store import AppConfig, ConfigStore
from models.service import SavedService, ServiceType


def test_missing_file_returns_defaults(tmp_path):
    store = ConfigStore(path=tmp_path / "config.json")
    config = store.load()
    assert config.saved_services == []
    assert config.network_interface is None
    assert config.start_on_login is False


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "nested" / "config.json"
    store = ConfigStore(path=path)
    config = AppConfig(
        network_interface="wlan0",
        start_on_login=True,
        saved_services=[
            SavedService(id="a", name="App", type=ServiceType.PORT, port=3000),
        ],
    )
    store.save(config)
    loaded = ConfigStore(path=path).load()
    assert loaded.network_interface == "wlan0"
    assert loaded.start_on_login is True
    assert loaded.saved_services == config.saved_services


def test_corrupt_file_returns_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    config = ConfigStore(path=path).load()
    assert config.saved_services == []


def test_undecodable_bytes_return_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b"\xff\xfe{")
    config = ConfigStore(path=path).load()
    assert config.saved_services == []
    assert config.network_interface is None
    assert config.start_on_login is False


def test_malformed_entries_are_skipped(tmp_path):
    path = tmp_path / "config.json"
    good = SavedService(id="ok", name="Fine", type=ServiceType.FOLDER).to_dict()
    path.write_text(json.dumps({"saved_services": [{"garbage": True}, good]}), encoding="utf-8")
    config = ConfigStore(path=path).load()
    assert len(config.saved_services) == 1
    assert config.saved_services[0].id == "ok"


def test_start_on_login_toggle(tmp_path, monkeypatch):
    autostart = tmp_path / "autostart"
    monkeypatch.setattr(config_module, "AUTOSTART_DIR", autostart)
    monkeypatch.setattr(config_module, "AUTOSTART_FILE", autostart / "xposer.desktop")
    store = ConfigStore(path=tmp_path / "config.json")
    store.set_start_on_login(True)
    assert (autostart / "xposer.desktop").exists()
    store.set_start_on_login(False)
    assert not (autostart / "xposer.desktop").exists()


def test_autostart_entry_targets_real_entry_point(tmp_path, monkeypatch):
    autostart = tmp_path / "autostart"
    monkeypatch.setattr(config_module, "AUTOSTART_DIR", autostart)
    monkeypatch.setattr(config_module, "AUTOSTART_FILE", autostart / "xposer.desktop")
    store = ConfigStore(path=tmp_path / "config.json")
    store.set_start_on_login(True)
    content = (autostart / "xposer.desktop").read_text(encoding="utf-8")
    exec_lines = [
        line for line in content.splitlines() if line.startswith("Exec=")
    ]
    assert len(exec_lines) == 1
    main_path = str(config_module.MAIN_ENTRY_PATH)
    assert f'"{main_path}"' in exec_lines[0]


def test_concurrent_saves_never_tear_the_file(tmp_path):
    store = ConfigStore(path=tmp_path / "config.json")
    worker_count = 8
    barrier = threading.Barrier(worker_count)
    errors: list[Exception] = []

    def save_one(index: int) -> None:
        name = f"svc{index}"
        try:
            config = store.load()
            config.saved_services = [
                SavedService(id=name, name=name, type=ServiceType.PORT, port=1)
            ]
            barrier.wait()
            store.save(config)
        except Exception as exc:  # surface worker failures to the assertion
            errors.append(exc)

    threads = [
        threading.Thread(target=save_one, args=(index,))
        for index in range(worker_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    loaded = ConfigStore(path=tmp_path / "config.json").load()
    assert len(loaded.saved_services) == 1

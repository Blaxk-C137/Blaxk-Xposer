"""Tests for the service domain models."""

from models.service import Service, ServiceStatus, ServiceType, SavedService


def make_service() -> Service:
    return Service(
        id="abc",
        name="My App",
        type=ServiceType.PORT,
        assigned_port=3000,
        lan_url="http://192.168.1.5:3000",
        port=3000,
    )


def test_enum_values_match_config_schema():
    assert ServiceType.PORT.value == "port"
    assert ServiceType.FOLDER.value == "folder"
    assert ServiceStatus.RUNNING.value == "running"
    assert ServiceStatus.STOPPED.value == "stopped"
    assert ServiceStatus.ERROR.value == "error"


def test_service_defaults():
    service = make_service()
    assert service.status == ServiceStatus.STOPPED
    assert service.connected_devices == 0
    assert service.qr_image_path is None
    assert service.folder_path is None
    assert service.auto_start is False


def test_saved_roundtrip():
    saved = make_service().to_saved()
    assert saved.id == "abc"
    assert saved.type == ServiceType.PORT
    restored = SavedService.from_dict(saved.to_dict())
    assert restored == saved


def test_folder_saved_service_serialization():
    saved = SavedService(
        id="x", name="Site", type=ServiceType.FOLDER, folder_path="/tmp/site"
    )
    data = saved.to_dict()
    assert data["type"] == "folder"
    assert data["port"] is None
    assert SavedService.from_dict(data) == saved

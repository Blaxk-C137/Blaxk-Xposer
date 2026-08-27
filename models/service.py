"""Core data structures for Xposer services."""

from dataclasses import dataclass
from enum import Enum


class ServiceType(Enum):
    """The kind of workload a service exposes."""

    PORT = "port"
    FOLDER = "folder"


class ServiceStatus(Enum):
    """Lifecycle state of a service."""

    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class SavedService:
    """Persistent form of a service stored in the config file."""

    id: str
    name: str
    type: ServiceType
    port: int | None = None
    folder_path: str | None = None
    auto_start: bool = False

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type.value,
            "port": self.port,
            "folder_path": self.folder_path,
            "auto_start": self.auto_start,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SavedService":
        """Rebuild a SavedService from its dictionary form."""
        port_value = data.get("port")
        folder_value = data.get("folder_path")
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            type=ServiceType(str(data["type"])),
            port=int(port_value) if port_value is not None else None,
            folder_path=str(folder_value) if folder_value is not None else None,
            auto_start=bool(data.get("auto_start", False)),
        )


@dataclass
class Service:
    """A single exposed service shown in the tray and QR windows."""

    id: str
    name: str
    type: ServiceType
    assigned_port: int
    lan_url: str
    status: ServiceStatus = ServiceStatus.STOPPED
    port: int | None = None
    folder_path: str | None = None
    connected_devices: int = 0
    qr_image_path: str | None = None
    auto_start: bool = False

    def to_saved(self) -> SavedService:
        """Project this runtime service into its persistent form."""
        return SavedService(
            id=self.id,
            name=self.name,
            type=self.type,
            port=self.port,
            folder_path=self.folder_path,
            auto_start=self.auto_start,
        )

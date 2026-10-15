"""Core data structures for Xposer services."""

from dataclasses import dataclass, field
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


class ShareScheme(Enum):
    """The link scheme LAN devices use to open a shared port.

    HTTP works for every local app: when the app itself speaks HTTPS,
    Xposer talks TLS to it and plain http to the phone, so no certificate
    warning appears. HTTPS keeps TLS end-to-end (needed for camera,
    microphone and other secure-context browser APIs) at the price of the
    dev certificate warning. TCP is a raw byte relay for non-HTTP servers.
    """

    HTTP = "http"
    HTTPS = "https"
    TCP = "tcp"


class LocalProtocol(Enum):
    """What the local app speaks, as detected when it was shared."""

    HTTP = "http"
    HTTPS = "https"
    TCP = "tcp"


@dataclass(frozen=True)
class BackendHint:
    """A local port the shared app calls but which is not shared yet."""

    port: int
    label: str = ""

    @property
    def description(self) -> str:
        """'port 8000 (Uvicorn)' or 'port 8000'."""
        return f"port {self.port} ({self.label})" if self.label else f"port {self.port}"


@dataclass(frozen=True)
class FolderOptions:
    """Per-folder serving options."""

    spa_fallback: bool = False
    show_hidden: bool = False


@dataclass
class SavedService:
    """Persistent form of a service stored in the config file."""

    id: str
    name: str
    type: ServiceType
    port: int | None = None
    folder_path: str | None = None
    auto_start: bool = False
    hostname: str | None = None
    share_scheme: ShareScheme | None = None
    folder_options: FolderOptions = field(default_factory=FolderOptions)

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type.value,
            "port": self.port,
            "folder_path": self.folder_path,
            "auto_start": self.auto_start,
            "hostname": self.hostname,
            "share_scheme": self.share_scheme.value if self.share_scheme else None,
            "spa_fallback": self.folder_options.spa_fallback,
            "show_hidden": self.folder_options.show_hidden,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SavedService":
        """Rebuild a SavedService from its dictionary form.

        Keys added in later versions are optional so older config files
        keep loading.
        """
        port_value = data.get("port")
        folder_value = data.get("folder_path")
        hostname_value = data.get("hostname")
        scheme_value = data.get("share_scheme")
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            type=ServiceType(str(data["type"])),
            port=int(str(port_value)) if port_value is not None else None,
            folder_path=str(folder_value) if folder_value is not None else None,
            auto_start=bool(data.get("auto_start", False)),
            hostname=str(hostname_value) if hostname_value is not None else None,
            share_scheme=ShareScheme(str(scheme_value)) if scheme_value else None,
            folder_options=FolderOptions(
                spa_fallback=bool(data.get("spa_fallback", False)),
                show_hidden=bool(data.get("show_hidden", False)),
            ),
        )


@dataclass
class Service:
    """A single exposed service shown in the tray and dashboard.

    lan_url is the primary link (the custom .local link when it is live,
    otherwise the IP link); ip_url is always the IP form, the dependable
    fallback for devices that cannot resolve .local names.
    """

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
    hostname: str | None = None
    ip_url: str = ""
    share_scheme: ShareScheme = ShareScheme.HTTP
    preferred_scheme: ShareScheme | None = None
    local_protocol: LocalProtocol = LocalProtocol.HTTP
    hostname_active: bool = False
    requests_served: int = 0
    qr_custom_path: str | None = None
    backend_hints: tuple[BackendHint, ...] = ()
    folder_options: FolderOptions = field(default_factory=FolderOptions)
    last_error: str = ""

    @property
    def is_running(self) -> bool:
        """True while the service is reachable from the LAN."""
        return self.status == ServiceStatus.RUNNING

    def to_saved(self) -> SavedService:
        """Project this runtime service into its persistent form."""
        return SavedService(
            id=self.id,
            name=self.name,
            type=self.type,
            port=self.port,
            folder_path=self.folder_path,
            auto_start=self.auto_start,
            hostname=self.hostname,
            share_scheme=self.preferred_scheme,
            folder_options=self.folder_options,
        )

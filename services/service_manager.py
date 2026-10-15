"""Single source of truth for service lifecycle and state.

Every mutation of service state flows through ServiceManager. Windows and
the tray observe it; engines (ProxyEngine, FileServer) are owned siblings
and never talk to each other or to UI code. The manager also keeps the
proxy's share map current, which is what lets a shared frontend reach a
shared backend and lets custom .local links pass dev-server host checks.
"""

import os
import threading
import time
import uuid
import zlib
from collections.abc import Callable
from pathlib import Path

from config.config_store import ConfigStore
from models.service import (
    BackendHint,
    FolderOptions,
    LocalProtocol,
    SavedService,
    Service,
    ServiceStatus,
    ServiceType,
    ShareScheme,
)
from network.mdns_publisher import MdnsPublisher, normalize_hostname, to_local_name
from network.network_detector import (
    ALTERNATE_PORT_OFFSET,
    MAX_PORT,
    SCAN_RANGE_START,
    ListeningPort,
    NetworkDetector,
    UpstreamKind,
    UpstreamProbe,
)
from network.qr_generator import QRGenerator
from network.socket_utils import LOOPBACK_HOSTS, host_for_url, is_loopback
from services.file_server import FileServer, FileServerError
from services.http_rewriter import PublicEndpoint, RewriteMap
from services.proxy_engine import LISTEN_HOST_ANY, ProxyEngine, ProxyError
from utils.logger import get_logger
from utils.platform_env import IS_MACOS, IS_WINDOWS

logger = get_logger("services")

EVENT_SERVICES_CHANGED = "services_changed"
MONITOR_INTERVAL_SECONDS = 5.0
PORT_SCAN_CACHE_SECONDS = 10.0
MAX_BACKEND_HINTS = 4
FOLDER_PORT_SPAN = 900
OS_ASSIGNED_PORT = 0


def _sensitive_folders() -> tuple[Path, ...]:
    """System locations nobody should share by accident, per platform."""
    if IS_WINDOWS:
        drive = os.environ.get("SystemDrive", "C:")
        return (
            Path(f"{drive}\\"),
            Path(os.environ.get("WINDIR", rf"{drive}\Windows")),
            Path(os.environ.get("ProgramFiles", rf"{drive}\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", rf"{drive}\Program Files (x86)")),
            Path(os.environ.get("ProgramData", rf"{drive}\ProgramData")),
            Path.home() / "AppData",
        )
    unix = tuple(
        Path(path)
        for path in ("/", "/bin", "/boot", "/etc", "/lib", "/lib64", "/sbin", "/usr", "/var")
    )
    if IS_MACOS:
        return unix + (Path("/System"), Path("/Library"), Path("/private"))
    return unix + (Path.home() / ".ssh", Path.home() / ".gnupg")


SENSITIVE_FOLDERS: tuple[Path, ...] = _sensitive_folders()


class ServiceError(Exception):
    """Base class for errors with a human-readable message."""

    user_message = "Something went wrong while managing the service."

    def __init__(self, detail: str) -> None:
        super().__init__(detail)


class ValidationError(ServiceError):
    """User input failed validation."""

    user_message = "Please check the values you entered and try again."


class PortAlreadySharedError(ValidationError):
    """The local port is already shared by another running service."""

    user_message = "That port is already shared. Open the dashboard to find its link."


class PortNotListeningError(ServiceError):
    """The requested localhost port has no listener."""

    user_message = "That port is not running on this machine. Start your app first, then expose it."


class PortBindError(ServiceError):
    """A network port could not be opened."""

    user_message = "Xposer could not open a network port. It may already be in use."


class FolderInvalidError(ServiceError):
    """The chosen folder does not exist or is empty."""

    user_message = "That folder does not exist or is empty."


class ServiceNotFoundError(ServiceError):
    """The referenced service is no longer registered."""

    user_message = "That service no longer exists."


def build_url(scheme: ShareScheme, host: str, port: int) -> str:
    """Link for a service: 'http://host:port', or 'host:port' for raw TCP."""
    authority = f"{host_for_url(host)}:{port}"
    if scheme == ShareScheme.TCP:
        return authority
    return f"{scheme.value}://{authority}"


class ServiceManager:
    """Registry and lifecycle owner for every exposed service.

    Threading: registry reads/writes use self._lock (cheap, held briefly).
    Lifecycle operations (start/stop/rebind) are serialized by
    self._lifecycle so the monitor's IP-change rebinding can never
    interleave with a user's stop or restart.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        detector: NetworkDetector,
        qr_generator: QRGenerator,
        proxy_engine: ProxyEngine | None = None,
        file_server: FileServer | None = None,
        mdns_publisher: MdnsPublisher | None = None,
        monitor_interval: float = MONITOR_INTERVAL_SECONDS,
    ) -> None:
        self._config_store = config_store
        self._detector = detector
        self._qr_generator = qr_generator
        self._proxy_engine = proxy_engine if proxy_engine is not None else ProxyEngine()
        self._file_server = file_server if file_server is not None else FileServer()
        self._mdns = mdns_publisher if mdns_publisher is not None else MdnsPublisher()
        self._monitor_interval = monitor_interval
        self._services: dict[str, Service] = {}
        self._observers: list[Callable[[str], None]] = []
        self._lock = threading.RLock()
        self._lifecycle = threading.RLock()
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._listen_hosts: dict[str, str] = {}
        self._upstream_hosts: dict[str, str] = {}
        self._map_version = 0
        self._lan_ip: str | None = None
        self._port_scan: tuple[float, dict[int, ListeningPort]] | None = None
        config = config_store.load()
        self._interface_override = config.network_interface
        self._proxy_engine.set_smart_mode(config.smart_proxy)

    # ---- observation ----

    def add_observer(self, callback: Callable[[str], None]) -> None:
        """Register callback(event_name); called from arbitrary threads."""
        self._observers.append(callback)

    def _notify(self, event: str) -> None:
        """Call every observer, isolating their failures."""
        for observer in list(self._observers):
            try:
                observer(event)
            except Exception as exc:  # observers must never break the manager
                logger.warning(f"Observer raised during {event}: {exc}")

    # ---- queries ----

    def get_service(self, service_id: str) -> Service | None:
        """Return the live Service record for service_id, or None."""
        with self._lock:
            return self._services.get(service_id)

    def active_services(self) -> list[Service]:
        """Snapshot of services currently RUNNING."""
        with self._lock:
            return [s for s in self._services.values() if s.status == ServiceStatus.RUNNING]

    def all_services(self) -> list[Service]:
        """Snapshot of every known service (running first, then by name)."""
        with self._lock:
            services = list(self._services.values())
        return sorted(services, key=lambda s: (s.status == ServiceStatus.STOPPED, s.name.lower()))

    def active_ports(self) -> set[int]:
        """Network ports Xposer itself currently listens on."""
        with self._lock:
            return {
                s.assigned_port
                for s in self._services.values()
                if s.status != ServiceStatus.STOPPED and s.assigned_port
            }

    def shared_local_ports(self) -> set[int]:
        """Local app ports currently shared."""
        with self._lock:
            return {
                s.port
                for s in self._services.values()
                if s.type == ServiceType.PORT and s.status != ServiceStatus.STOPPED and s.port
            }

    def detected_ports(self) -> list[ListeningPort]:
        """Listening local ports, minus Xposer's own listeners."""
        return list(self._scan_ports(force=True).values())

    def _own_listeners(self) -> frozenset[tuple[str, int]]:
        """(address, port) of every socket Xposer itself listens on."""
        with self._lock:
            return frozenset(
                (self._listen_hosts.get(s.id, LISTEN_HOST_ANY), s.assigned_port)
                for s in self._services.values()
                if s.status != ServiceStatus.STOPPED and s.assigned_port
            )

    def probe_port(self, port: int) -> UpstreamProbe:
        """Where a local port answers and what it speaks (blocking, ~0.3-1.3 s)."""
        return self._detector.probe_upstream(port)

    def lan_reachable(self, service: Service) -> bool:
        """True when the service answers on the current LAN address."""
        return self._detector.is_port_in_use(service.assigned_port, host=self.current_lan_ip())

    def is_sensitive_folder(self, folder: Path) -> bool:
        """True when folder is home or a system directory worth warning about."""
        resolved = os.path.normcase(str(folder.expanduser().resolve()))
        sensitive = {os.path.normcase(str(path.resolve())) for path in SENSITIVE_FOLDERS}
        sensitive.add(os.path.normcase(str(Path.home().resolve())))
        return resolved in sensitive

    def hostname_support(self) -> list[str]:
        """Names of the mDNS backends available for custom links."""
        return self._mdns.backend_names()

    def smart_proxy_enabled(self) -> bool:
        """True when HTTP-aware rewriting is on."""
        return self._proxy_engine.smart_mode()

    # ---- lifecycle ----

    def expose_port(
        self,
        name: str,
        port: int,
        hostname: str | None = None,
        share_scheme: ShareScheme | None = None,
    ) -> Service:
        """Expose an already-running localhost port to the LAN.

        A stopped service for the same port is reused, so sharing the same
        app again keeps one record and, where possible, the same link.
        """
        trimmed = self._validate_name(name)
        if not self._detector.validate_port(port):
            raise ValidationError("Port must be between 1 and 65535.")
        fqdn = self._prepare_hostname(hostname)
        with self._lifecycle:
            if port in self.active_ports() and port not in self.shared_local_ports():
                raise ValidationError(f"Port {port} is one of Xposer's own shared links.")
            existing = self._find_service(ServiceType.PORT, lambda s: s.port == port)
            if existing is not None and existing.status != ServiceStatus.STOPPED:
                raise PortAlreadySharedError(f"Port {port} is already shared as '{existing.name}'")
            service = existing or Service(
                id=str(uuid.uuid4()),
                name=trimmed,
                type=ServiceType.PORT,
                port=port,
                assigned_port=0,
                lan_url="",
            )
            with self._lock:
                service.name = trimmed
                service.hostname = fqdn
                service.preferred_scheme = share_scheme
            self._start_port_service(service)
            self._register(service)
        logger.info(f"Exposed port {port} as {service.lan_url}")
        return service

    def serve_folder(
        self,
        name: str,
        folder_path: str,
        hostname: str | None = None,
        options: FolderOptions | None = None,
    ) -> Service:
        """Serve a local folder as a static HTTP service."""
        trimmed = self._validate_name(name)
        fqdn = self._prepare_hostname(hostname)
        folder = self._validate_folder(Path(folder_path).expanduser())
        resolved = str(folder.resolve())
        with self._lifecycle:
            existing = self._find_service(ServiceType.FOLDER, lambda s: s.folder_path == resolved)
            if existing is not None and existing.status != ServiceStatus.STOPPED:
                raise PortAlreadySharedError(f"{resolved} is already shared as '{existing.name}'")
            service = existing or Service(
                id=str(uuid.uuid4()),
                name=trimmed,
                type=ServiceType.FOLDER,
                folder_path=resolved,
                assigned_port=0,
                lan_url="",
            )
            with self._lock:
                service.name = trimmed
                service.hostname = fqdn
                service.folder_options = options or FolderOptions()
            self._start_folder_service(service, Path(resolved))
            self._register(service)
        logger.info(f"Serving folder {resolved} as {service.lan_url}")
        return service

    def stop_service(self, service_id: str) -> None:
        """Tear a service's engine down but keep it listed so it can restart."""
        with self._lifecycle:
            service = self._require(service_id)
            self._teardown(service)
            with self._lock:
                service.status = ServiceStatus.STOPPED
                service.connected_devices = 0
                service.backend_hints = ()
            self._refresh_rewrite_map()
        self._notify(EVENT_SERVICES_CHANGED)
        logger.info(f"Service '{service.name}' stopped")

    def restart_service(self, service_id: str) -> None:
        """Start a stopped or failed service again with its saved settings."""
        with self._lifecycle:
            service = self._require(service_id)
            if service.status == ServiceStatus.RUNNING:
                return
            self._teardown(service)
            try:
                if service.type == ServiceType.PORT:
                    self._start_port_service(service)
                else:
                    if service.folder_path is None:
                        raise FolderInvalidError("Service has no stored folder.")
                    folder = self._validate_folder(Path(service.folder_path))
                    self._start_folder_service(service, folder)
            except ServiceError as exc:
                with self._lock:
                    service.status = ServiceStatus.STOPPED
                    service.last_error = exc.user_message
                self._notify(EVENT_SERVICES_CHANGED)
                raise
            self._persist(service)
            self._refresh_rewrite_map()
        self._notify(EVENT_SERVICES_CHANGED)
        logger.info(f"Service '{service.name}' restarted at {service.lan_url}")

    def remove_service(self, service_id: str) -> None:
        """Stop a service and forget it, including its saved record."""
        with self._lifecycle:
            service = self._require(service_id)
            self._teardown(service)
            self._listen_hosts.pop(service_id, None)
            self._upstream_hosts.pop(service_id, None)
            with self._lock:
                self._services.pop(service_id, None)
                config = self._config_store.load()
                config.saved_services = [s for s in config.saved_services if s.id != service_id]
                self._config_store.save(config)
            self._refresh_rewrite_map()
        self._notify(EVENT_SERVICES_CHANGED)
        logger.info(f"Service '{service.name}' removed")

    def share_backend(self, service_id: str, port: int) -> Service:
        """Share a backend the given service calls, under the same custom link."""
        service = self._require(service_id)
        hint = next((h for h in service.backend_hints if h.port == port), None)
        label = hint.label if hint and hint.label else f"port {port}"
        hostname = service.hostname.removesuffix(".local") if service.hostname else None
        return self.expose_port(f"{service.name} · {label}", port, hostname=hostname)

    def restore_saved_services(self) -> None:
        """List services from previous sessions as STOPPED, ready to restart.

        Older versions saved a new record on every share; only the newest
        record per port or folder is kept, and the cleaned list is saved.
        """
        with self._lock:
            config = self._config_store.load()
            unique: dict[tuple[ServiceType, str], SavedService] = {}
            for saved in reversed(config.saved_services):
                key = (
                    saved.type,
                    str(saved.port if saved.type == ServiceType.PORT else saved.folder_path),
                )
                unique.setdefault(key, saved)
            kept = list(reversed(unique.values()))
            if len(kept) != len(config.saved_services):
                config.saved_services = kept
                self._config_store.save(config)
            for saved in kept:
                if saved.id in self._services:
                    continue
                self._services[saved.id] = Service(
                    id=saved.id,
                    name=saved.name,
                    type=saved.type,
                    assigned_port=0,
                    lan_url="",
                    port=saved.port,
                    folder_path=saved.folder_path,
                    auto_start=saved.auto_start,
                    hostname=saved.hostname,
                    preferred_scheme=saved.share_scheme,
                    folder_options=saved.folder_options,
                )
        self._notify(EVENT_SERVICES_CHANGED)

    # ---- configuration passthrough ----

    def set_network_interface(self, interface: str | None) -> None:
        """Persist the preferred network interface (None = automatic)."""
        with self._lock:
            config = self._config_store.load()
            config.network_interface = interface
            self._config_store.save(config)
            self._interface_override = interface

    def set_smart_proxy(self, enabled: bool) -> None:
        """Turn HTTP-aware rewriting on or off (applies to new connections)."""
        with self._lock:
            config = self._config_store.load()
            config.smart_proxy = enabled
            self._config_store.save(config)
        self._proxy_engine.set_smart_mode(enabled)

    def clear_saved_services(self) -> None:
        """Drop persisted records and forget stopped services."""
        with self._lock:
            config = self._config_store.load()
            config.saved_services = []
            self._config_store.save(config)
            for service_id, service in list(self._services.items()):
                if service.status == ServiceStatus.STOPPED:
                    del self._services[service_id]
            remaining = list(self._services.values())
        for service in remaining:
            self._persist(service)
        self._notify(EVENT_SERVICES_CHANGED)

    def current_lan_ip(self) -> str:
        """Resolve the LAN IP, honouring a configured interface override."""
        interface = self._interface_override
        if interface is not None:
            for info in self._detector.list_interfaces():
                if info.name == interface:
                    return info.ip
            logger.warning(f"Configured interface {interface} missing; auto-detecting")
        return self._detector.get_lan_ip()

    def cached_lan_ip(self) -> str:
        """Last LAN IP seen by the manager (cheap; safe for UI code)."""
        if self._lan_ip is None:
            self._lan_ip = self.current_lan_ip()
        return self._lan_ip

    # ---- monitoring ----

    def start_monitor(self) -> None:
        """Start the background health monitor (safe to call once)."""
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="xposer-monitor", daemon=True
        )
        self._monitor_thread.start()

    def shutdown(self) -> None:
        """Stop the monitor and tear every engine down. Call at app exit."""
        self._monitor_stop.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=self._monitor_interval + 1.0)
            self._monitor_thread = None
        self._proxy_engine.stop_all()
        self._file_server.stop_all()
        self._mdns.stop_all()
        logger.info("ServiceManager shut down")

    # ---- internals: starting ----

    def _start_port_service(self, service: Service) -> None:
        """Probe the local app, start its proxy, publish links, render QR codes."""
        if service.port is None:
            raise PortNotListeningError("Service has no stored port.")
        probe = self._detector.probe_upstream(service.port, self._upstream_hosts.get(service.id))
        if not probe.reachable or probe.host is None:
            raise PortNotListeningError(f"Nothing listens on localhost:{service.port}")
        protocol, scheme = _classify(probe, service.preferred_scheme)
        hosts = [probe.host] + [host for host in LOOPBACK_HOSTS if host != probe.host]
        lan_ip = self.current_lan_ip()
        listen_host, assigned_port = self._bind_proxy(
            service, service.port, lan_ip, hosts, protocol
        )
        self._upstream_hosts[service.id] = probe.host
        self._listen_hosts[service.id] = listen_host
        with self._lock:
            service.assigned_port = assigned_port
            service.local_protocol = protocol
            service.share_scheme = scheme
        self._lan_ip = lan_ip
        self._apply_links(service, lan_ip)

    def _bind_proxy(
        self,
        service: Service,
        local_port: int,
        lan_ip: str,
        hosts: list[str],
        protocol: LocalProtocol,
    ) -> tuple[str, int]:
        """Start the proxy on the best free address; returns (host, port)."""
        last_error = "No free network port available."
        for listen_host, port in self._proxy_candidates(local_port, service.assigned_port, lan_ip):
            try:
                self._proxy_engine.start_proxy(
                    service.id,
                    listen_host,
                    port,
                    local_port,
                    upstream_hosts=hosts,
                    upstream_tls=protocol == LocalProtocol.HTTPS,
                    raw_mode=protocol == LocalProtocol.TCP,
                    service_name=service.name,
                )
            except ProxyError as exc:
                last_error = str(exc)
                continue
            return listen_host, self._proxy_engine.listen_port(service.id) or port
        raise PortBindError(last_error)

    def _proxy_candidates(
        self, local_port: int, previous: int, lan_ip: str
    ) -> list[tuple[str, int]]:
        """Listen addresses in preference order.

        1. The previous link's port (links survive restarts).
        2. The app's own port on the LAN IP: http://192.168.1.5:5173.
        3. The app's port + 10000 on all interfaces.
        4. A free port from Xposer's quiet range, then any free port.
        """
        candidates: list[tuple[str, int]] = []
        lan_usable = not is_loopback(lan_ip)
        if previous:
            same_port = previous == local_port
            if not same_port or lan_usable:
                candidates.append((lan_ip if same_port else LISTEN_HOST_ANY, previous))
        if lan_usable:
            candidates.append((lan_ip, local_port))
        alternate = local_port + ALTERNATE_PORT_OFFSET
        if alternate <= MAX_PORT:
            candidates.append((LISTEN_HOST_ANY, alternate))
        free = self._detector.find_available_port(host=LISTEN_HOST_ANY)
        if free is not None:
            candidates.append((LISTEN_HOST_ANY, free))
        candidates.append((LISTEN_HOST_ANY, OS_ASSIGNED_PORT))
        return list(dict.fromkeys(candidates))

    def _start_folder_service(self, service: Service, folder: Path) -> None:
        """Start the file server on a stable port and publish links."""
        lan_ip = self.current_lan_ip()
        last_error = "No free network port available."
        chosen: int | None = None
        for port in self._folder_port_candidates(str(folder), service.assigned_port):
            try:
                self._file_server.start_server(service.id, folder, port, service.folder_options)
            except FileServerError as exc:
                last_error = str(exc)
                continue
            chosen = port
            break
        if chosen is None:
            raise PortBindError(last_error)
        self._listen_hosts[service.id] = LISTEN_HOST_ANY
        with self._lock:
            service.assigned_port = chosen
            service.share_scheme = ShareScheme.HTTP
            service.local_protocol = LocalProtocol.HTTP
        self._lan_ip = lan_ip
        self._apply_links(service, lan_ip)

    def _folder_port_candidates(self, folder_path: str, previous: int) -> list[int]:
        """Previous port, then a port derived from the folder path, then any."""
        stable = SCAN_RANGE_START + zlib.crc32(folder_path.encode("utf-8")) % FOLDER_PORT_SPAN
        candidates = [previous] if previous else []
        candidates.append(stable)
        free = self._detector.find_available_port(host=LISTEN_HOST_ANY)
        if free is not None:
            candidates.append(free)
        return list(dict.fromkeys(candidates))

    def _apply_links(self, service: Service, lan_ip: str) -> None:
        """Publish the custom link (if any), render QR codes, mark RUNNING.

        The IP link is always valid; the .local link is used as the primary
        link only once mDNS publishing succeeded, so a failed publish never
        leaves a dead link on screen.
        """
        ip_url = build_url(service.share_scheme, lan_ip, service.assigned_port)
        active = False
        if service.hostname is not None:
            active = self._mdns.publish(service.hostname, lan_ip, service.assigned_port)
            if not active:
                logger.warning(f"Could not publish {service.hostname}; using {ip_url}")
        with self._lock:
            service.hostname_active = active
        self._render_links(service, ip_url)
        with self._lock:
            service.status = ServiceStatus.RUNNING
            service.last_error = ""

    def _render_links(self, service: Service, ip_url: str) -> None:
        """Generate QR images first, then commit every link field at once.

        Committing together means the UI never shows a new link next to an
        old QR code.
        """
        name_url = None
        if service.hostname_active and service.hostname is not None:
            name_url = build_url(service.share_scheme, service.hostname, service.assigned_port)
        ip_qr = str(self._qr_generator.generate(service.name, ip_url))
        name_qr = str(self._qr_generator.generate(service.name, name_url)) if name_url else None
        with self._lock:
            service.ip_url = ip_url
            service.lan_url = name_url or ip_url
            service.qr_image_path = ip_qr
            service.qr_custom_path = name_qr

    def _register(self, service: Service) -> None:
        """Record, persist, refresh the share map, and announce a service."""
        with self._lock:
            self._services[service.id] = service
        self._persist(service)
        self._refresh_rewrite_map()
        self._notify(EVENT_SERVICES_CHANGED)

    # ---- internals: stopping ----

    def _teardown(self, service: Service) -> None:
        """Stop the engine and withdraw the custom link; idempotent."""
        if service.type == ServiceType.PORT:
            self._proxy_engine.stop_proxy(service.id)
        else:
            self._file_server.stop_server(service.id)
        if service.hostname_active and service.hostname is not None:
            self._mdns.unpublish(service.hostname)
        with self._lock:
            service.hostname_active = False

    # ---- internals: helpers ----

    def _require(self, service_id: str) -> Service:
        """Service by id or ServiceNotFoundError."""
        with self._lock:
            service = self._services.get(service_id)
        if service is None:
            raise ServiceNotFoundError(service_id)
        return service

    def _find_service(
        self, service_type: ServiceType, predicate: Callable[[Service], bool]
    ) -> Service | None:
        """First service of a type matching predicate."""
        with self._lock:
            for service in self._services.values():
                if service.type == service_type and predicate(service):
                    return service
        return None

    @staticmethod
    def _validate_name(name: str) -> str:
        """Trimmed name or ValidationError."""
        trimmed = name.strip()
        if not trimmed:
            raise ValidationError("Service name must not be empty.")
        return trimmed

    @staticmethod
    def _validate_folder(folder: Path) -> Path:
        """Existing, non-empty folder or FolderInvalidError."""
        if not folder.is_dir():
            raise FolderInvalidError(f"No such folder: {folder}")
        try:
            if not any(folder.iterdir()):
                raise FolderInvalidError(f"Folder is empty: {folder}")
        except PermissionError as exc:
            raise FolderInvalidError(f"Folder is not readable: {folder}") from exc
        return folder

    @staticmethod
    def _prepare_hostname(hostname: str | None) -> str | None:
        """Normalize optional user input into a .local name (None if blank)."""
        if hostname is None or not hostname.strip():
            return None
        try:
            return to_local_name(normalize_hostname(hostname))
        except ValueError as exc:
            raise ValidationError(
                "Hostnames may only contain letters, numbers, and hyphens."
            ) from exc

    def _persist(self, service: Service) -> None:
        """Upsert the service's saved record into the config file.

        The load-modify-save cycle runs under the manager lock so two
        services exposed concurrently cannot drop each other's record.
        """
        with self._lock:
            config = self._config_store.load()
            saved = service.to_saved()
            config.saved_services = [s for s in config.saved_services if s.id != saved.id]
            config.saved_services.append(saved)
            self._config_store.save(config)

    def _refresh_rewrite_map(self) -> None:
        """Publish every shared HTTP(S) port's public address to the proxy."""
        lan_ip = self._lan_ip or self.current_lan_ip()
        with self._lock:
            services = [
                s
                for s in self._services.values()
                if s.type == ServiceType.PORT
                and s.port is not None
                and s.share_scheme != ShareScheme.TCP
                and s.status != ServiceStatus.STOPPED
            ]
        endpoints = [
            PublicEndpoint(
                local_port=s.port,
                public_port=s.assigned_port,
                ip_host=lan_ip,
                name_host=s.hostname if s.hostname_active else None,
                public_scheme=s.share_scheme.value,
                upstream_tls=s.local_protocol == LocalProtocol.HTTPS,
            )
            for s in services
            if s.port is not None
        ]
        if self._proxy_engine.rewrite_map().same_endpoints(endpoints):
            return
        self._map_version += 1
        self._proxy_engine.set_rewrite_map(RewriteMap(endpoints, self._map_version))

    def _scan_ports(self, force: bool = False) -> dict[int, ListeningPort]:
        """Listening ports by number, cached briefly for the monitor."""
        now = time.monotonic()
        if (
            not force
            and self._port_scan is not None
            and now - self._port_scan[0] < PORT_SCAN_CACHE_SECONDS
        ):
            return self._port_scan[1]
        ports = {
            entry.port: entry
            for entry in self._detector.detect_listening_ports(self._own_listeners())
        }
        self._port_scan = (now, ports)
        return ports

    # ---- internals: monitoring ----

    def _monitor_loop(self) -> None:
        """Poll health, traffic, backend hints and the LAN IP until shutdown."""
        while not self._monitor_stop.wait(self._monitor_interval):
            try:
                changed = self._check_lan_ip()
                changed |= self._refresh_services()
            except Exception as exc:  # the monitor must survive any single failure
                logger.warning(f"Monitor tick failed: {exc}")
                continue
            if changed:
                self._notify(EVENT_SERVICES_CHANGED)

    def _refresh_services(self) -> bool:
        """Update traffic counters, health and backend hints; True if changed."""
        with self._lock:
            services = [s for s in self._services.values() if s.status != ServiceStatus.STOPPED]
        changed = False
        for service in services:
            devices, requests, unresolved = self._traffic(service)
            healthy = self._is_healthy(service)
            hints = self._backend_hints(service, unresolved) if unresolved else ()
            with self._lock:
                if service.status == ServiceStatus.STOPPED:
                    continue
                if (service.connected_devices, service.requests_served) != (devices, requests):
                    service.connected_devices = devices
                    service.requests_served = requests
                    changed = True
                if service.backend_hints != hints:
                    service.backend_hints = hints
                    changed = True
                changed |= self._apply_health(service, healthy)
        return changed

    def _traffic(self, service: Service) -> tuple[int, int, frozenset[int]]:
        """(devices, requests, unresolved localhost ports) for a service."""
        if service.type == ServiceType.FOLDER:
            return (
                self._file_server.connected_devices(service.id),
                service.requests_served,
                frozenset(),
            )
        stats = self._proxy_engine.stats(service.id)
        if stats is None:
            return 0, service.requests_served, frozenset()
        return stats.connected_devices, stats.requests, stats.unresolved_ports

    def _backend_hints(
        self, service: Service, unresolved: frozenset[int]
    ) -> tuple[BackendHint, ...]:
        """Unshared local ports this app links to that are actually running."""
        shared = self.shared_local_ports()
        candidates = sorted(
            port for port in unresolved if port not in shared and port != service.port
        )
        listening = [
            port
            for port in candidates[:MAX_BACKEND_HINTS]
            if self._detector.is_port_listening(port)
        ]
        if not listening:
            return ()
        scan = self._scan_ports()
        return tuple(
            BackendHint(port=port, label=scan[port].app_label if port in scan else "")
            for port in listening
        )

    def _is_healthy(self, service: Service) -> bool:
        """Engine health, independent of recorded status (so recovery is seen)."""
        if service.type == ServiceType.PORT:
            if service.port is None or not self._proxy_engine.is_running(service.id):
                return False
            preferred = self._upstream_hosts.get(service.id)
            return self._detector.is_port_listening(service.port, [preferred] if preferred else [])
        return self._file_server.is_running(service.id)

    def _apply_health(self, service: Service, healthy: bool) -> bool:
        """Transition one service's status from a health reading."""
        if not healthy and service.status == ServiceStatus.RUNNING:
            service.status = ServiceStatus.ERROR
            service.last_error = "The app stopped answering."
            logger.warning(f"Service '{service.name}' reported unhealthy")
            return True
        if healthy and service.status == ServiceStatus.ERROR and service.type == ServiceType.PORT:
            service.status = ServiceStatus.RUNNING
            service.last_error = ""
            logger.info(f"Service '{service.name}' recovered automatically")
            return True
        return False

    def _check_lan_ip(self) -> bool:
        """Follow LAN address changes (new Wi-Fi, DHCP renewal); True if changed.

        Losing the network entirely (only loopback left) is not a change:
        links keep their address so they work again when Wi-Fi returns.
        """
        new_ip = self.current_lan_ip()
        old_ip = self._lan_ip
        if is_loopback(new_ip) and old_ip is not None:
            return False
        if old_ip is None or new_ip == old_ip:
            self._lan_ip = new_ip
            return False
        logger.info(f"LAN address changed from {old_ip} to {new_ip}")
        with self._lifecycle:
            self._lan_ip = new_ip
            with self._lock:
                services = [s for s in self._services.values() if s.status != ServiceStatus.STOPPED]
            for service in services:
                self._follow_new_ip(service, new_ip)
            self._mdns.update_ip(new_ip)
            self._refresh_rewrite_map()
        return True

    def _follow_new_ip(self, service: Service, new_ip: str) -> None:
        """Rebind a proxy tied to the old address and refresh links and QR."""
        if (
            service.type == ServiceType.PORT
            and service.port is not None
            and self._listen_hosts.get(service.id) != LISTEN_HOST_ANY
        ):
            self._proxy_engine.stop_proxy(service.id)
            preferred = self._upstream_hosts.get(service.id, LOOPBACK_HOSTS[0])
            hosts = list(dict.fromkeys([preferred, *LOOPBACK_HOSTS]))
            try:
                listen_host, port = self._bind_proxy(
                    service, service.port, new_ip, hosts, service.local_protocol
                )
            except PortBindError as exc:
                with self._lock:
                    service.status = ServiceStatus.ERROR
                    service.last_error = f"Could not reopen the link: {exc}"
                return
            self._listen_hosts[service.id] = listen_host
            with self._lock:
                service.assigned_port = port
        self._render_links(service, build_url(service.share_scheme, new_ip, service.assigned_port))


def _classify(
    probe: UpstreamProbe, preferred: ShareScheme | None
) -> tuple[LocalProtocol, ShareScheme]:
    """Local protocol and the scheme LAN devices should use."""
    if probe.kind.is_raw:
        return LocalProtocol.TCP, ShareScheme.TCP
    if probe.kind == UpstreamKind.TLS:
        scheme = ShareScheme.HTTPS if preferred == ShareScheme.HTTPS else ShareScheme.HTTP
        return LocalProtocol.HTTPS, scheme
    return LocalProtocol.HTTP, ShareScheme.HTTP

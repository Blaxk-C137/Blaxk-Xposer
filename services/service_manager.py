"""Single source of truth for service lifecycle and state.

Every mutation of service state flows through ServiceManager. Windows and
the tray observe it; engines (ProxyEngine, FileServer) are owned siblings
and never talk to each other or to UI code.
"""

import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from config.config_store import ConfigStore
from models.service import Service, ServiceStatus, ServiceType
from network.mdns_publisher import MdnsPublisher, normalize_hostname, to_local_name
from network.network_detector import NetworkDetector
from network.qr_generator import QRGenerator
from services.file_server import FileServer, FileServerError
from services.proxy_engine import LISTEN_HOST_ANY, ProxyEngine, ProxyError

from utils.logger import get_logger

logger = get_logger("services")

EVENT_SERVICES_CHANGED = "services_changed"
MONITOR_INTERVAL_SECONDS = 5.0
SENSITIVE_FOLDERS: tuple[Path, ...] = (
    Path("/"),
    Path("/bin"),
    Path("/boot"),
    Path("/etc"),
    Path("/lib"),
    Path("/lib64"),
    Path("/sbin"),
    Path("/usr"),
    Path("/var"),
)


class ServiceError(Exception):
    """Base class for errors with a human-readable message."""

    user_message = "Something went wrong while managing the service."

    def __init__(self, detail: str) -> None:
        super().__init__(detail)


class ValidationError(ServiceError):
    """User input failed validation."""

    user_message = "Please check the values you entered and try again."


class PortNotListeningError(ServiceError):
    """The requested localhost port has no listener."""

    user_message = (
        "That port is not running on this machine. Start your app first, "
        "then expose it."
    )


class PortBindError(ServiceError):
    """A network port could not be opened."""

    user_message = "Xposer could not open a network port. It may already be in use."


class FolderInvalidError(ServiceError):
    """The chosen folder does not exist or is empty."""

    user_message = "That folder does not exist or is empty."


class ServiceNotFoundError(ServiceError):
    """The referenced service is no longer registered."""

    user_message = "That service no longer exists."


class ServiceManager:
    """Registry and lifecycle owner for every exposed service."""

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
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()

    # ---- observation ----

    def add_observer(self, callback: Callable[[str], None]) -> None:
        """Register callback(event_name); called from arbitrary threads."""
        self._observers.append(callback)

    def _notify(self, event: str) -> None:
        for observer in list(self._observers):
            try:
                observer(event)
            except Exception as exc:
                logger.warning(f"Observer raised during {event}: {exc}")

    # ---- queries ----

    def get_service(self, service_id: str) -> Service | None:
        """Return the live Service record for service_id, or None."""
        with self._lock:
            return self._services.get(service_id)

    def active_services(self) -> list[Service]:
        """Snapshot of services currently RUNNING."""
        with self._lock:
            return [
                service
                for service in self._services.values()
                if service.status == ServiceStatus.RUNNING
            ]

    def all_services(self) -> list[Service]:
        """Snapshot of every service known this session."""
        with self._lock:
            return list(self._services.values())

    def active_ports(self) -> set[int]:
        """Assigned ports currently held by session services."""
        with self._lock:
            return {
                service.assigned_port
                for service in self._services.values()
                if service.status == ServiceStatus.RUNNING
            }

    def lan_reachable(self, service: Service) -> bool:
        """True when the assigned port answers on the current LAN IP."""
        return self._detector.is_port_in_use(
            service.assigned_port, host=self.current_lan_ip()
        )

    def is_sensitive_folder(self, folder: Path) -> bool:
        """True when folder is home or a system directory worth warning about."""
        resolved = folder.expanduser().resolve()
        sensitive = {path.resolve() for path in SENSITIVE_FOLDERS}
        sensitive.add(Path.home())
        return resolved in sensitive

    # ---- lifecycle ----

    def expose_port(
        self, name: str, port: int, hostname: str | None = None
    ) -> Service:
        """Expose an already-running localhost port to the LAN."""
        trimmed = name.strip()
        if not trimmed:
            raise ValidationError("Service name must not be empty.")
        if not self._detector.validate_port(port):
            raise ValidationError("Port must be between 1 and 65535.")
        if not self._detector.is_port_in_use(port):
            raise PortNotListeningError(f"Nothing listens on localhost:{port}")
        fqdn = self._prepare_hostname(hostname)

        lan_ip = self.current_lan_ip()
        listen_host, assigned_port = self._pick_proxy_binding(port, lan_ip)
        service = Service(
            id=str(uuid.uuid4()),
            name=trimmed,
            type=ServiceType.PORT,
            port=port,
            assigned_port=assigned_port,
            lan_url=f"http://{lan_ip}:{assigned_port}",
            hostname=fqdn,
        )
        try:
            self._proxy_engine.start_proxy(
                service.id, listen_host, assigned_port, port
            )
        except ProxyError as exc:
            raise PortBindError(str(exc)) from exc
        self._apply_hostname_url(service, lan_ip)
        self._activate(service)
        logger.info(f"Exposed port {port} as {service.lan_url}")
        return service

    def serve_folder(
        self, name: str, folder_path: str, hostname: str | None = None
    ) -> Service:
        """Serve a local folder as a static HTTP service."""
        trimmed = name.strip()
        if not trimmed:
            raise ValidationError("Service name must not be empty.")
        fqdn = self._prepare_hostname(hostname)
        folder = Path(folder_path).expanduser()
        if not folder.is_dir():
            raise FolderInvalidError(f"No such folder: {folder}")
        if not any(folder.iterdir()):
            raise FolderInvalidError(f"Folder is empty: {folder}")

        lan_ip = self.current_lan_ip()
        assigned_port = self._detector.find_available_port()
        if assigned_port is None:
            raise PortBindError("No free network port available.")
        service = Service(
            id=str(uuid.uuid4()),
            name=trimmed,
            type=ServiceType.FOLDER,
            folder_path=str(folder),
            assigned_port=assigned_port,
            lan_url=f"http://{lan_ip}:{assigned_port}",
            hostname=fqdn,
        )
        try:
            self._file_server.start_server(service.id, folder, assigned_port)
        except FileServerError as exc:
            raise PortBindError(str(exc)) from exc
        self._apply_hostname_url(service, lan_ip)
        self._activate(service)
        logger.info(f"Serving folder {folder} as {service.lan_url}")
        return service

    def stop_service(self, service_id: str) -> None:
        """Tear a service's engine down but keep it listed so it can restart."""
        with self._lock:
            service = self._services.get(service_id)
        if service is None:
            raise ServiceNotFoundError(service_id)
        if service.type == ServiceType.PORT:
            self._proxy_engine.stop_proxy(service_id)
        else:
            self._file_server.stop_server(service_id)
        if service.hostname is not None:
            self._mdns.unpublish(service.hostname)
        with self._lock:
            service.status = ServiceStatus.STOPPED
            service.connected_devices = 0
        self._notify(EVENT_SERVICES_CHANGED)
        logger.info(f"Service '{service.name}' stopped")

    def restart_service(self, service_id: str) -> None:
        """Restart a stopped service using its original configuration."""
        with self._lock:
            service = self._services.get(service_id)
        if service is None:
            raise ServiceNotFoundError(service_id)
        if service.status == ServiceStatus.RUNNING:
            return
        if service.type == ServiceType.PORT:
            if service.port is None:
                raise PortNotListeningError("Service has no stored port.")
            if not self._detector.is_port_in_use(service.port):
                raise PortNotListeningError(
                    f"Nothing listens on localhost:{service.port}"
                )
            listen_host, assigned_port = self._pick_proxy_binding(service.port)
            try:
                self._proxy_engine.stop_proxy(service_id)
                self._proxy_engine.start_proxy(
                    service_id, listen_host, assigned_port, service.port
                )
            except ProxyError as exc:
                raise PortBindError(str(exc)) from exc
        else:
            if service.folder_path is None:
                raise FolderInvalidError("Service has no stored folder.")
            folder = Path(service.folder_path)
            if not folder.is_dir():
                raise FolderInvalidError(f"No such folder: {folder}")
            assigned_port = self._detector.find_available_port()
            if assigned_port is None:
                raise PortBindError("No free network port available.")
            try:
                self._file_server.stop_server(service_id)
                self._file_server.start_server(service_id, folder, assigned_port)
            except FileServerError as exc:
                raise PortBindError(str(exc)) from exc
        lan_ip = self.current_lan_ip()
        with self._lock:
            service.assigned_port = assigned_port
        if service.hostname is not None:
            self._apply_hostname_url(service, lan_ip)
        else:
            service.lan_url = f"http://{lan_ip}:{assigned_port}"
        qr_image_path = str(
            self._qr_generator.generate(service.name, service.lan_url)
        )
        with self._lock:
            service.qr_image_path = qr_image_path
            service.status = ServiceStatus.RUNNING
        self._persist(service)
        self._notify(EVENT_SERVICES_CHANGED)
        logger.info(f"Service '{service.name}' restarted at {service.lan_url}")

    # ---- configuration passthrough ----

    def set_network_interface(self, interface: str | None) -> None:
        """Persist the preferred network interface (None = automatic)."""
        config = self._config_store.load()
        config.network_interface = interface
        self._config_store.save(config)

    def clear_saved_services(self) -> None:
        """Drop all persisted service records."""
        config = self._config_store.load()
        config.saved_services = []
        self._config_store.save(config)

    def current_lan_ip(self) -> str:
        """Resolve the LAN IP, honouring a configured interface override."""
        interface = self._config_store.load().network_interface
        if interface is None:
            return self._detector.get_lan_ip()
        for info in self._detector.list_interfaces():
            if info.name == interface:
                return info.ip
        logger.warning(f"Configured interface {interface} missing; auto-detecting")
        return self._detector.get_lan_ip()

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

    # ---- internals ----

    def _prepare_hostname(self, hostname: str | None) -> str | None:
        """Normalize optional user input into a .local name (None if blank)."""
        if hostname is None or not hostname.strip():
            return None
        try:
            return to_local_name(normalize_hostname(hostname))
        except ValueError as exc:
            raise ValidationError(
                "Hostnames may only contain letters, numbers, and hyphens."
            ) from exc

    def _apply_hostname_url(self, service: Service, lan_ip: str) -> None:
        """Rewrite the URL for a service with a hostname.

        The IP URL is always rebuilt first (with the service's current
        assigned port), then upgraded to hostname form only when mDNS
        publishing succeeds. A failed publish (no avahi, name rejected
        or taken) therefore leaves a correct, reachable IP URL — never
        a stale hostname URL from a previous run.
        """
        if service.hostname is None:
            return
        service.lan_url = f"http://{lan_ip}:{service.assigned_port}"
        if self._mdns.publish(service.hostname, lan_ip):
            service.lan_url = f"http://{service.hostname}:{service.assigned_port}"
        else:
            logger.warning(
                f"Could not publish {service.hostname}; using {service.lan_url}"
            )

    def _activate(self, service: Service) -> None:
        """Finish registration shared by both modes: QR, registry, notify."""
        qr_image_path = str(
            self._qr_generator.generate(service.name, service.lan_url)
        )
        with self._lock:
            service.qr_image_path = qr_image_path
            service.status = ServiceStatus.RUNNING
            self._services[service.id] = service
        self._persist(service)
        self._notify(EVENT_SERVICES_CHANGED)

    def _pick_proxy_binding(
        self, target_port: int, lan_ip: str | None = None
    ) -> tuple[str, int]:
        """Prefer binding the LAN IP on the same port; else any-host free port."""
        if lan_ip is None:
            lan_ip = self.current_lan_ip()
        if self._detector.is_port_bindable(target_port, host=lan_ip):
            return lan_ip, target_port
        assigned = self._detector.find_available_port()
        if assigned is None:
            raise PortBindError("No free network port available.")
        return LISTEN_HOST_ANY, assigned

    def _persist(self, service: Service) -> None:
        """Upsert the service's saved record into the config file.

        The load-modify-save cycle runs under the manager lock so two
        services exposed concurrently cannot drop each other's record.
        """
        with self._lock:
            config = self._config_store.load()
            saved = service.to_saved()
            config.saved_services = [
                existing
                for existing in config.saved_services
                if existing.id != saved.id
            ]
            config.saved_services.append(saved)
            self._config_store.save(config)

    def _is_healthy(self, service: Service) -> bool:
        """Report engine health regardless of recorded status.

        Health is deliberately independent of status so the monitor can
        both detect failure and observe recovery on later ticks.
        """
        if service.type == ServiceType.PORT:
            return (
                self._proxy_engine.is_running(service.id)
                and service.port is not None
                and self._detector.is_port_in_use(service.port)
            )
        return self._file_server.is_running(service.id)

    def _current_devices(self, service: Service) -> int:
        if service.status != ServiceStatus.RUNNING:
            return 0
        if service.type == ServiceType.PORT:
            return self._proxy_engine.connected_devices(service.id)
        return self._file_server.connected_devices(service.id)

    def _monitor_loop(self) -> None:
        """Poll health and device counts until shutdown is requested.

        A running port proxy whose upstream dies flips to ERROR; when the
        upstream comes back the same proxy is serving again, so it returns
        to RUNNING. Folder servers whose thread died stay ERROR — the
        engine is gone and only an explicit restart can revive them.
        """
        while not self._monitor_stop.wait(self._monitor_interval):
            with self._lock:
                services = list(self._services.values())
            changed = False
            for service in services:
                devices = self._current_devices(service)
                healthy = self._is_healthy(service)
                with self._lock:
                    if service.connected_devices != devices:
                        service.connected_devices = devices
                        changed = True
                    changed |= self._apply_health(service, healthy)
            if changed:
                self._notify(EVENT_SERVICES_CHANGED)

    def _apply_health(self, service: Service, healthy: bool) -> bool:
        """Transition one service's status from a health reading."""
        if not healthy and service.status == ServiceStatus.RUNNING:
            service.status = ServiceStatus.ERROR
            logger.warning(f"Service '{service.name}' reported unhealthy")
            return True
        if (
            healthy
            and service.status == ServiceStatus.ERROR
            and service.type == ServiceType.PORT
        ):
            service.status = ServiceStatus.RUNNING
            logger.info(f"Service '{service.name}' recovered automatically")
            return True
        return False

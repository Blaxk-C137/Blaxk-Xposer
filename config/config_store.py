"""Read and write the Xposer configuration file.

ConfigStore is the only component that touches disk for configuration,
persistence, or autostart state.
"""

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from models.service import SavedService

from utils.logger import get_logger

CONFIG_VERSION = "1.0.0"
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "xposer"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.json"
MAIN_ENTRY_PATH = Path(__file__).resolve().parent.parent / "main.py"
AUTOSTART_DIR = Path.home() / ".config" / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "xposer.desktop"

AUTOSTART_DESKTOP_ENTRY_TEMPLATE = """[Desktop Entry]
Type=Application
Name=Xposer
Comment=Expose local ports and folders over WiFi
Exec={exec_command}
Terminal=false
Categories=Network;
"""

logger = get_logger("config")


@dataclass
class AppConfig:
    """Top-level application configuration."""

    version: str = CONFIG_VERSION
    network_interface: str | None = None
    start_on_login: bool = False
    saved_services: list[SavedService] = field(default_factory=list)


class ConfigStore:
    """Loads and saves the JSON configuration file.

    All disk access is serialized: save() swaps a temp file into place, so
    two unsynchronized saves would otherwise share one temp path and tear
    the file. Callers doing load-modify-save still need their own
    coordination for lost-update avoidance (see ServiceManager._persist).
    """

    def __init__(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """Location of the config file."""
        return self._path

    def load(self) -> AppConfig:
        """Load configuration; missing or corrupt files yield defaults."""
        with self._lock:
            if not self._path.exists():
                return AppConfig()
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning(f"Could not read config {self._path}: {exc}")
                return AppConfig()
        if not isinstance(raw, dict):
            logger.warning(f"Config root is not an object: {self._path}")
            return AppConfig()
        return self._parse(raw)

    def save(self, config: AppConfig) -> None:
        """Atomically write configuration to disk."""
        payload = json.dumps(self._dump(config), indent=2)
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(self._path)

    def set_start_on_login(self, enabled: bool) -> None:
        """Install or remove the XDG autostart desktop entry."""
        if enabled:
            AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
            AUTOSTART_FILE.write_text(self._desktop_entry_text(), encoding="utf-8")
        else:
            AUTOSTART_FILE.unlink(missing_ok=True)

    @staticmethod
    def _desktop_entry_text() -> str:
        """Build the desktop entry launching Xposer from its real location.

        The path is double-quoted per the Desktop Entry spec so installs
        under directories containing spaces still resolve.
        """
        exec_command = f'python3 "{MAIN_ENTRY_PATH}"'
        return AUTOSTART_DESKTOP_ENTRY_TEMPLATE.format(exec_command=exec_command)

    def _parse(self, raw: dict[str, object]) -> AppConfig:
        saved: list[SavedService] = []
        entries = raw.get("saved_services", [])
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                try:
                    saved.append(SavedService.from_dict(entry))
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning(f"Skipping malformed saved service: {exc}")
        interface = raw.get("network_interface")
        return AppConfig(
            version=str(raw.get("version", CONFIG_VERSION)),
            network_interface=str(interface) if interface is not None else None,
            start_on_login=bool(raw.get("start_on_login", False)),
            saved_services=saved,
        )

    @staticmethod
    def _dump(config: AppConfig) -> dict[str, object]:
        return {
            "version": config.version,
            "network_interface": config.network_interface,
            "start_on_login": config.start_on_login,
            "saved_services": [saved.to_dict() for saved in config.saved_services],
        }

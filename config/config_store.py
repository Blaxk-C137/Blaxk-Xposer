"""Read and write the Xposer configuration file.

ConfigStore is the only component that touches disk for configuration,
persistence, or autostart state (XDG desktop entry on Linux, the per-user
Run registry key on Windows).
"""

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from models.service import SavedService
from utils.logger import get_logger
from utils.platform_env import IS_LINUX, IS_WINDOWS, config_dir, gui_python_executable

CONFIG_VERSION = "1.1.0"
DEFAULT_CONFIG_DIR = config_dir()
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.json"
MAIN_ENTRY_PATH = Path(__file__).resolve().parent.parent / "main.py"
AUTOSTART_DIR = Path.home() / ".config" / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "xposer.desktop"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_RUN_VALUE = "Xposer"

AUTOSTART_DESKTOP_ENTRY_TEMPLATE = """[Desktop Entry]
Type=Application
Name=Xposer
Comment=Expose local ports and folders over WiFi
Exec={exec_command}
Terminal=false
Categories=Network;
X-GNOME-Autostart-enabled=true
"""

logger = get_logger("config")


class AutostartError(RuntimeError):
    """Raised when start-on-login cannot be changed on this system."""


@dataclass
class AppConfig:
    """Top-level application configuration."""

    version: str = CONFIG_VERSION
    network_interface: str | None = None
    start_on_login: bool = False
    saved_services: list[SavedService] = field(default_factory=list)
    smart_proxy: bool = True


def autostart_supported() -> bool:
    """True where Xposer knows how to register itself to start on login."""
    return IS_LINUX or IS_WINDOWS


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
        """Register or unregister Xposer to start when the user logs in.

        Raises AutostartError when the platform is unsupported or the
        system refuses the change.
        """
        if IS_WINDOWS:
            self._set_windows_autostart(enabled)
            return
        if not IS_LINUX:
            raise AutostartError("Start on login is not supported on this system")
        try:
            if enabled:
                AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
                AUTOSTART_FILE.write_text(self._desktop_entry_text(), encoding="utf-8")
            else:
                AUTOSTART_FILE.unlink(missing_ok=True)
        except OSError as exc:
            raise AutostartError(f"Could not update {AUTOSTART_FILE}: {exc}") from exc

    @staticmethod
    def launch_command() -> str:
        """Command line that starts this copy of Xposer without a console."""
        return f'"{gui_python_executable()}" "{MAIN_ENTRY_PATH}"'

    @classmethod
    def _desktop_entry_text(cls) -> str:
        """Build the desktop entry launching Xposer from its real location.

        Paths are double-quoted per the Desktop Entry spec so installs
        under directories containing spaces still resolve, and the running
        interpreter is used so virtualenv installs keep working.
        """
        return AUTOSTART_DESKTOP_ENTRY_TEMPLATE.format(exec_command=cls.launch_command())

    @classmethod
    def _set_windows_autostart(cls, enabled: bool) -> None:
        """Write or delete HKCU\\...\\Run\\Xposer (no admin rights needed)."""
        try:
            import winreg
        except ImportError as exc:
            raise AutostartError("Windows registry is unavailable") from exc
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                if enabled:
                    winreg.SetValueEx(
                        key, WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, cls.launch_command()
                    )
                else:
                    try:
                        winreg.DeleteValue(key, WINDOWS_RUN_VALUE)
                    except FileNotFoundError:
                        pass
        except OSError as exc:
            raise AutostartError(f"Could not update the Run registry key: {exc}") from exc

    def _parse(self, raw: dict[str, object]) -> AppConfig:
        """Build an AppConfig from parsed JSON, skipping malformed entries."""
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
            smart_proxy=bool(raw.get("smart_proxy", True)),
        )

    @staticmethod
    def _dump(config: AppConfig) -> dict[str, object]:
        """JSON-ready dictionary for an AppConfig."""
        return {
            "version": CONFIG_VERSION,
            "network_interface": config.network_interface,
            "start_on_login": config.start_on_login,
            "smart_proxy": config.smart_proxy,
            "saved_services": [saved.to_dict() for saved in config.saved_services],
        }

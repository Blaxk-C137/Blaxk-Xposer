"""Platform facts and per-user directories.

Every OS-specific path decision lives here so the rest of Xposer can ask
"where do I keep config?" without branching on sys.platform itself.
"""

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

APP_NAME = "Xposer"
POSIX_DIR_NAME = "xposer"
WINDOWS_GUI_INTERPRETER = "pythonw.exe"


def config_dir() -> Path:
    """Directory holding config.json (created lazily by ConfigStore)."""
    if IS_WINDOWS:
        return _windows_base("APPDATA", Path("AppData") / "Roaming") / APP_NAME
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return _xdg_base("XDG_CONFIG_HOME", ".config") / POSIX_DIR_NAME


def cache_dir() -> Path:
    """Directory for regenerable files: QR images, badge icons, logs."""
    if IS_WINDOWS:
        return _windows_base("LOCALAPPDATA", Path("AppData") / "Local") / APP_NAME / "Cache"
    if IS_MACOS:
        return Path.home() / "Library" / "Caches" / APP_NAME
    return _xdg_base("XDG_CACHE_HOME", ".cache") / POSIX_DIR_NAME


def gui_python_executable() -> str:
    """Interpreter that launches Xposer without opening a console window.

    On Windows that is pythonw.exe beside the running python.exe; elsewhere
    the running interpreter is already console-agnostic.
    """
    current = Path(sys.executable)
    if IS_WINDOWS:
        candidate = current.with_name(WINDOWS_GUI_INTERPRETER)
        if candidate.exists():
            return str(candidate)
    return str(current)


def _windows_base(variable: str, fallback: Path) -> Path:
    """Resolve a Windows known-folder environment variable."""
    value = os.environ.get(variable)
    return Path(value) if value else Path.home() / fallback


def _xdg_base(variable: str, fallback: str) -> Path:
    """Resolve an XDG base directory, ignoring relative values per the spec."""
    value = os.environ.get(variable)
    if value and Path(value).is_absolute():
        return Path(value)
    return Path.home() / fallback

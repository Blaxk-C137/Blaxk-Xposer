"""Shared pytest bootstrap and fixtures for the Xposer test suite."""

import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Keep logs, QR images and config of the test run out of the real home
# directory. Must happen before any Xposer module computes default paths.
_SANDBOX = Path(tempfile.mkdtemp(prefix="xposer-tests-"))
for _variable in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA"):
    os.environ[_variable] = str(_SANDBOX / _variable.lower())

import pytest

from config.config_store import ConfigStore
from network.mdns_publisher import MdnsPublisher
from network.network_detector import NetworkDetector
from network.qr_generator import QRGenerator
from services.service_manager import ServiceManager


def gtk_available() -> bool:
    """True when PyGObject with GTK 3 can be imported and a display exists."""
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
    except (ImportError, ValueError):
        return False
    return bool(Gtk.init_check()[0])


@pytest.fixture()
def components(tmp_path: Path) -> dict[str, object]:
    """Real ServiceManager stack wired to temporary directories.

    mDNS uses no backends so tests never announce names on the real
    network. The stack is shut down on teardown so engine threads and
    listening sockets never leak between tests.
    """
    config = ConfigStore(path=tmp_path / "config.json")
    detector = NetworkDetector()
    qr = QRGenerator(output_dir=tmp_path / "qr")
    manager = ServiceManager(
        config_store=config,
        detector=detector,
        qr_generator=qr,
        mdns_publisher=MdnsPublisher(backends=[]),
        monitor_interval=0.1,
    )
    components = {
        "tmp_path": tmp_path,
        "config": config,
        "detector": detector,
        "qr": qr,
        "manager": manager,
    }
    try:
        yield components
    finally:
        manager.shutdown()

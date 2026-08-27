"""Shared pytest bootstrap and fixtures for the Xposer test suite."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from config.config_store import ConfigStore
from network.network_detector import NetworkDetector
from network.qr_generator import QRGenerator
from services.service_manager import ServiceManager


@pytest.fixture()
def components(tmp_path: Path) -> dict[str, object]:
    """Real ServiceManager stack wired to temporary directories.

    Shuts the stack down on teardown so engine threads and listening
    sockets never leak between tests.
    """
    config = ConfigStore(path=tmp_path / "config.json")
    detector = NetworkDetector()
    qr = QRGenerator(output_dir=tmp_path / "qr")
    manager = ServiceManager(
        config_store=config,
        detector=detector,
        qr_generator=qr,
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

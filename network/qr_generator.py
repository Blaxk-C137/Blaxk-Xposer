"""Generate and manage QR code images for service URLs."""

import hashlib
import re
import shutil
from pathlib import Path

import qrcode

from utils.logger import get_logger

logger = get_logger("qr")

DEFAULT_QR_DIR = Path.home() / ".cache" / "xposer" / "qr"
QR_BOX_SIZE = 10
QR_BORDER = 2
FILENAME_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")
FILENAME_HASH_LENGTH = 8


class QRGenerator:
    """Creates QR code PNG files for LAN URLs."""

    def __init__(self, output_dir: Path = DEFAULT_QR_DIR) -> None:
        self._output_dir = output_dir

    def generate(self, service_name: str, url: str) -> Path:
        """Render url as a QR code PNG and return the written path.

        The filename carries a short hash of the url so two services with
        the same name can never overwrite each other's code.
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)
        image = qrcode.make(url, box_size=QR_BOX_SIZE, border=QR_BORDER)
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        suffix = digest[:FILENAME_HASH_LENGTH]
        path = self._output_dir / f"{self._safe_name(service_name)}-{suffix}.png"
        image.save(path, format="PNG")
        logger.info(f"QR code written to {path}")
        return path

    def save_copy(self, source: Path, destination: Path) -> Path:
        """Copy an existing QR image to a user-chosen location."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        logger.info(f"QR image copied to {destination}")
        return destination

    @staticmethod
    def _safe_name(service_name: str) -> str:
        slug = FILENAME_SAFE_PATTERN.sub("_", service_name.strip()).strip("_")
        return slug or "service"

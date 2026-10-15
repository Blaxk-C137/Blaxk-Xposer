"""Generate and manage QR code images for service URLs."""

import hashlib
import re
import shutil
from pathlib import Path

import qrcode

from utils.logger import get_logger
from utils.platform_env import cache_dir

logger = get_logger("qr")

DEFAULT_QR_DIR = cache_dir() / "qr"
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
        the same name can never overwrite each other's code, and an image
        that already exists for the same name and url is reused as is.
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        suffix = digest[:FILENAME_HASH_LENGTH]
        path = self._output_dir / f"{self._safe_name(service_name)}-{suffix}.png"
        if path.is_file() and path.stat().st_size > 0:
            return path
        image = qrcode.make(url, box_size=QR_BOX_SIZE, border=QR_BORDER)
        temporary = path.with_suffix(".tmp")
        image.save(temporary, format="PNG")
        temporary.replace(path)
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
        """Filesystem-safe slug of a service name."""
        slug = FILENAME_SAFE_PATTERN.sub("_", service_name.strip()).strip("_")
        return slug or "service"

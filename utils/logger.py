"""Centralized logging setup for Xposer.

Logs go to stderr when there is one and always to a small rotating file in
the cache directory. The file matters on Windows: pythonw.exe has no
console, so without it every warning would vanish.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from utils.platform_env import cache_dir

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE_NAME = "xposer.log"
LOG_FILE_MAX_BYTES = 512 * 1024
LOG_FILE_BACKUPS = 2


def log_file_path() -> Path:
    """Location of the rotating log file."""
    return cache_dir() / LOG_FILE_NAME


def setup_logging(level: int = logging.INFO, to_file: bool = True) -> logging.Logger:
    """Configure the xposer root logger and return it.

    Safe to call repeatedly: handlers are attached exactly once.
    """
    logger = logging.getLogger("xposer")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        logger.addHandler(console)
    if to_file:
        _attach_file_handler(logger, formatter)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the xposer namespace."""
    return logging.getLogger(f"xposer.{name}")


def _attach_file_handler(logger: logging.Logger, formatter: logging.Formatter) -> None:
    """Add the rotating file handler; an unwritable cache dir is not fatal."""
    path = log_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUPS,
            encoding="utf-8",
        )
    except OSError:
        return
    handler.setFormatter(formatter)
    logger.addHandler(handler)

"""Centralized logging setup for Xposer."""

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure the xposer root logger and return it.

    Safe to call repeatedly: handlers are attached exactly once.
    """
    logger = logging.getLogger("xposer")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the xposer namespace."""
    return logging.getLogger(f"xposer.{name}")

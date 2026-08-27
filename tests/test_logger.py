"""Tests for centralized logging setup."""

import logging

from utils.logger import get_logger, setup_logging


def test_setup_logging_is_idempotent():
    first = setup_logging()
    handler_count = len(first.handlers)
    second = setup_logging()
    assert first is second
    assert len(second.handlers) == handler_count


def test_get_logger_namespaced():
    logger = get_logger("test")
    assert logger.name == "xposer.test"
    assert logger.parent is logging.getLogger("xposer")

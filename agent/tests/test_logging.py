"""Tests for structured logging and secret redaction."""
import logging
from logging import LogRecord
from logging_utils import RedactingFormatter, setup_logging


def test_redacting_formatter():
    formatter = RedactingFormatter("[%(levelname)s] %(message)s")

    record1 = LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='Bearer my_secret_token_12345 and lease_token="12345678-1234-1234-1234-123456789abc"',
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record1)
    assert "my_secret_token_12345" not in formatted
    assert "[REDACTED]" in formatted
    assert "12345678-1234-1234-1234-123456789abc" not in formatted


def test_setup_logging():
    logger = setup_logging()
    assert logger.name == "telegramfonts.agent"
    assert len(logger.handlers) > 0

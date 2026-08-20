"""Structured and sanitized logging for A23 Agent."""
from __future__ import annotations

import logging
import re
import sys

# Sensitive pattern matching for redaction
REDACTION_PATTERNS = [
    re.compile(r"(Bearer\s+)[a-zA-Z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"([a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12})"),  # UUID/lease token
    re.compile(r"(\"?(?:token|secret|password|lease_token|auth)\"?\s*[:=]\s*\"?)[^\",\s}]+", re.IGNORECASE),
]


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        orig = super().format(record)
        redacted = orig
        for pattern in REDACTION_PATTERNS:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        return redacted


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("telegramfonts.agent")
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            RedactingFormatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
        )
        logger.addHandler(handler)

    return logger

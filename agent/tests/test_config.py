"""Tests for agent configuration."""
from pathlib import Path
import pytest
from pydantic import ValidationError

from config import Settings


def test_settings_validation(tmp_path: Path):
    settings = Settings(
        CF_ACCOUNT_ID="acc1",
        CF_QUEUE_ID="q1",
        CF_QUEUES_TOKEN="tok1",
        EDGE_BASE_URL="http://example.com/edge/",
        A23_NODE_SECRET="sec1",
        A23_WORKER_ID="worker!@#123",
        SCRATCH_DIR=tmp_path,
    )

    # Normalized base url
    assert settings.EDGE_BASE_URL == "http://example.com/edge"
    # Sanitized worker id
    assert settings.A23_WORKER_ID == "worker123"
    # Secrets protected
    assert settings.A23_NODE_SECRET.get_secret_value() == "sec1"
    assert settings.CF_QUEUES_TOKEN.get_secret_value() == "tok1"


def test_settings_missing_fields():
    with pytest.raises(ValidationError):
        Settings()

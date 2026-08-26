"""Tests for agent configuration and lease safety validation."""
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
        HEARTBEAT_INTERVAL_SECONDS=30,
        LEASE_DURATION_SECONDS=300,
    )

    # Normalized base url
    assert settings.EDGE_BASE_URL == "http://example.com/edge"
    # Sanitized worker id
    assert settings.A23_WORKER_ID == "worker123"
    # Secrets protected
    assert settings.A23_NODE_SECRET.get_secret_value() == "sec1"
    assert settings.CF_QUEUES_TOKEN.get_secret_value() == "tok1"


def test_settings_rejects_unsafe_and_near_margin_heartbeat_lease_relation(tmp_path: Path):
    # 1. Heartbeat interval >= lease duration
    with pytest.raises(ValidationError, match="Unsafe configuration"):
        Settings(
            CF_ACCOUNT_ID="acc1",
            CF_QUEUE_ID="q1",
            CF_QUEUES_TOKEN="tok1",
            EDGE_BASE_URL="http://example.com/edge",
            A23_NODE_SECRET="sec1",
            SCRATCH_DIR=tmp_path,
            HEARTBEAT_INTERVAL_SECONDS=300,
            LEASE_DURATION_SECONDS=300,
        )

    # 2. Near-margin config: heartbeat + 15s >= lease duration (BLOCK D)
    with pytest.raises(ValidationError, match="15s safety margin"):
        Settings(
            CF_ACCOUNT_ID="acc1",
            CF_QUEUE_ID="q1",
            CF_QUEUES_TOKEN="tok1",
            EDGE_BASE_URL="http://example.com/edge",
            A23_NODE_SECRET="sec1",
            SCRATCH_DIR=tmp_path,
            HEARTBEAT_INTERVAL_SECONDS=290,
            LEASE_DURATION_SECONDS=300,
        )


def test_settings_missing_fields(monkeypatch):
    monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CF_QUEUE_ID", raising=False)
    monkeypatch.delenv("CF_QUEUES_TOKEN", raising=False)
    monkeypatch.delenv("EDGE_BASE_URL", raising=False)
    monkeypatch.delenv("A23_NODE_SECRET", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_OPENROUTER_DEV_VARS_LOWERCASE_LOADS(tmp_path: Path, monkeypatch):
    """OPENROUTER_DEV_VARS_LOWERCASE_LOADS: non-versioned dev.vars-shaped lowercase
    openrouter_api_key loads safely into OPENROUTER_API_KEY (temporary fake file;
    the real dev.vars is never read and the value never leaks into repr/logs)."""
    fake_key = "sk-or-v1-fake-devvars-load-test-000000000000"
    (tmp_path / "dev.vars").write_text(f"openrouter_api_key = {fake_key}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    settings = Settings(
        CF_ACCOUNT_ID="acc1",
        CF_QUEUE_ID="q1",
        CF_QUEUES_TOKEN="tok1",
        EDGE_BASE_URL="http://example.com/edge",
        A23_NODE_SECRET="sec1",
        SCRATCH_DIR=tmp_path,
    )

    assert settings.OPENROUTER_API_KEY is not None
    assert settings.OPENROUTER_API_KEY.get_secret_value() == fake_key
    # VIETNAMESE_AI_ENABLED stays explicitly opt-in (default false).
    assert settings.VIETNAMESE_AI_ENABLED is False
    # Secret value never leaks into stringified settings.
    assert fake_key not in repr(settings)
    assert fake_key not in str(settings)

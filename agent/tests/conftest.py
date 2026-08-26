"""Pytest configuration and fixtures for Agent tests."""
import os
import sys
from pathlib import Path
import pytest

# Add agent/src and repo root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import Settings


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    # Hermetic: never read local .env / dev.vars / ~/.telefont.env runtime
    # secret files; every test value is supplied explicitly.
    return Settings(
        _env_file=None,
        CF_ACCOUNT_ID="test_account_123",
        CF_QUEUE_ID="test_queue_456",
        CF_QUEUES_TOKEN="test_cf_token_secret",
        EDGE_BASE_URL="http://localhost:8787",
        A23_NODE_SECRET="test_node_secret_abc",
        A23_WORKER_ID="test-worker-1",
        SCRATCH_DIR=tmp_path / "scratch",
        HEARTBEAT_INTERVAL_SECONDS=1,
    )

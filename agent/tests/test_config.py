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


def test_SETTINGS_DEFAULT_NO_DEV_VARS_READ(tmp_path: Path, monkeypatch):
    """SETTINGS_DEFAULT_NO_DEV_VARS_READ: direct Settings construction can never
    open a cwd/repo dev.vars sentinel. The dev.vars-shaped key is consumed
    only by the explicit runtime loader at the composition boundary."""
    sentinel = "sk-or-v1-devvars-sentinel-must-not-load"
    (tmp_path / "dev.vars").write_text(f"openrouter_api_key = {sentinel}\n", encoding="utf-8")
    (tmp_path / ".dev.vars").write_text(f"openrouter_api_key = {sentinel}\n", encoding="utf-8")
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

    # The sentinel never reaches Settings; VI stays explicitly opt-in.
    assert settings.OPENROUTER_API_KEY is None
    assert settings.VIETNAMESE_AI_ENABLED is False


def test_RUNTIME_KEY_ONLY_VI_PROVIDER(tmp_path: Path, test_settings: Settings):
    """RUNTIME_KEY_ONLY_VI_PROVIDER: explicit temporary dev.vars-shaped lowercase
    key-only file -> VI missing-coverage provider available through the fixed
    OpenRouter route (mocked transport). Real dev.vars reads=0, live requests=0."""
    import asyncio
    import json

    import httpx

    from composition import build_production_components, load_dev_vars_secret
    from compute.openrouter_client import MODEL_PRIMARY
    from tests.test_issue72_review_repros import _valid_candidate_payload

    fake_key = "sk-or-v1-fake-key-only-runnable-000000000000"
    dev_vars = tmp_path / "dev.vars"
    dev_vars.write_text(
        "# temporary fake secret file (test only)\n"
        f"openrouter_api_key = {fake_key}\n",
        encoding="utf-8",
    )

    # Lowercase key shape parses exactly; missing key/file fails closed.
    assert load_dev_vars_secret(dev_vars, "openrouter_api_key") == fake_key
    assert load_dev_vars_secret(tmp_path / "absent.dev.vars", "openrouter_api_key") == ""

    # Key-only shape: flag stays false, no env key; the explicit loader path
    # makes the fixed OpenRouter provider available for VI missing coverage.
    settings = test_settings.model_copy(update={"VIETNAMESE_AI_ENABLED": False})
    assert settings.OPENROUTER_API_KEY is None
    components = build_production_components(
        settings, tmp_path / "scratch", dev_vars_path=dev_vars
    )
    provider = components["vietnamese_ai_provider"]
    assert provider is not None
    assert provider.model_id == "openrouter"

    # Fixed route under a mocked transport (live requests=0): routine missing
    # case -> PRIMARY only.
    missing = [0x0110, 0x0111]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["model"])
        return httpx.Response(
            200, json={"choices": [{"message": {"content": _valid_candidate_payload(missing)}}]}
        )

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider._owns_client = False
    specs = asyncio.run(
        provider.generate_candidates(
            {
                "missing_codepoints": missing,
                "units_per_em": 1000,
                "source_hash": "s" * 64,
                "style_evidence": {"family_name": "Key Only Fam", "style_name": "Regular"},
            }
        )
    )
    assert len(specs) == 2
    assert calls == [MODEL_PRIMARY]

    # Without any explicit dev.vars path, no file is read and (flag false)
    # composition stays provider-less: missing key fails closed only when AI
    # is actually required.
    bare = build_production_components(settings, tmp_path / "scratch2")
    assert bare["vietnamese_ai_provider"] is None

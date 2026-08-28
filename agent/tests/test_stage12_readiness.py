"""Stage 12 tests for production readiness, composition preflight, deployment manifest,
authorization guard, and end-to-end multi-tier pipeline validation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

pytestmark = pytest.mark.integration

from config import Settings
from manifest import generate_deployment_manifest, verify_deployment_manifest
from readiness import ReadinessReport, run_a23_preflight
from runner import A23Runner, RunnerAction
from soak import run_a23_soak_harness


def test_A1_prod_composition_and_preflight():
    """A1: Consolidated preflight checks all stores, schemas, migrations, dependencies, and settings."""
    settings = Settings(
        CF_ACCOUNT_ID="test_acc",
        CF_QUEUE_ID="test_queue",
        CF_QUEUES_TOKEN="test_token",
        EDGE_BASE_URL="http://localhost:8787",
        A23_NODE_SECRET="test_secret",
        A23_WORKER_ID="worker-1",
        SCRATCH_DIR=Path(tempfile.gettempdir()) / "test_scratch_a1",
        FONT_ARCHIVE_ROOT=Path(tempfile.gettempdir()) / "test_archive_a1",
        ACQUISITION_ENABLED=True,
    )
    result = run_a23_preflight(settings)
    assert result.overall_status in ("PASS", "WARN")
    assert len(result.checks) >= 20
    check_names = {c.name for c in result.checks}
    assert "Dependency [fonttools]" in check_names
    assert "Dependency [freetype-py]" in check_names
    assert "Dependency [uharfbuzz]" in check_names
    assert "Dependency [sqlite3]" in check_names
    assert "ObservationStore Schema & Migrations" in check_names
    assert "CanonicalFontModelCache Schema" in check_names
    assert "AuthorizedBinaryCache Schema" in check_names
    assert "FinalFontArchive Schema" in check_names


def test_A8_deploy_manifest_and_tamper_detection(tmp_path: Path):
    """A8: Deployment manifest binds code, schema, config, dependency identities and detects tamper."""
    manifest = generate_deployment_manifest(repo_root=Path("."))
    assert manifest["main_commit_sha"]
    assert manifest["manifest_signature"]
    assert len(manifest["core_file_hashes"]) >= 40
    assert "source_collections" in manifest["database_schemas"]

    # Save to temp file and verify
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    valid, drift = verify_deployment_manifest(manifest_path, repo_root=Path("."))
    assert valid is True
    assert len(drift) == 0

    # Tamper test: modify a schema hash and verify it detects drift
    tampered_dict = dict(manifest)
    tampered_dict["database_schemas"] = dict(tampered_dict["database_schemas"])
    tampered_dict["database_schemas"]["source_collections"] = "0" * 64
    manifest_path.write_text(json.dumps(tampered_dict, indent=2), encoding="utf-8")
    tamper_valid, tamper_drift = verify_deployment_manifest(manifest_path, repo_root=Path("."))
    assert tamper_valid is False
    assert any("source_collections" in d.lower() for d in tamper_drift)


def test_A9_auth_guard_physical_proof(monkeypatch):
    """A9: Physical-proof script without exact authorization token fails closed with zero mutation."""
    monkeypatch.delenv("A23_PHYSICAL_PROOF_AUTH_TOKEN", raising=False)
    import importlib.util
    proof_path = Path("scripts/run_physical_a23_proof.py").resolve()
    spec = importlib.util.spec_from_file_location("run_physical_a23_proof", proof_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    verify_auth = mod.verify_physical_proof_authorization
    run_proof = mod.run_a23_full_style_proof

    # 1. verify_auth checks
    assert verify_auth(None) is False
    assert verify_auth("") is False
    assert verify_auth("INVALID_TOKEN_1234567890123456") is False
    assert verify_auth("ARCHITECT_EXECUTING_AUTHORIZED_short") is False
    assert verify_auth("ARCHITECT_EXECUTING_AUTHORIZED_0123456789abcdef") is True

    # 2. run_proof raises PermissionError without valid token
    with pytest.raises(PermissionError) as exc:
        run_proof(browser_version="b", config_hash="c", auth_token=None)
    assert "UNAUTHORIZED_PHYSICAL_PROOF_INVOCATION" in str(exc.value)

    with pytest.raises(PermissionError):
        run_proof(browser_version="b", config_hash="c", auth_token="INVALID")


@pytest.mark.asyncio
async def test_A7_soak_harness_small_run(tmp_path: Path):
    """A7: Soak harness runs deterministically with zero duplicates, zero partial publishes, and clean scratch."""
    res1 = await run_a23_soak_harness(num_jobs=10, seed=123, work_root=tmp_path / "soak1")
    assert res1.passed is True
    assert res1.total_jobs == 10
    assert res1.duplicate_completions == 0
    assert res1.partial_publishes == 0
    assert res1.orphan_scratch_dirs == 0

    # Rerun with identical seed produces identical trace hash
    res2 = await run_a23_soak_harness(num_jobs=10, seed=123, work_root=tmp_path / "soak2")
    assert res2.passed is True
    assert res2.soak_trace_hash == res1.soak_trace_hash


@pytest.mark.asyncio
async def test_A2_binary_e2e(tmp_path: Path):
    """A2: Binary-first path reaches verified TTF+OTF artifacts with zero reconstruction or AI calls."""
    res = await run_a23_soak_harness(num_jobs=5, seed=42, work_root=tmp_path / "a2")
    assert res.passed is True
    binary_traces = [t for t in res.job_traces if t.scenario == "BINARY_FIRST"]
    assert len(binary_traces) == 5
    for t in binary_traces:
        assert t.action == "acked"
        assert t.artifact_sha is not None
        assert t.zip_size > 0
        assert t.ai_calls == 0


@pytest.mark.asyncio
async def test_A6_all_or_nothing_e2e(tmp_path: Path):
    """A6: Failing jobs leave zero uploaded artifacts, zero DB completions, and zero orphan state."""
    res = await run_a23_soak_harness(num_jobs=100, seed=42, work_root=tmp_path / "a6")
    assert res.passed is True
    neg_traces = [t for t in res.job_traces if t.scenario == "ALL_OR_NOTHING_FAILURE"]
    assert len(neg_traces) == 5
    for t in neg_traces:
        assert t.artifact_key is None
        assert t.artifact_sha is None
    assert res.partial_publishes == 0
    assert res.orphan_scratch_dirs == 0

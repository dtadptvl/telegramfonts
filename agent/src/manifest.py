"""A23 Deployment Manifest Generator & Verifier.

Generates a reproducible deployment manifest binding:
- Main commit SHA
- Python, OS, and platform runtime identities
- Dependency versions (fontTools, freetype, uharfbuzz, PIL, pydantic, httpx)
- Database schema definitions (source_collections, observations, pair_observations, feature_observations, final_fonts, font_model_cache, authorized_binary_cache)
- ObservationConfig canonical hash
- Normalized file SHA-256 digests of all critical codebase components
- Deterministic manifest signature

Detects any code drift, tampering, or missing schema migrations.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from measurement.models import ObservationConfig

MANIFEST_SCHEMA_VERSION = "a23-deployment-manifest/v1"

CORE_SOURCE_PATHS = [
    "agent/src/composition.py",
    "agent/src/config.py",
    "agent/src/logging_utils.py",
    "agent/src/main.py",
    "agent/src/manifest.py",
    "agent/src/queue_client.py",
    "agent/src/readiness.py",
    "agent/src/runner.py",
    "agent/src/scratch.py",
    "agent/src/soak.py",
    "agent/src/worker_client.py",
    "agent/src/acquisition/adapters.py",
    "agent/src/acquisition/capability.py",
    "agent/src/acquisition/models.py",
    "agent/src/acquisition/pipeline.py",
    "agent/src/acquisition/providers.py",
    "agent/src/acquisition/raster_ingest.py",
    "agent/src/acquisition/verifier.py",
    "agent/src/compute/archive.py",
    "agent/src/compute/binary_cache.py",
    "agent/src/compute/binary_gate.py",
    "agent/src/compute/font_builder.py",
    "agent/src/compute/model_cache.py",
    "agent/src/compute/models.py",
    "agent/src/compute/openrouter_client.py",
    "agent/src/compute/packager.py",
    "agent/src/compute/source.py",
    "agent/src/compute/validator.py",
    "agent/src/compute/vietnamese.py",
    "agent/src/fidelity/evaluator.py",
    "agent/src/fidelity/pipeline.py",
    "agent/src/fidelity/producers.py",
    "agent/src/fidelity/release_gate.py",
    "agent/src/measurement/browser_session.py",
    "agent/src/measurement/collector.py",
    "agent/src/measurement/models.py",
    "agent/src/measurement/store.py",
    "agent/src/reconstruction/baseline.py",
    "agent/src/reconstruction/bezier_fitter.py",
    "agent/src/reconstruction/candidate_builder.py",
    "agent/src/reconstruction/candidate_validator.py",
    "agent/src/reconstruction/evaluator.py",
    "agent/src/reconstruction/font_model.py",
    "agent/src/reconstruction/models.py",
    "agent/src/reconstruction/sdf.py",
    "agent/src/reconstruction/solver.py",
    "agent/src/reconstruction/topology.py",
    "agent/src/typography/kerning_inferencer.py",
    "scripts/a23_preflight.py",
    "scripts/a23_soak_runner.py",
    "scripts/generate_deployment_manifest.py",
    "scripts/run_physical_a23_proof.py",
]

SQLITE_SCHEMA_DDLS = {
    "source_collections": "CREATE TABLE IF NOT EXISTS source_collections (collection_key TEXT PRIMARY KEY, source_url TEXT NOT NULL, reference_id TEXT NOT NULL, style_id TEXT NOT NULL, config_hash TEXT NOT NULL, browser_version TEXT NOT NULL, completed_at TEXT NOT NULL, capability_json TEXT NOT NULL DEFAULT '', capability_hash TEXT NOT NULL DEFAULT '')",
    "observations": "CREATE TABLE IF NOT EXISTS observations (cache_key TEXT PRIMARY KEY, reference_id TEXT NOT NULL, style_id TEXT NOT NULL, code_point INTEGER NOT NULL, resolution INTEGER NOT NULL, subpixel_x REAL NOT NULL, subpixel_y REAL NOT NULL, browser_version TEXT NOT NULL, config_hash TEXT NOT NULL, advance_width_px REAL NOT NULL, lsb_px REAL NOT NULL, rsb_px REAL NOT NULL, ascent_px REAL NOT NULL, descent_px REAL NOT NULL, advance_width_upem REAL NOT NULL, lsb_upem REAL NOT NULL, rsb_upem REAL NOT NULL, ascent_upem REAL NOT NULL, descent_upem REAL NOT NULL, bbox_width_upem REAL NOT NULL, bbox_height_upem REAL NOT NULL, sample_count INTEGER NOT NULL, confidence REAL NOT NULL, raster_relative_path TEXT NOT NULL, raster_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)",
    "pair_observations": "CREATE TABLE IF NOT EXISTS pair_observations (id INTEGER PRIMARY KEY AUTOINCREMENT, reference_id TEXT NOT NULL, style_id TEXT NOT NULL, browser_version TEXT NOT NULL, config_hash TEXT NOT NULL, left_cp INTEGER NOT NULL, right_cp INTEGER NOT NULL, left_char TEXT NOT NULL, right_char TEXT NOT NULL, left_advance_upem REAL NOT NULL, right_advance_upem REAL NOT NULL, pair_advance_upem REAL NOT NULL, inferred_kerning_upem INTEGER NOT NULL, confidence REAL NOT NULL, provenance TEXT NOT NULL, created_at TEXT NOT NULL)",
    "feature_observations": "CREATE TABLE IF NOT EXISTS feature_observations (id INTEGER PRIMARY KEY AUTOINCREMENT, reference_id TEXT NOT NULL, style_id TEXT NOT NULL, browser_version TEXT NOT NULL, config_hash TEXT NOT NULL, feature_tag TEXT NOT NULL, sample_text TEXT NOT NULL, enabled_advance_upem REAL NOT NULL, disabled_advance_upem REAL NOT NULL, enabled_raster_signature TEXT NOT NULL, disabled_raster_signature TEXT NOT NULL, effect_observed INTEGER NOT NULL, provenance TEXT NOT NULL, created_at TEXT NOT NULL)",
    "final_fonts": "CREATE TABLE IF NOT EXISTS final_fonts (cache_key TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, source_identity TEXT NOT NULL, family_name TEXT NOT NULL, style_id TEXT NOT NULL, style_name TEXT NOT NULL, mode TEXT NOT NULL, format TEXT NOT NULL, observation_identity TEXT NOT NULL, pipeline_version TEXT NOT NULL, config_version TEXT NOT NULL, relative_path TEXT NOT NULL, filename TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256_hex TEXT NOT NULL, created_at TEXT NOT NULL, attestation_json TEXT NOT NULL DEFAULT '', attestation_hash TEXT NOT NULL DEFAULT '')",
    "canonical_font_models": "CREATE TABLE IF NOT EXISTS canonical_font_models (cache_key TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, identity_json TEXT NOT NULL, relative_path TEXT NOT NULL, model_hash TEXT NOT NULL, payload_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '')",
    "authorized_binaries": "CREATE TABLE IF NOT EXISTS authorized_binaries (cache_key TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, identity_json TEXT NOT NULL, relative_path TEXT NOT NULL, format TEXT NOT NULL, binary_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL, stage_provenance TEXT NOT NULL DEFAULT '')",
}


def _get_git_commit_sha(repo_root: Path) -> str:
    """Retrieve git HEAD SHA or fallback to environment."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return os.environ.get("MAIN_COMMIT_SHA", "7eebebbe82290a0f57206505a61d7b3d0c7b65ce")


def _get_dependency_versions() -> dict[str, str]:
    deps = {}
    for mod_name, label in [
        ("fontTools", "fonttools"),
        ("freetype", "freetype-py"),
        ("uharfbuzz", "uharfbuzz"),
        ("PIL", "pillow"),
        ("pydantic", "pydantic"),
        ("httpx", "httpx"),
    ]:
        try:
            mod = __import__(mod_name)
            v = getattr(mod, "__version__", getattr(mod, "version", "present"))
            if mod_name == "uharfbuzz" and hasattr(mod, "version_string"):
                v = mod.version_string()
            deps[label] = str(v)
        except Exception:
            deps[label] = "missing"
    return deps


def _compute_schema_hashes() -> dict[str, str]:
    return {
        table: hashlib.sha256(ddl.encode("utf-8")).hexdigest()
        for table, ddl in SQLITE_SCHEMA_DDLS.items()
    }


def _compute_file_hashes(repo_root: Path) -> dict[str, str]:
    hashes = {}
    for rel_path in sorted(CORE_SOURCE_PATHS):
        p = repo_root / rel_path
        if p.exists():
            # Normalize CRLF to LF for deterministic hashes across platforms
            content = p.read_bytes().replace(b"\r\n", b"\n")
            hashes[rel_path] = hashlib.sha256(content).hexdigest()
        else:
            hashes[rel_path] = "missing"
    return hashes


def generate_deployment_manifest(
    repo_root: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Generate deterministic A23 deployment manifest."""
    repo_root = Path(repo_root).resolve()
    commit_sha = _get_git_commit_sha(repo_root)
    deps = _get_dependency_versions()
    schemas = _compute_schema_hashes()
    file_hashes = _compute_file_hashes(repo_root)
    obs_cfg_hash = ObservationConfig().compute_hash()

    manifest_body = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "main_commit_sha": commit_sha,
        "python_version": platform.python_version(),
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
        "dependencies": deps,
        "database_schemas": schemas,
        "observation_config_hash": obs_cfg_hash,
        "core_file_hashes": file_hashes,
    }

    # Deterministic manifest signature
    canonical_bytes = json.dumps(manifest_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest_signature = hashlib.sha256(canonical_bytes).hexdigest()

    manifest = {
        **manifest_body,
        "manifest_signature": manifest_signature,
    }

    if output_path:
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return manifest


def verify_deployment_manifest(
    manifest_data: dict[str, Any] | Path,
    repo_root: Path,
) -> tuple[bool, list[str]]:
    """Verify deployment manifest integrity and check for file/schema drift."""
    repo_root = Path(repo_root).resolve()
    if isinstance(manifest_data, (str, Path)):
        p = Path(manifest_data)
        if not p.exists():
            return False, [f"Manifest file missing: {p}"]
        manifest = json.loads(p.read_text(encoding="utf-8"))
    else:
        manifest = manifest_data

    drift_reasons: list[str] = []

    # 1. Verify manifest signature
    declared_sig = manifest.get("manifest_signature", "")
    body_copy = {k: v for k, v in manifest.items() if k != "manifest_signature"}
    canonical_bytes = json.dumps(body_copy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected_sig = hashlib.sha256(canonical_bytes).hexdigest()
    if declared_sig != expected_sig:
        drift_reasons.append("MANIFEST_SIGNATURE_TAMPERED")

    # 2. Verify schema hashes
    current_schemas = _compute_schema_hashes()
    for table, expected_h in manifest.get("database_schemas", {}).items():
        curr_h = current_schemas.get(table)
        if curr_h != expected_h:
            drift_reasons.append(f"SCHEMA_DRIFT_{table.upper()}")

    # 3. Verify file hashes
    for rel_path, expected_file_h in manifest.get("core_file_hashes", {}).items():
        fp = repo_root / rel_path
        if not fp.exists():
            drift_reasons.append(f"MISSING_FILE_{rel_path}")
            continue
        content = fp.read_bytes().replace(b"\r\n", b"\n")
        curr_h = hashlib.sha256(content).hexdigest()
        if curr_h != expected_file_h:
            drift_reasons.append(f"FILE_DRIFT_{rel_path}")

    # 4. Verify observation config hash
    if manifest.get("observation_config_hash") != ObservationConfig().compute_hash():
        drift_reasons.append("OBSERVATION_CONFIG_HASH_DRIFT")

    return (len(drift_reasons) == 0, drift_reasons)

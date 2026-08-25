"""L2 reuse tier: durable canonical FontModel cache with exact identity binding.

An entry is keyed over the full reuse identity: source/reference fingerprint,
family/style, mode, coverage fingerprint, pipeline version, provenance, and
(for AI-assisted Vietnamese extension) the AI model/version/prompt binding.
Corruption, stale versions, or ambiguous identity are fail-closed misses.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("telegramfonts.agent.model_cache")

MODEL_CACHE_SCHEMA_VERSION = 1
MODEL_CACHE_PIPELINE_VERSION = "stage9d-fontmodel-v1"


@dataclass(frozen=True)
class FontModelCacheIdentity:
    """Exact reuse identity; any dimension drift is a different entry."""

    reference_fingerprint: str
    family_name: str
    style_id: str
    mode: str  # ORIGINAL | VIETNAMESE
    coverage_fingerprint: str
    provenance: str
    pipeline_version: str = MODEL_CACHE_PIPELINE_VERSION
    ai_model_id: str = ""
    ai_model_version: str = ""
    ai_prompt_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("reference_fingerprint", "family_name", "style_id", "mode", "coverage_fingerprint", "provenance"):
            if not getattr(self, name).strip():
                raise ValueError(f"MODEL_CACHE_IDENTITY_EMPTY_{name.upper()}")
        normalized_mode = self.mode.strip().upper()
        if normalized_mode not in {"ORIGINAL", "VIETNAMESE"}:
            raise ValueError("MODEL_CACHE_IDENTITY_MODE_UNSUPPORTED")
        object.__setattr__(self, "mode", normalized_mode)

    def to_dict(self) -> dict:
        return {
            "schema_version": MODEL_CACHE_SCHEMA_VERSION,
            "reference_fingerprint": self.reference_fingerprint,
            "family_name": self.family_name,
            "style_id": self.style_id,
            "mode": self.mode,
            "coverage_fingerprint": self.coverage_fingerprint,
            "provenance": self.provenance,
            "pipeline_version": self.pipeline_version,
            "ai_model_id": self.ai_model_id,
            "ai_model_version": self.ai_model_version,
            "ai_prompt_hash": self.ai_prompt_hash,
        }

    @property
    def cache_key(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CanonicalFontModelCache:
    """SQLite-indexed immutable FontModel store with hash-verified reads."""

    def __init__(self, root: Path | str, index_path: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.index_path = Path(index_path).expanduser().resolve()
        try:
            self.index_path.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise ValueError("MODEL_CACHE_INDEX_MUST_BE_OFF_CACHE_ROOT")
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.index_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS canonical_font_models (
                    cache_key TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    identity_json TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    model_hash TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT ''
                );
                """
            )
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(canonical_font_models)")}
            if "metadata_json" not in existing_cols:
                conn.execute(
                    "ALTER TABLE canonical_font_models ADD COLUMN metadata_json TEXT NOT NULL DEFAULT ''"
                )
            conn.commit()

    def put(self, identity: FontModelCacheIdentity, model, metadata: dict | None = None) -> str:
        """Persist one canonical FontModel; returns its model hash."""
        model_hash = model.compute_canonical_hash()
        payload = pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)
        payload_sha = hashlib.sha256(payload).hexdigest()

        target_dir = self.root / identity.cache_key[:2]
        target_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{identity.cache_key[:16]}-", suffix=".tmp", dir=str(target_dir))
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fd = -1
                fh.write(payload)
            target_path = target_dir / f"{identity.cache_key}.{payload_sha[:32]}.model.pkl"
            if not target_path.exists():
                os.replace(temp_path, target_path)
            relative_path = target_path.relative_to(self.root).as_posix()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO canonical_font_models (
                        cache_key, schema_version, identity_json, relative_path,
                        model_hash, payload_sha256, size_bytes, created_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identity.cache_key,
                        MODEL_CACHE_SCHEMA_VERSION,
                        json.dumps(identity.to_dict(), sort_keys=True, separators=(",", ":")),
                        relative_path,
                        model_hash,
                        payload_sha,
                        len(payload),
                        datetime.now(timezone.utc).isoformat(),
                        json.dumps(metadata or {}, sort_keys=True, separators=(",", ":")),
                    ),
                )
                conn.commit()
            return model_hash
        finally:
            if fd != -1:
                os.close(fd)
            temp_path.unlink(missing_ok=True)

    def get(self, identity: FontModelCacheIdentity):
        """Return the verified FontModel or None (fail-closed miss)."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM canonical_font_models WHERE cache_key = ?",
                    (identity.cache_key,),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("FontModel cache index read failed: %s", exc)
            return None
        if row is None:
            return None
        if row["schema_version"] != MODEL_CACHE_SCHEMA_VERSION:
            return None
        try:
            stored_identity = json.loads(row["identity_json"])
        except (ValueError, TypeError):
            return None
        if stored_identity != identity.to_dict():
            return None

        file_path = self.root / row["relative_path"]
        try:
            file_path = file_path.resolve()
            file_path.relative_to(self.root)
        except (ValueError, OSError):
            return None
        if not file_path.is_file():
            return None
        try:
            payload = file_path.read_bytes()
        except OSError:
            return None
        if len(payload) != row["size_bytes"]:
            return None
        if hashlib.sha256(payload).hexdigest() != row["payload_sha256"]:
            return None
        try:
            model = pickle.loads(payload)
        except Exception:
            return None
        try:
            if model.compute_canonical_hash() != row["model_hash"]:
                return None
        except Exception:
            return None
        return model

    def get_metadata(self, identity: FontModelCacheIdentity) -> dict | None:
        """Return verified entry metadata or None (fail-closed miss)."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT metadata_json, schema_version FROM canonical_font_models WHERE cache_key = ?",
                    (identity.cache_key,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None or row["schema_version"] != MODEL_CACHE_SCHEMA_VERSION:
            return None
        try:
            payload = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

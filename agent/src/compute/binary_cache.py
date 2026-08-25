"""L3 reuse tier: durable, hash-verified authorized-binary cache.

Exact identity binds source/reference fingerprint, family/style, provenance,
and pipeline version. Atomic writes; reads are hash-verified. Corruption or
drift is a fail-closed miss; integrity ambiguity is terminal.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("telegramfonts.agent.binary_cache")

BINARY_CACHE_SCHEMA_VERSION = 1
BINARY_CACHE_PIPELINE_VERSION = "stage9d-authorized-binary-v1"


@dataclass(frozen=True)
class BinaryCacheIdentity:
    """Exact L3 reuse identity; any dimension drift is a different entry."""

    reference_fingerprint: str
    family_name: str
    style_id: str
    provenance: str
    pipeline_version: str = BINARY_CACHE_PIPELINE_VERSION

    def __post_init__(self) -> None:
        for name in ("reference_fingerprint", "family_name", "style_id", "provenance", "pipeline_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"BINARY_CACHE_IDENTITY_EMPTY_{name.upper()}")

    def to_dict(self) -> dict:
        return {
            "schema_version": BINARY_CACHE_SCHEMA_VERSION,
            "reference_fingerprint": self.reference_fingerprint,
            "family_name": self.family_name,
            "style_id": self.style_id,
            "provenance": self.provenance,
            "pipeline_version": self.pipeline_version,
        }

    @property
    def cache_key(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuthorizedBinaryCache:
    """SQLite-indexed immutable authorized-binary store with hash-verified reads."""

    def __init__(self, root: Path | str, index_path: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.index_path = Path(index_path).expanduser().resolve()
        try:
            self.index_path.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise ValueError("BINARY_CACHE_INDEX_MUST_BE_OFF_CACHE_ROOT")
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
                CREATE TABLE IF NOT EXISTS authorized_binaries (
                    cache_key TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    identity_json TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    format TEXT NOT NULL,
                    binary_sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    stage_provenance TEXT NOT NULL DEFAULT ''
                );
                """
            )
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(authorized_binaries)")}
            if "stage_provenance" not in existing_cols:
                conn.execute(
                    "ALTER TABLE authorized_binaries ADD COLUMN stage_provenance TEXT NOT NULL DEFAULT ''"
                )
            conn.commit()

    def put(self, identity: BinaryCacheIdentity, raw_bytes: bytes, fmt: str, stage_provenance: str = "") -> str:
        """Atomically persist one authorized binary; returns its SHA-256."""
        if not raw_bytes:
            raise ValueError("BINARY_CACHE_EMPTY_PAYLOAD")
        fmt_norm = fmt.strip().upper()
        if fmt_norm not in ("TTF", "OTF"):
            raise ValueError("BINARY_CACHE_UNSUPPORTED_FORMAT")
        binary_sha = hashlib.sha256(raw_bytes).hexdigest()

        target_dir = self.root / identity.cache_key[:2]
        target_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{identity.cache_key[:16]}-", suffix=".tmp", dir=str(target_dir))
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fd = -1
                fh.write(raw_bytes)
            target_path = target_dir / f"{identity.cache_key}.{binary_sha[:32]}.{fmt_norm.lower()}"
            if not target_path.exists():
                os.replace(temp_path, target_path)
            relative_path = target_path.relative_to(self.root).as_posix()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO authorized_binaries (
                        cache_key, schema_version, identity_json, relative_path,
                        format, binary_sha256, size_bytes, created_at, stage_provenance
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identity.cache_key,
                        BINARY_CACHE_SCHEMA_VERSION,
                        json.dumps(identity.to_dict(), sort_keys=True, separators=(",", ":")),
                        relative_path,
                        fmt_norm,
                        binary_sha,
                        len(raw_bytes),
                        datetime.now(timezone.utc).isoformat(),
                        stage_provenance,
                    ),
                )
                conn.commit()
            return binary_sha
        finally:
            if fd != -1:
                os.close(fd)
            temp_path.unlink(missing_ok=True)

    def get(self, identity: BinaryCacheIdentity) -> tuple[bytes | None, str, str, str]:
        """Return (raw_bytes, format, stage_provenance, status).

        status in HIT | MISS | CORRUPT. CORRUPT means an entry exists but
        failed identity/hash verification; callers must treat it as terminal
        integrity ambiguity.
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM authorized_binaries WHERE cache_key = ?",
                    (identity.cache_key,),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("Binary cache index read failed: %s", exc)
            return None, "", "", "MISS"
        if row is None:
            return None, "", "", "MISS"
        if row["schema_version"] != BINARY_CACHE_SCHEMA_VERSION:
            return None, "", "", "CORRUPT"
        try:
            stored_identity = json.loads(row["identity_json"])
        except (ValueError, TypeError):
            return None, "", "", "CORRUPT"
        if stored_identity != identity.to_dict():
            return None, "", "", "CORRUPT"

        file_path = (self.root / row["relative_path"]).resolve()
        try:
            file_path.relative_to(self.root)
        except ValueError:
            return None, "", "", "CORRUPT"
        if not file_path.is_file():
            return None, "", "", "CORRUPT"
        try:
            raw = file_path.read_bytes()
        except OSError:
            return None, "", "", "CORRUPT"
        if len(raw) != row["size_bytes"]:
            return None, "", "", "CORRUPT"
        if hashlib.sha256(raw).hexdigest() != row["binary_sha256"]:
            return None, "", "", "CORRUPT"
        return raw, str(row["format"]), str(row["stage_provenance"]), "HIT"

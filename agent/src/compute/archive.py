"""Durable immutable archive for validated final TTF/OTF font binaries."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from compute.models import GeneratedFontFile

logger = logging.getLogger("telegramfonts.agent.archive")

ARCHIVE_SCHEMA_VERSION = 2
FINAL_FONT_PIPELINE_VERSION = "max-final-font-v1"
ARCHIVEABLE_FORMATS = frozenset({"TTF", "OTF"})
ARCHIVE_COPY_CHUNK_BYTES = 1024 * 1024


def canonical_source_identity(source_url: str) -> str:
    """Return a stable, non-secret identity for a source URL."""
    clean = source_url.strip()
    parsed = urlparse(clean)
    if not parsed.scheme or not parsed.hostname or not parsed.path:
        raise ValueError("INVALID_ARCHIVE_SOURCE_IDENTITY")

    path = "/" + "/".join(part for part in parsed.path.split("/") if part)
    if path != "/":
        path = path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    host = parsed.hostname.lower()
    if host == "www.myfonts.com":
        host = "myfonts.com"
    return f"{parsed.scheme.lower()}://{host}{path}{query}"


@dataclass(frozen=True)
class ArchiveIdentity:
    """All dimensions that can change the bytes of a final font artifact."""

    source_identity: str
    family_name: str
    style_id: str
    style_name: str
    mode: str
    format: str
    observation_identity: str
    pipeline_version: str = FINAL_FONT_PIPELINE_VERSION
    config_version: str = ""

    def __post_init__(self) -> None:
        for name in (
            "source_identity",
            "family_name",
            "style_id",
            "style_name",
            "observation_identity",
            "pipeline_version",
            "config_version",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"EMPTY_ARCHIVE_IDENTITY_{name.upper()}")

        normalized_mode = self.mode.strip().upper()
        if normalized_mode not in {"ORIGINAL", "VIETNAMESE"}:
            raise ValueError(f"UNSUPPORTED_ARCHIVE_MODE: {normalized_mode}")
        object.__setattr__(self, "mode", normalized_mode)

        normalized_format = self.format.strip().upper()
        if normalized_format not in ARCHIVEABLE_FORMATS:
            raise ValueError(f"UNSUPPORTED_ARCHIVE_FORMAT: {normalized_format}")
        object.__setattr__(self, "format", normalized_format)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "source_identity": self.source_identity,
            "family_name": self.family_name,
            "style_id": self.style_id,
            "style_name": self.style_name,
            "mode": self.mode,
            "format": self.format,
            "observation_identity": self.observation_identity,
            "pipeline_version": self.pipeline_version,
            "config_version": self.config_version,
        }

    @property
    def cache_key(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ArchiveEntry:
    identity: ArchiveIdentity
    relative_path: str
    filename: str
    size_bytes: int
    sha256_hex: str
    created_at: str
    file_path: Path

    def to_generated_font_file(self) -> GeneratedFontFile:
        return GeneratedFontFile(
            style_id=self.identity.style_id,
            style_name=self.identity.style_name,
            format=self.identity.format,
            filename=self.filename,
            file_path=self.file_path,
            size_bytes=self.size_bytes,
            sha256_hex=self.sha256_hex,
        )


class FinalFontArchive:
    """SQLite-indexed immutable artifact store with the index kept off the archive disk."""

    def __init__(self, root: Path | str, index_path: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.index_path = Path(index_path).expanduser().resolve()

        try:
            self.index_path.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise ValueError("ARCHIVE_INDEX_MUST_BE_ON_INTERNAL_STORAGE")

        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @classmethod
    def from_settings(cls, settings: Any) -> FinalFontArchive | None:
        root = getattr(settings, "FONT_ARCHIVE_ROOT", None)
        if root is None:
            return None
        scratch_dir = Path(settings.SCRATCH_DIR)
        return cls(root, scratch_dir / "font_archive_index.sqlite3")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.index_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS final_fonts (
                    cache_key TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    source_identity TEXT NOT NULL,
                    family_name TEXT NOT NULL,
                    style_id TEXT NOT NULL,
                    style_name TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    format TEXT NOT NULL,
                    observation_identity TEXT NOT NULL,
                    pipeline_version TEXT NOT NULL,
                    config_version TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256_hex TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_final_fonts_lookup
                    ON final_fonts (source_identity, style_id, mode, format);
                """
            )
            # Stage 9D attestation migration: legacy rows keep empty attestation
            # and are therefore cache misses for attested lookups.
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(final_fonts)")}
            if "attestation_json" not in existing_cols:
                conn.execute(
                    "ALTER TABLE final_fonts ADD COLUMN attestation_json TEXT NOT NULL DEFAULT ''"
                )
            if "attestation_hash" not in existing_cols:
                conn.execute(
                    "ALTER TABLE final_fonts ADD COLUMN attestation_hash TEXT NOT NULL DEFAULT ''"
                )
            conn.commit()

    def _safe_archive_path(self, relative_path: str) -> Path | None:
        candidate_relative = Path(relative_path)
        if candidate_relative.is_absolute() or ".." in candidate_relative.parts:
            return None
        candidate = (self.root / candidate_relative).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _hash_file(file_path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with file_path.open("rb") as source:
            while True:
                chunk = source.read(ARCHIVE_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return size, digest.hexdigest()

    def _entry_from_row(self, row: sqlite3.Row, file_path: Path) -> ArchiveEntry:
        identity = ArchiveIdentity(
            source_identity=row["source_identity"],
            family_name=row["family_name"],
            style_id=row["style_id"],
            style_name=row["style_name"],
            mode=row["mode"],
            format=row["format"],
            observation_identity=row["observation_identity"],
            pipeline_version=row["pipeline_version"],
            config_version=row["config_version"],
        )
        return ArchiveEntry(
            identity=identity,
            relative_path=row["relative_path"],
            filename=row["filename"],
            size_bytes=row["size_bytes"],
            sha256_hex=row["sha256_hex"],
            created_at=row["created_at"],
            file_path=file_path,
        )

    def get(self, identity: ArchiveIdentity) -> ArchiveEntry | None:
        """Return a verified entry, or None for a missing/incompatible/corrupt entry."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM final_fonts WHERE cache_key = ?",
                    (identity.cache_key,),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("Final-font archive index read failed: %s", exc)
            return None

        if row is None:
            return None

        expected = identity.to_dict()
        if any(row[name] != value for name, value in expected.items() if name != "schema_version"):
            return None
        if row["schema_version"] != ARCHIVE_SCHEMA_VERSION:
            return None

        file_path = self._safe_archive_path(row["relative_path"])
        if file_path is None or not file_path.is_file():
            return None
        try:
            size, digest = self._hash_file(file_path)
        except OSError as exc:
            logger.warning("Final-font archive artifact read failed: %s", exc)
            return None
        if size != row["size_bytes"] or digest != row["sha256_hex"]:
            return None
        return self._entry_from_row(row, file_path)

    def put(self, identity: ArchiveIdentity, font_file: GeneratedFontFile) -> ArchiveEntry:
        """Atomically persist one validated final font and then commit its index metadata."""
        if font_file.format.strip().upper() != identity.format:
            raise ValueError("ARCHIVE_FONT_FORMAT_MISMATCH")
        if identity.format not in ARCHIVEABLE_FORMATS:
            raise ValueError(f"UNSUPPORTED_ARCHIVE_FORMAT: {identity.format}")

        source_path = Path(font_file.file_path).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"ARCHIVE_SOURCE_MISSING: {source_path}")
        filename = Path(font_file.filename).name
        if not filename:
            raise ValueError("ARCHIVE_FILENAME_EMPTY")

        existing = self.get(identity)
        if existing is not None:
            return existing

        temp_dir = self.root / ".tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        target_dir = self.root / identity.cache_key[:2]
        target_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{identity.cache_key[:16]}-",
            suffix=".tmp",
            dir=str(temp_dir),
        )
        temp_path = Path(temp_name)
        try:
            digest = hashlib.sha256()
            size = 0
            target = os.fdopen(fd, "wb")
            fd = -1
            with source_path.open("rb") as source, target:
                while True:
                    chunk = source.read(ARCHIVE_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            sha256_hex = digest.hexdigest()
            if size == 0:
                raise ValueError("ARCHIVE_SOURCE_EMPTY")
            extension = identity.format.lower()
            target_path = target_dir / f"{identity.cache_key}.{sha256_hex}.{extension}"
            if target_path.exists():
                try:
                    existing_size, existing_sha256 = self._hash_file(target_path)
                except OSError:
                    existing_size, existing_sha256 = -1, ""
                if existing_size != size or existing_sha256 != sha256_hex:
                    target_path = target_dir / (
                        f"{identity.cache_key}.{sha256_hex}.repair-{uuid.uuid4().hex}.{extension}"
                    )

            if not target_path.exists():
                os.replace(temp_path, target_path)
                self._fsync_directory(target_path.parent)
            else:
                temp_path.unlink(missing_ok=True)

            relative_path = target_path.relative_to(self.root).as_posix()
            created_at = datetime.now(timezone.utc).isoformat()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO final_fonts (
                        cache_key, schema_version, source_identity, family_name,
                        style_id, style_name, mode, format, observation_identity,
                        pipeline_version, config_version, relative_path, filename,
                        size_bytes, sha256_hex, created_at,
                        attestation_json, attestation_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identity.cache_key,
                        ARCHIVE_SCHEMA_VERSION,
                        identity.source_identity,
                        identity.family_name,
                        identity.style_id,
                        identity.style_name,
                        identity.mode,
                        identity.format,
                        identity.observation_identity,
                        identity.pipeline_version,
                        identity.config_version,
                        relative_path,
                        filename,
                        size,
                        sha256_hex,
                        created_at,
                        "",
                        "",
                    ),
                )
                conn.commit()

            return ArchiveEntry(
                identity=identity,
                relative_path=relative_path,
                filename=filename,
                size_bytes=size,
                sha256_hex=sha256_hex,
                created_at=created_at,
                file_path=target_path,
            )
        finally:
            if fd != -1:
                os.close(fd)
            temp_path.unlink(missing_ok=True)

    def put_attested(
        self,
        identity: ArchiveIdentity,
        font_file: GeneratedFontFile,
        attestation_json: str,
        attestation_hash: str,
    ) -> ArchiveEntry:
        """Persist one Stage 9 PASS-gated artifact bound to its immutable attestation."""
        if not attestation_json.strip() or not attestation_hash.strip():
            raise ValueError("ARCHIVE_ATTESTATION_REQUIRED")
        entry = self.put(identity, font_file)
        with self._connect() as conn:
            conn.execute(
                "UPDATE final_fonts SET attestation_json = ?, attestation_hash = ? WHERE cache_key = ?",
                (attestation_json, attestation_hash, identity.cache_key),
            )
            conn.commit()
        return entry

    def get_attested(self, identity: ArchiveIdentity) -> ArchiveEntry | None:
        """Return a verified entry only when a valid Stage 9 attestation is bound.

        Legacy/unattested rows, tampered attestation payloads, and attestation
        hashes that fail recomputation are cache misses (fail-closed).
        """
        entry = self.get(identity)
        if entry is None:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT attestation_json, attestation_hash FROM final_fonts WHERE cache_key = ?",
                    (identity.cache_key,),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("Final-font archive attestation read failed: %s", exc)
            return None
        if row is None:
            return None
        attestation_json = row["attestation_json"]
        attestation_hash = row["attestation_hash"]
        if not attestation_json or not attestation_hash:
            return None
        try:
            payload = json.loads(attestation_json)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        recomputed = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if recomputed != attestation_hash:
            return None
        if payload.get("artifact_sha256") != entry.sha256_hex:
            return None
        if int(payload.get("artifact_size_bytes", -1)) != entry.size_bytes:
            return None
        if str(payload.get("format", "")).strip().upper() != identity.format:
            return None
        return entry

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Persist a rename where the platform exposes directory fsync."""
        try:
            directory_fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)

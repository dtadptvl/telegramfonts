"""Immutable observation persistence, lossless raster storage, and SQLite-backed minimal indexing."""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import Any

from measurement.manifest import ReproducibilityManifest
from measurement.models import (
    DirectMetrics,
    MetricObservation,
    ObservationRecord,
    OpenTypeFeatureObservation,
)

logger = logging.getLogger("telegramfonts.agent.measurement.store")


class ObservationStore:
    """Persistent storage and minimal index for immutable multi-resolution observations and metrics."""

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.base_dir / "index.sqlite3"
        self._init_db()

    @contextlib.contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create required tables with indexes for fast resume checks."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    cache_key TEXT PRIMARY KEY,
                    reference_id TEXT NOT NULL,
                    style_id TEXT NOT NULL,
                    code_point INTEGER NOT NULL,
                    resolution INTEGER NOT NULL,
                    subpixel_x REAL NOT NULL,
                    subpixel_y REAL NOT NULL,
                    raster_relative_path TEXT NOT NULL,
                    raster_sha256 TEXT NOT NULL,
                    raster_size_bytes INTEGER NOT NULL,
                    advance_width_px REAL NOT NULL,
                    advance_width_upem REAL NOT NULL,
                    lsb_px REAL NOT NULL,
                    lsb_upem REAL NOT NULL,
                    rsb_px REAL NOT NULL,
                    rsb_upem REAL NOT NULL,
                    ascent_px REAL NOT NULL,
                    ascent_upem REAL NOT NULL,
                    descent_px REAL NOT NULL,
                    descent_upem REAL NOT NULL,
                    bbox_width_upem REAL NOT NULL,
                    bbox_height_upem REAL NOT NULL,
                    sample_count INTEGER NOT NULL,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    browser_version TEXT NOT NULL,
                    config_hash TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_obs_ref_style ON observations (reference_id, style_id, code_point)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS manifests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    git_commit TEXT NOT NULL,
                    git_is_dirty INTEGER NOT NULL,
                    os_name TEXT NOT NULL,
                    os_release TEXT NOT NULL,
                    architecture TEXT NOT NULL,
                    python_version TEXT NOT NULL,
                    chromium_version TEXT NOT NULL,
                    playwright_version TEXT,
                    freetype_version TEXT,
                    harfbuzz_version TEXT,
                    fonttools_version TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS unicode_coverage (
                    reference_id TEXT NOT NULL,
                    style_id TEXT NOT NULL,
                    code_point INTEGER NOT NULL,
                    PRIMARY KEY (reference_id, style_id, code_point)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pair_observations (
                    reference_id TEXT NOT NULL,
                    style_id TEXT NOT NULL,
                    left_cp INTEGER NOT NULL,
                    right_cp INTEGER NOT NULL,
                    left_char TEXT NOT NULL,
                    right_char TEXT NOT NULL,
                    left_advance_upem REAL NOT NULL,
                    right_advance_upem REAL NOT NULL,
                    pair_advance_upem REAL NOT NULL,
                    inferred_kerning_upem INTEGER NOT NULL,
                    confidence REAL NOT NULL,
                    provenance TEXT NOT NULL DEFAULT 'untrusted',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (reference_id, style_id, left_cp, right_cp)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metric_observations (
                    reference_id TEXT NOT NULL,
                    style_id TEXT NOT NULL,
                    code_point INTEGER NOT NULL,
                    font_size_px REAL NOT NULL,
                    browser_version TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (
                        reference_id, style_id, code_point, font_size_px,
                        browser_version, config_hash
                    )
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feature_observations (
                    reference_id TEXT NOT NULL,
                    style_id TEXT NOT NULL,
                    feature_tag TEXT NOT NULL,
                    sample_text TEXT NOT NULL,
                    enabled_advance_upem REAL NOT NULL,
                    disabled_advance_upem REAL NOT NULL,
                    enabled_raster_signature TEXT NOT NULL,
                    disabled_raster_signature TEXT NOT NULL,
                    effect_observed INTEGER NOT NULL,
                    provenance TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (reference_id, style_id, feature_tag, sample_text)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_collections (
                    collection_key TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL,
                    reference_id TEXT NOT NULL,
                    style_id TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    browser_version TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_collection_attempts (
                    collection_key TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL
                )
                """
            )
            # Automatic schema migration for existing databases
            try:
                conn.execute(
                    "ALTER TABLE pair_observations ADD COLUMN provenance TEXT NOT NULL DEFAULT 'untrusted'"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "ALTER TABLE observations ADD COLUMN browser_version TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "ALTER TABLE observations ADD COLUMN config_hash TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass

            conn.commit()

    def save_metric_observation(self, observation: MetricObservation) -> None:
        """Persist an immutable direct metric sample for one size."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO metric_observations (
                    reference_id, style_id, code_point, font_size_px,
                    browser_version, config_hash, metrics_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.reference_id,
                    observation.style_id,
                    observation.metrics.code_point,
                    observation.metrics.font_size_px,
                    observation.browser_version,
                    observation.config_hash,
                    json.dumps(observation.metrics.__dict__, sort_keys=True),
                    observation.created_at,
                ),
            )
            conn.commit()

    def get_metric_observations(self, reference_id: str, style_id: str) -> list[dict[str, Any]]:
        """Return persisted multi-size metric samples."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM metric_observations
                WHERE reference_id = ? AND style_id = ?
                ORDER BY code_point, font_size_px
                """,
                (reference_id, style_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def save_feature_observation(self, observation: OpenTypeFeatureObservation) -> None:
        """Persist an immutable browser OpenType feature probe."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO feature_observations (
                    reference_id, style_id, feature_tag, sample_text,
                    enabled_advance_upem, disabled_advance_upem,
                    enabled_raster_signature, disabled_raster_signature,
                    effect_observed, provenance, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.reference_id,
                    observation.style_id,
                    observation.feature_tag,
                    observation.sample_text,
                    observation.enabled_advance_upem,
                    observation.disabled_advance_upem,
                    observation.enabled_raster_signature,
                    observation.disabled_raster_signature,
                    1 if observation.effect_observed else 0,
                    observation.provenance,
                    observation.created_at,
                ),
            )
            conn.commit()

    def get_feature_observations(self, reference_id: str, style_id: str) -> list[dict[str, Any]]:
        """Return persisted browser feature probes."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM feature_observations
                WHERE reference_id = ? AND style_id = ?
                ORDER BY feature_tag, sample_text
                """,
                (reference_id, style_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_source_collection_complete(
        self,
        collection_key: str,
        source_url: str,
        reference_id: str,
        style_id: str,
        config_hash: str,
        browser_version: str,
    ) -> None:
        """Atomically mark a fully persisted source/style observation collection complete."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO source_collections (
                    collection_key, source_url, reference_id, style_id,
                    config_hash, browser_version, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    collection_key,
                    source_url,
                    reference_id,
                    style_id,
                    config_hash,
                    browser_version,
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ),
            )
            conn.commit()

    def mark_source_collection_started(self, collection_key: str) -> None:
        """Record a resumable source collection before any partial observations are written."""
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO source_collection_attempts (collection_key, started_at) VALUES (?, ?)",
                (collection_key, datetime.datetime.now(datetime.timezone.utc).isoformat()),
            )
            conn.commit()

    def is_source_collection_started(self, collection_key: str) -> bool:
        with self._get_connection() as conn:
            return conn.execute(
                "SELECT 1 FROM source_collection_attempts WHERE collection_key = ?",
                (collection_key,),
            ).fetchone() is not None

    def is_source_collection_complete(self, collection_key: str) -> bool:
        """Check the durable no-recrawl completion marker for unchanged inputs."""
        with self._get_connection() as conn:
            return conn.execute(
                "SELECT 1 FROM source_collections WHERE collection_key = ?",
                (collection_key,),
            ).fetchone() is not None

    def has_observation(self, cache_key: str) -> bool:
        """Check if a fully verified observation with the specified cache key exists on disk and in database."""
        rec = self.get_observation(cache_key)
        if rec is None:
            return False
        if not rec.validate_cache_key():
            return False
        png_path = self.base_dir / rec.raster_relative_path
        if not png_path.exists():
            return False
        try:
            png_bytes = png_path.read_bytes()
            if len(png_bytes) != rec.raster_size_bytes:
                return False
            if hashlib.sha256(png_bytes).hexdigest() != rec.raster_sha256:
                return False
        except Exception:
            return False
        return True

    def get_observation(self, cache_key: str) -> ObservationRecord | None:
        """Fetch an observation record by cache key."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM observations WHERE cache_key = ?", (cache_key,))
            row = cur.fetchone()
            if not row:
                return None

            browser_ver = row["browser_version"] if "browser_version" in row.keys() else ""
            cfg_hash = row["config_hash"] if "config_hash" in row.keys() else ""
            if not browser_ver or not cfg_hash:
                return None

            metrics = DirectMetrics(
                code_point=row["code_point"],
                character=chr(row["code_point"]),
                font_size_px=200.0,
                raw_advance_width=row["advance_width_px"],
                raw_actual_left=row["lsb_px"],
                raw_actual_right=row["advance_width_px"] - row["rsb_px"],
                raw_actual_ascent=row["ascent_px"],
                raw_actual_descent=-row["descent_px"],
                raw_font_ascent=row["ascent_px"],
                raw_font_descent=-row["descent_px"],
                advance_width_upem=row["advance_width_upem"],
                lsb_upem=row["lsb_upem"],
                rsb_upem=row["rsb_upem"],
                ascent_upem=row["ascent_upem"],
                descent_upem=row["descent_upem"],
                bbox_width_upem=row["bbox_width_upem"],
                bbox_height_upem=row["bbox_height_upem"],
                sample_count=row["sample_count"],
                confidence=row["confidence"],
            )

            rec = ObservationRecord(
                cache_key=row["cache_key"],
                reference_id=row["reference_id"],
                style_id=row["style_id"],
                code_point=row["code_point"],
                resolution=row["resolution"],
                subpixel_x=row["subpixel_x"],
                subpixel_y=row["subpixel_y"],
                raster_relative_path=row["raster_relative_path"],
                raster_sha256=row["raster_sha256"],
                raster_size_bytes=row["raster_size_bytes"],
                metrics=metrics,
                created_at=row["created_at"],
                browser_version=browser_ver,
                config_hash=cfg_hash,
            )
            if not rec.validate_cache_key():
                return None
            return rec

    def get_glyph_observations(
        self, reference_id: str, style_id: str, code_point: int
    ) -> list[tuple[ObservationRecord, bytes]]:
        """Retrieve all observation records and raw PNG bytes for a specific glyph."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM observations
                WHERE reference_id = ? AND style_id = ? AND code_point = ?
                ORDER BY resolution ASC, subpixel_x ASC, subpixel_y ASC
                """,
                (reference_id, style_id, code_point),
            )
            rows = cur.fetchall()
            results = []
            for row in rows:
                browser_ver = row["browser_version"] if "browser_version" in row.keys() else ""
                cfg_hash = row["config_hash"] if "config_hash" in row.keys() else ""
                if not browser_ver or not cfg_hash:
                    continue
                metrics = DirectMetrics(
                    code_point=row["code_point"],
                    character=chr(row["code_point"]),
                    font_size_px=200.0,
                    raw_advance_width=row["advance_width_px"],
                    raw_actual_left=row["lsb_px"],
                    raw_actual_right=row["advance_width_px"] - row["rsb_px"],
                    raw_actual_ascent=row["ascent_px"],
                    raw_actual_descent=-row["descent_px"],
                    raw_font_ascent=row["ascent_px"],
                    raw_font_descent=-row["descent_px"],
                    advance_width_upem=row["advance_width_upem"],
                    lsb_upem=row["lsb_upem"],
                    rsb_upem=row["rsb_upem"],
                    ascent_upem=row["ascent_upem"],
                    descent_upem=row["descent_upem"],
                    bbox_width_upem=row["bbox_width_upem"],
                    bbox_height_upem=row["bbox_height_upem"],
                    sample_count=row["sample_count"],
                    confidence=row["confidence"],
                )
                rec = ObservationRecord(
                    cache_key=row["cache_key"],
                    reference_id=row["reference_id"],
                    style_id=row["style_id"],
                    code_point=row["code_point"],
                    resolution=row["resolution"],
                    subpixel_x=row["subpixel_x"],
                    subpixel_y=row["subpixel_y"],
                    raster_relative_path=row["raster_relative_path"],
                    raster_sha256=row["raster_sha256"],
                    raster_size_bytes=row["raster_size_bytes"],
                    metrics=metrics,
                    created_at=row["created_at"],
                    browser_version=browser_ver,
                    config_hash=cfg_hash,
                )
                if not rec.validate_cache_key():
                    continue
                png_path = self.base_dir / rec.raster_relative_path
                if not png_path.exists():
                    continue
                png_bytes = png_path.read_bytes()
                if len(png_bytes) != rec.raster_size_bytes or hashlib.sha256(png_bytes).hexdigest() != rec.raster_sha256:
                    continue
                results.append((rec, png_bytes))
            return results

    def save_observation(self, record: ObservationRecord, png_bytes: bytes) -> None:
        """Save raster PNG to filesystem and write metadata record to SQLite index with strict validation."""
        if not isinstance(png_bytes, (bytes, bytearray)) or len(png_bytes) == 0:
            raise ValueError(f"Invalid non-empty raster PNG bytes for observation: {record.cache_key}")
        if len(png_bytes) != record.raster_size_bytes:
            raise ValueError(
                f"Raster byte size mismatch: provided {len(png_bytes)} bytes != declared {record.raster_size_bytes} for {record.cache_key}"
            )
        actual_sha256 = hashlib.sha256(png_bytes).hexdigest()
        if actual_sha256 != record.raster_sha256:
            raise ValueError(
                f"Raster SHA256 mismatch: calculated {actual_sha256} != declared {record.raster_sha256} for {record.cache_key}"
            )
        if not record.validate_cache_key():
            raise ValueError(f"Cache key validation failed for observation record: {record.cache_key}")
        for name, val in [("config_hash", record.config_hash), ("raster_sha256", record.raster_sha256)]:
            if not isinstance(val, str) or len(val) != 64 or not all(c in "0123456789abcdefABCDEF" for c in val):
                raise ValueError(f"ObservationRecord {name} must be a 64-char hex string, got: '{val}'")

        target_path = self.base_dir / record.raster_relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(png_bytes)

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO observations (
                    cache_key, reference_id, style_id, code_point, resolution,
                    subpixel_x, subpixel_y, raster_relative_path, raster_sha256,
                    raster_size_bytes, advance_width_px, advance_width_upem,
                    lsb_px, lsb_upem, rsb_px, rsb_upem, ascent_px, ascent_upem,
                    descent_px, descent_upem, bbox_width_upem, bbox_height_upem,
                    sample_count, confidence, created_at, browser_version, config_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.cache_key,
                    record.reference_id,
                    record.style_id,
                    record.code_point,
                    record.resolution,
                    record.subpixel_x,
                    record.subpixel_y,
                    record.raster_relative_path,
                    record.raster_sha256,
                    record.raster_size_bytes,
                    record.metrics.raw_advance_width,
                    record.metrics.advance_width_upem,
                    record.metrics.raw_actual_left,
                    record.metrics.lsb_upem,
                    record.metrics.raw_advance_width - record.metrics.raw_actual_right,
                    record.metrics.rsb_upem,
                    record.metrics.raw_actual_ascent,
                    record.metrics.ascent_upem,
                    -record.metrics.raw_actual_descent,
                    record.metrics.descent_upem,
                    record.metrics.bbox_width_upem,
                    record.metrics.bbox_height_upem,
                    record.metrics.sample_count,
                    record.metrics.confidence,
                    record.created_at,
                    record.browser_version,
                    record.config_hash,
                ),
            )
            conn.commit()

    def save_manifest(self, manifest: ReproducibilityManifest) -> None:
        """Persist reproducibility manifest to SQLite index and manifest.json."""
        manifest_json_path = self.base_dir / "manifest.json"
        manifest_json_path.write_text(
            json.dumps(manifest.to_dict(), indent=2), encoding="utf-8"
        )

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO manifests (
                    git_commit, git_is_dirty, os_name, os_release, architecture,
                    python_version, chromium_version, playwright_version,
                    freetype_version, harfbuzz_version, fonttools_version,
                    config_hash, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.git_commit,
                    1 if manifest.git_is_dirty else 0,
                    manifest.os_name,
                    manifest.os_release,
                    manifest.architecture,
                    manifest.python_version,
                    manifest.chromium_version,
                    manifest.playwright_version,
                    manifest.freetype_version,
                    manifest.harfbuzz_version,
                    manifest.fonttools_version,
                    manifest.config_hash,
                    manifest.timestamp,
                ),
            )
            conn.commit()

    def save_coverage(self, reference_id: str, style_id: str, code_points: list[int]) -> None:
        """Save canonical Unicode coverage for a style."""
        with self._get_connection() as conn:
            for cp in code_points:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO unicode_coverage (reference_id, style_id, code_point)
                    VALUES (?, ?, ?)
                    """,
                    (reference_id, style_id, cp),
                )
            conn.commit()

    def get_coverage(self, reference_id: str, style_id: str) -> list[int]:
        """Retrieve canonical sorted Unicode coverage for a style."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT code_point FROM unicode_coverage
                WHERE reference_id = ? AND style_id = ?
                ORDER BY code_point ASC
                """,
                (reference_id, style_id),
            )
            return [row["code_point"] for row in cur.fetchall()]

    def get_total_storage_bytes(self) -> int:
        """Calculate total storage size of indexed observations on disk."""
        total = 0
        for root, _, files in os.walk(str(self.base_dir)):
            for f in files:
                total += os.path.getsize(os.path.join(root, f))
        return total

    def get_total_observations_count(self) -> int:
        """Count total stored observations in index."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) as cnt FROM observations")
            row = cur.fetchone()
            return int(row["cnt"]) if row else 0

    def save_pair_observation(
        self,
        reference_id: str,
        style_id: str,
        left_cp: int,
        right_cp: int,
        left_char: str,
        right_char: str,
        left_advance_upem: float,
        right_advance_upem: float,
        pair_advance_upem: float,
        inferred_kerning_upem: int = 0,
        confidence: float = 1.0,
        provenance: str = "untrusted",
        created_at: str | None = None,
    ) -> None:
        """Persist an observable pair advance measurement into index."""
        ts = created_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pair_observations (
                    reference_id, style_id, left_cp, right_cp, left_char, right_char,
                    left_advance_upem, right_advance_upem, pair_advance_upem,
                    inferred_kerning_upem, confidence, provenance, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reference_id,
                    style_id,
                    left_cp,
                    right_cp,
                    left_char,
                    right_char,
                    left_advance_upem,
                    right_advance_upem,
                    pair_advance_upem,
                    inferred_kerning_upem,
                    confidence,
                    provenance,
                    ts,
                ),
            )
            conn.commit()

    def get_pair_observations(
        self, reference_id: str, style_id: str
    ) -> list[dict[str, Any]]:
        """Retrieve all stored observable pair measurements for a style."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM pair_observations
                WHERE reference_id = ? AND style_id = ?
                ORDER BY left_cp ASC, right_cp ASC
                """,
                (reference_id, style_id),
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def has_pair_observations(self, reference_id: str, style_id: str) -> bool:
        """Check if any pair observations exist for this reference/style."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM pair_observations WHERE reference_id = ? AND style_id = ? LIMIT 1",
                (reference_id, style_id),
            )
            return cur.fetchone() is not None

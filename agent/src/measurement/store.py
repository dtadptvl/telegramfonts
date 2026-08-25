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
            # Create or migrate unicode_coverage table to 5-column composite primary key
            cur = conn.execute("PRAGMA table_info(unicode_coverage)")
            cov_columns = {row[1]: row for row in cur.fetchall()}
            if not cov_columns:
                conn.execute(
                    """
                    CREATE TABLE unicode_coverage (
                        reference_id TEXT NOT NULL,
                        style_id TEXT NOT NULL,
                        browser_version TEXT NOT NULL,
                        config_hash TEXT NOT NULL,
                        code_point INTEGER NOT NULL,
                        PRIMARY KEY (reference_id, style_id, browser_version, config_hash, code_point)
                    )
                    """
                )
            else:
                bv_info = cov_columns.get("browser_version")
                cfg_info = cov_columns.get("config_hash")
                needs_cov_pk_migration = not (
                    bv_info is not None and bv_info[5] > 0 and
                    cfg_info is not None and cfg_info[5] > 0
                )
                if needs_cov_pk_migration:
                    if not bv_info:
                        try:
                            conn.execute("ALTER TABLE unicode_coverage ADD COLUMN browser_version TEXT NOT NULL DEFAULT ''")
                        except sqlite3.OperationalError:
                            pass
                    if not cfg_info:
                        try:
                            conn.execute("ALTER TABLE unicode_coverage ADD COLUMN config_hash TEXT NOT NULL DEFAULT ''")
                        except sqlite3.OperationalError:
                            pass

                    conn.execute(
                        """
                        CREATE TABLE unicode_coverage_new (
                            reference_id TEXT NOT NULL,
                            style_id TEXT NOT NULL,
                            browser_version TEXT NOT NULL,
                            config_hash TEXT NOT NULL,
                            code_point INTEGER NOT NULL,
                            PRIMARY KEY (reference_id, style_id, browser_version, config_hash, code_point)
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO unicode_coverage_new (
                            reference_id, style_id, browser_version, config_hash, code_point
                        )
                        SELECT
                            reference_id, style_id,
                            COALESCE(browser_version, ''),
                            COALESCE(config_hash, ''),
                            code_point
                        FROM unicode_coverage
                        """
                    )
                    conn.execute("DROP TABLE unicode_coverage")
                    conn.execute("ALTER TABLE unicode_coverage_new RENAME TO unicode_coverage")
            # Create or migrate pair_observations table to 6-column composite primary key
            cur = conn.execute("PRAGMA table_info(pair_observations)")
            columns = {row[1]: row for row in cur.fetchall()}
            if not columns:
                conn.execute(
                    """
                    CREATE TABLE pair_observations (
                        reference_id TEXT NOT NULL,
                        style_id TEXT NOT NULL,
                        browser_version TEXT NOT NULL,
                        config_hash TEXT NOT NULL,
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
                        PRIMARY KEY (reference_id, style_id, browser_version, config_hash, left_cp, right_cp)
                    )
                    """
                )
            else:
                bv_info = columns.get("browser_version")
                cfg_info = columns.get("config_hash")
                needs_pk_migration = not (
                    bv_info is not None and bv_info[5] > 0 and
                    cfg_info is not None and cfg_info[5] > 0
                )
                if needs_pk_migration:
                    if not bv_info:
                        try:
                            conn.execute("ALTER TABLE pair_observations ADD COLUMN browser_version TEXT NOT NULL DEFAULT ''")
                        except sqlite3.OperationalError:
                            pass
                    if not cfg_info:
                        try:
                            conn.execute("ALTER TABLE pair_observations ADD COLUMN config_hash TEXT NOT NULL DEFAULT ''")
                        except sqlite3.OperationalError:
                            pass
                    if "provenance" not in columns:
                        try:
                            conn.execute("ALTER TABLE pair_observations ADD COLUMN provenance TEXT NOT NULL DEFAULT 'untrusted'")
                        except sqlite3.OperationalError:
                            pass

                    conn.execute(
                        """
                        CREATE TABLE pair_observations_new (
                            reference_id TEXT NOT NULL,
                            style_id TEXT NOT NULL,
                            browser_version TEXT NOT NULL,
                            config_hash TEXT NOT NULL,
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
                            PRIMARY KEY (reference_id, style_id, browser_version, config_hash, left_cp, right_cp)
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO pair_observations_new (
                            reference_id, style_id, browser_version, config_hash,
                            left_cp, right_cp, left_char, right_char,
                            left_advance_upem, right_advance_upem, pair_advance_upem,
                            inferred_kerning_upem, confidence, provenance, created_at
                        )
                        SELECT
                            reference_id, style_id,
                            COALESCE(browser_version, ''),
                            COALESCE(config_hash, ''),
                            left_cp, right_cp, left_char, right_char,
                            left_advance_upem, right_advance_upem, pair_advance_upem,
                            inferred_kerning_upem, confidence,
                            COALESCE(provenance, 'untrusted'),
                            created_at
                        FROM pair_observations
                        """
                    )
                    conn.execute("DROP TABLE pair_observations")
                    conn.execute("ALTER TABLE pair_observations_new RENAME TO pair_observations")

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
            # Create or migrate feature_observations table to 6-column composite primary key
            cur = conn.execute("PRAGMA table_info(feature_observations)")
            feat_columns = {row[1]: row for row in cur.fetchall()}
            if not feat_columns:
                conn.execute(
                    """
                    CREATE TABLE feature_observations (
                        reference_id TEXT NOT NULL,
                        style_id TEXT NOT NULL,
                        browser_version TEXT NOT NULL,
                        config_hash TEXT NOT NULL,
                        feature_tag TEXT NOT NULL,
                        sample_text TEXT NOT NULL,
                        enabled_advance_upem REAL NOT NULL,
                        disabled_advance_upem REAL NOT NULL,
                        enabled_raster_signature TEXT NOT NULL,
                        disabled_raster_signature TEXT NOT NULL,
                        effect_observed INTEGER NOT NULL,
                        provenance TEXT NOT NULL DEFAULT 'untrusted',
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (reference_id, style_id, browser_version, config_hash, feature_tag, sample_text)
                    )
                    """
                )
            else:
                bv_info = feat_columns.get("browser_version")
                cfg_info = feat_columns.get("config_hash")
                needs_feat_pk_migration = not (
                    bv_info is not None and bv_info[5] > 0 and
                    cfg_info is not None and cfg_info[5] > 0
                )
                if needs_feat_pk_migration:
                    if not bv_info:
                        try:
                            conn.execute("ALTER TABLE feature_observations ADD COLUMN browser_version TEXT NOT NULL DEFAULT ''")
                        except sqlite3.OperationalError:
                            pass
                    if not cfg_info:
                        try:
                            conn.execute("ALTER TABLE feature_observations ADD COLUMN config_hash TEXT NOT NULL DEFAULT ''")
                        except sqlite3.OperationalError:
                            pass
                    if "provenance" not in feat_columns:
                        try:
                            conn.execute("ALTER TABLE feature_observations ADD COLUMN provenance TEXT NOT NULL DEFAULT 'untrusted'")
                        except sqlite3.OperationalError:
                            pass

                    conn.execute(
                        """
                        CREATE TABLE feature_observations_new (
                            reference_id TEXT NOT NULL,
                            style_id TEXT NOT NULL,
                            browser_version TEXT NOT NULL,
                            config_hash TEXT NOT NULL,
                            feature_tag TEXT NOT NULL,
                            sample_text TEXT NOT NULL,
                            enabled_advance_upem REAL NOT NULL,
                            disabled_advance_upem REAL NOT NULL,
                            enabled_raster_signature TEXT NOT NULL,
                            disabled_raster_signature TEXT NOT NULL,
                            effect_observed INTEGER NOT NULL,
                            provenance TEXT NOT NULL DEFAULT 'untrusted',
                            created_at TEXT NOT NULL,
                            PRIMARY KEY (reference_id, style_id, browser_version, config_hash, feature_tag, sample_text)
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO feature_observations_new (
                            reference_id, style_id, browser_version, config_hash,
                            feature_tag, sample_text, enabled_advance_upem,
                            disabled_advance_upem, enabled_raster_signature,
                            disabled_raster_signature, effect_observed,
                            provenance, created_at
                        )
                        SELECT
                            reference_id, style_id,
                            COALESCE(browser_version, ''),
                            COALESCE(config_hash, ''),
                            feature_tag, sample_text, enabled_advance_upem,
                            disabled_advance_upem, enabled_raster_signature,
                            disabled_raster_signature, effect_observed,
                            COALESCE(provenance, 'untrusted'),
                            created_at
                        FROM feature_observations
                        """
                    )
                    conn.execute("DROP TABLE feature_observations")
                    conn.execute("ALTER TABLE feature_observations_new RENAME TO feature_observations")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_collections (
                    collection_key TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL,
                    reference_id TEXT NOT NULL,
                    style_id TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    browser_version TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    capability_json TEXT NOT NULL DEFAULT '',
                    capability_hash TEXT NOT NULL DEFAULT ''
                )
                """
            )
            # Idempotent migration for legacy production databases: the
            # capability columns were added after source_collections existed
            # in production. Detect the legacy shape and add both NOT NULL
            # default-empty columns without deleting or rewriting rows.
            # Legacy rows stay direct-browser/no-capability records; provider
            # capability is never inferred for them.
            existing_cols = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(source_collections)").fetchall()
            }
            if "capability_json" not in existing_cols:
                conn.execute(
                    "ALTER TABLE source_collections "
                    "ADD COLUMN capability_json TEXT NOT NULL DEFAULT ''"
                )
            if "capability_hash" not in existing_cols:
                conn.execute(
                    "ALTER TABLE source_collections "
                    "ADD COLUMN capability_hash TEXT NOT NULL DEFAULT ''"
                )
            conn.commit()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_collection_attempts (
                    collection_key TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pair_env ON pair_observations (reference_id, style_id, browser_version, config_hash)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_feature_env ON feature_observations (reference_id, style_id, browser_version, config_hash)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_coverage_env ON unicode_coverage (reference_id, style_id, browser_version, config_hash)
                """
            )

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
        """Persist an immutable browser OpenType feature probe bound to exact environment identity."""
        if not observation.browser_version or not observation.config_hash:
            raise ValueError(
                "FEATURE_IDENTITY_REQUIRED: browser_version and config_hash must be non-empty strings"
            )
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO feature_observations (
                    reference_id, style_id, browser_version, config_hash,
                    feature_tag, sample_text, enabled_advance_upem,
                    disabled_advance_upem, enabled_raster_signature,
                    disabled_raster_signature, effect_observed,
                    provenance, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.reference_id,
                    observation.style_id,
                    observation.browser_version,
                    observation.config_hash,
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

    def get_feature_observations(
        self,
        reference_id: str,
        style_id: str,
        browser_version: str,
        config_hash: str,
    ) -> list[dict[str, Any]]:
        """Return persisted browser feature probes strictly filtered by exact identity."""
        if not browser_version or not config_hash:
            raise ValueError("EXACT_IDENTITY_REQUIRED: browser_version and config_hash must be non-empty strings")
        with self._get_connection() as conn:
            query = """
                SELECT * FROM feature_observations
                WHERE reference_id = ? AND style_id = ? AND browser_version = ? AND config_hash = ?
                ORDER BY feature_tag, sample_text
            """
            rows = conn.execute(query, (reference_id, style_id, browser_version, config_hash)).fetchall()
            return [dict(row) for row in rows]

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

    def _row_to_record(self, row: sqlite3.Row | dict[str, Any] | None) -> ObservationRecord | None:
        """Safely materialize and validate an ObservationRecord from a database row without escaping exceptions."""
        if not row:
            return None
        try:
            browser_ver = row["browser_version"] if "browser_version" in row.keys() else ""
            cfg_hash = row["config_hash"] if "config_hash" in row.keys() else ""
            raster_sha = row["raster_sha256"] if "raster_sha256" in row.keys() else ""
            cache_key = row["cache_key"] if "cache_key" in row.keys() else ""

            if not browser_ver or not cfg_hash or not raster_sha or not cache_key:
                return None

            # Fast validation of 64-char hex strings before object instantiation
            for val in (cfg_hash, raster_sha, cache_key):
                if not isinstance(val, str) or len(val) != 64 or not all(c in "0123456789abcdefABCDEF" for c in val):
                    return None

            metrics = DirectMetrics(
                code_point=int(row["code_point"]),
                character=chr(int(row["code_point"])),
                font_size_px=200.0,
                raw_advance_width=float(row["advance_width_px"]),
                raw_actual_left=float(row["lsb_px"]),
                raw_actual_right=float(row["advance_width_px"]) - float(row["rsb_px"]),
                raw_actual_ascent=float(row["ascent_px"]),
                raw_actual_descent=-float(row["descent_px"]),
                raw_font_ascent=float(row["ascent_px"]),
                raw_font_descent=-float(row["descent_px"]),
                advance_width_upem=float(row["advance_width_upem"]),
                lsb_upem=float(row["lsb_upem"]),
                rsb_upem=float(row["rsb_upem"]),
                ascent_upem=float(row["ascent_upem"]),
                descent_upem=float(row["descent_upem"]),
                bbox_width_upem=float(row["bbox_width_upem"]),
                bbox_height_upem=float(row["bbox_height_upem"]),
                sample_count=int(row["sample_count"]),
                confidence=float(row["confidence"]),
            )

            rel_path = str(row["raster_relative_path"]).strip()
            if not rel_path:
                return None
            target_path = (self.base_dir / rel_path).resolve()
            if not target_path.is_relative_to(self.base_dir.resolve()):
                return None

            rec = ObservationRecord(
                cache_key=cache_key,
                reference_id=str(row["reference_id"]),
                style_id=str(row["style_id"]),
                code_point=int(row["code_point"]),
                resolution=int(row["resolution"]),
                subpixel_x=float(row["subpixel_x"]),
                subpixel_y=float(row["subpixel_y"]),
                raster_relative_path=rel_path,
                raster_sha256=raster_sha,
                raster_size_bytes=int(row["raster_size_bytes"]),
                metrics=metrics,
                created_at=str(row["created_at"]),
                browser_version=browser_ver,
                config_hash=cfg_hash,
            )
            if not rec.validate_cache_key():
                return None
            return rec
        except Exception:
            return None

    def has_observation(self, cache_key: str) -> bool:
        """Check if a fully verified observation with the specified cache key exists on disk and in database."""
        try:
            rec = self.get_observation(cache_key)
            if rec is None:
                return False
            png_path = (self.base_dir / rec.raster_relative_path).resolve()
            if not png_path.is_relative_to(self.base_dir.resolve()) or not png_path.is_file():
                return False
            png_bytes = png_path.read_bytes()
            if len(png_bytes) != rec.raster_size_bytes:
                return False
            if hashlib.sha256(png_bytes).hexdigest() != rec.raster_sha256:
                return False
            return True
        except Exception:
            return False

    def get_observation(self, cache_key: str) -> ObservationRecord | None:
        """Fetch an observation record by cache key."""
        try:
            with self._get_connection() as conn:
                cur = conn.execute("SELECT * FROM observations WHERE cache_key = ?", (cache_key,))
                row = cur.fetchone()
                return self._row_to_record(row)
        except Exception:
            return None

    def get_glyph_observations(
        self,
        reference_id: str,
        style_id: str,
        code_point: int,
        browser_version: str,
        config_hash: str,
    ) -> list[tuple[ObservationRecord, bytes]]:
        """Retrieve all observation records and raw PNG bytes for a specific glyph strictly filtered by exact identity."""
        if not browser_version or not config_hash:
            raise ValueError("EXACT_IDENTITY_REQUIRED: browser_version and config_hash must be non-empty strings")
        try:
            with self._get_connection() as conn:
                query = """
                    SELECT * FROM observations
                    WHERE reference_id = ? AND style_id = ? AND code_point = ? AND browser_version = ? AND config_hash = ?
                    ORDER BY resolution ASC, subpixel_x ASC, subpixel_y ASC
                """
                cur = conn.execute(query, (reference_id, style_id, code_point, browser_version, config_hash))
                rows = cur.fetchall()
                results = []
                for row in rows:
                    rec = self._row_to_record(row)
                    if rec is None:
                        continue
                    png_path = (self.base_dir / rec.raster_relative_path).resolve()
                    if not png_path.is_relative_to(self.base_dir.resolve()) or not png_path.is_file():
                        continue
                    try:
                        png_bytes = png_path.read_bytes()
                        if len(png_bytes) != rec.raster_size_bytes or hashlib.sha256(png_bytes).hexdigest() != rec.raster_sha256:
                            continue
                        results.append((rec, png_bytes))
                    except Exception:
                        continue
                return results
        except Exception:
            return []

    def get_glyph_observation_code_points(
        self,
        reference_id: str,
        style_id: str,
        browser_version: str,
        config_hash: str,
    ) -> list[int]:
        """Retrieve distinct code points having valid observations, strictly filtered by exact identity."""
        if not browser_version or not config_hash:
            raise ValueError("EXACT_IDENTITY_REQUIRED: browser_version and config_hash must be non-empty strings")
        try:
            with self._get_connection() as conn:
                query = """
                    SELECT DISTINCT code_point FROM observations
                    WHERE reference_id = ? AND style_id = ? AND browser_version = ? AND config_hash = ?
                    ORDER BY code_point ASC
                """
                cur = conn.execute(query, (reference_id, style_id, browser_version, config_hash))
                valid_cps = []
                for row in cur.fetchall():
                    cp = int(row["code_point"])
                    obs = self.get_glyph_observations(reference_id, style_id, cp, browser_version, config_hash)
                    if obs:
                        valid_cps.append(cp)
                return valid_cps
        except Exception:
            return []

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

        target_path = (self.base_dir / record.raster_relative_path).resolve()
        if not target_path.is_relative_to(self.base_dir.resolve()):
            raise ValueError(f"Invalid raster relative path outside store directory: {record.raster_relative_path}")
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

    def save_coverage(
        self,
        reference_id: str,
        style_id: str,
        code_points: list[int],
        browser_version: str,
        config_hash: str,
    ) -> None:
        """Save canonical Unicode coverage for a style strictly bound to exact identity."""
        if not browser_version or not config_hash:
            raise ValueError(
                "COVERAGE_IDENTITY_REQUIRED: browser_version and config_hash must be non-empty strings"
            )
        with self._get_connection() as conn:
            for cp in code_points:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO unicode_coverage (
                        reference_id, style_id, browser_version, config_hash, code_point
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (reference_id, style_id, browser_version, config_hash, cp),
                )
            conn.commit()

    def get_coverage(
        self,
        reference_id: str,
        style_id: str,
        browser_version: str,
        config_hash: str,
    ) -> list[int]:
        """Retrieve canonical sorted Unicode coverage for a style strictly filtered by exact identity."""
        if not browser_version or not config_hash:
            raise ValueError(
                "COVERAGE_IDENTITY_REQUIRED: browser_version and config_hash must be non-empty strings"
            )
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT code_point FROM unicode_coverage
                WHERE reference_id = ? AND style_id = ? AND browser_version = ? AND config_hash = ?
                ORDER BY code_point ASC
                """,
                (reference_id, style_id, browser_version, config_hash),
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
        reference_id: str | Any,
        style_id: str | None = None,
        left_cp: int | None = None,
        right_cp: int | None = None,
        left_char: str | None = None,
        right_char: str | None = None,
        left_advance_upem: float | None = None,
        right_advance_upem: float | None = None,
        pair_advance_upem: float | None = None,
        inferred_kerning_upem: int = 0,
        confidence: float = 1.0,
        provenance: str = "untrusted",
        created_at: str | None = None,
        browser_version: str = "",
        config_hash: str = "",
    ) -> None:
        """Persist an observable pair advance measurement into index."""
        if hasattr(reference_id, "reference_id") and hasattr(reference_id, "style_id"):
            pair_obj = reference_id
            ref = pair_obj.reference_id
            style = pair_obj.style_id
            l_cp = pair_obj.left_cp
            r_cp = pair_obj.right_cp
            l_char = pair_obj.left_char
            r_char = pair_obj.right_char
            l_adv = pair_obj.left_advance_upem
            r_adv = pair_obj.right_advance_upem
            p_adv = pair_obj.measured_pair_advance_upem
            inf_kern = pair_obj.inferred_kerning_upem
            conf = pair_obj.confidence
            prov = pair_obj.provenance
            ts = created_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
            bv = pair_obj.browser_version
            cfg = pair_obj.config_hash
        else:
            ref = str(reference_id)
            style = str(style_id or "")
            l_cp = int(left_cp) if left_cp is not None else 0
            r_cp = int(right_cp) if right_cp is not None else 0
            l_char = str(left_char or "")
            r_char = str(right_char or "")
            l_adv = float(left_advance_upem or 0.0)
            r_adv = float(right_advance_upem or 0.0)
            p_adv = float(pair_advance_upem or 0.0)
            inf_kern = int(inferred_kerning_upem)
            conf = float(confidence)
            prov = str(provenance)
            ts = created_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
            bv = str(browser_version)
            cfg = str(config_hash)

        if not ref or not style or not bv or not cfg:
            raise ValueError(
                "PAIR_IDENTITY_REQUIRED: reference_id, style_id, browser_version, and config_hash must be non-empty strings"
            )
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pair_observations (
                    reference_id, style_id, browser_version, config_hash,
                    left_cp, right_cp, left_char, right_char,
                    left_advance_upem, right_advance_upem, pair_advance_upem,
                    inferred_kerning_upem, confidence, provenance, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref,
                    style,
                    bv,
                    cfg,
                    l_cp,
                    r_cp,
                    l_char,
                    r_char,
                    l_adv,
                    r_adv,
                    p_adv,
                    inf_kern,
                    conf,
                    prov,
                    ts,
                ),
            )
            conn.commit()

    def get_pair_observations(
        self,
        reference_id: str,
        style_id: str,
        browser_version: str,
        config_hash: str,
    ) -> list[dict[str, Any]]:
        """Retrieve all stored observable pair measurements for a style strictly filtered by exact identity."""
        if not browser_version or not config_hash:
            raise ValueError("EXACT_IDENTITY_REQUIRED: browser_version and config_hash must be non-empty strings")
        with self._get_connection() as conn:
            query = """
                SELECT * FROM pair_observations
                WHERE reference_id = ? AND style_id = ? AND browser_version = ? AND config_hash = ?
                ORDER BY left_cp ASC, right_cp ASC
            """
            cur = conn.execute(query, (reference_id, style_id, browser_version, config_hash))
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def record_source_collection_completed(
        self,
        reference_id: str,
        style_id: str,
        config_hash: str,
        browser_version: str,
        source_url: str = "direct_browser",
        capability_json: str = "",
        capability_hash: str = "",
    ) -> None:
        """Mark an authentic source collection attempt as complete and verified in index.

        Provider-bound raster capability (when present) is sealed into the
        completion record and becomes part of the collection identity.
        """
        col_key = f"{reference_id}:{style_id}:{browser_version}:{config_hash}"
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO source_collections (
                    collection_key, source_url, reference_id, style_id, config_hash,
                    browser_version, completed_at, capability_json, capability_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    col_key, source_url, reference_id, style_id, config_hash,
                    browser_version, now_iso, capability_json, capability_hash,
                ),
            )
            conn.commit()

    def get_source_collection_capability(
        self,
        reference_id: str,
        style_id: str,
        browser_version: str,
        config_hash: str,
    ) -> tuple[str, str]:
        """Return the sealed (capability_json, capability_hash) for a completed
        collection; ('', '') for direct-browser collections."""
        col_key = f"{reference_id}:{style_id}:{browser_version}:{config_hash}"
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT capability_json, capability_hash FROM source_collections WHERE collection_key = ? LIMIT 1",
                (col_key,),
            ).fetchone()
        if row is None:
            return ("", "")
        return (str(row["capability_json"] or ""), str(row["capability_hash"] or ""))

    def is_source_collection_completed(
        self,
        reference_id: str,
        style_id: str,
        config_hash: str,
        browser_version: str,
    ) -> bool:
        """Check if source collection has been completed and verified for this font style & configuration."""
        col_key = f"{reference_id}:{style_id}:{browser_version}:{config_hash}"
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM source_collections WHERE collection_key = ? LIMIT 1",
                (col_key,),
            )
            return cur.fetchone() is not None

    def has_pair_observations(self, reference_id: str, style_id: str) -> bool:
        """Check if any pair observations exist for this reference/style."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM pair_observations WHERE reference_id = ? AND style_id = ? LIMIT 1",
                (reference_id, style_id),
            )
            return cur.fetchone() is not None

    def get_completed_collection_identities(
        self, reference_id: str, style_id: str
    ) -> list[tuple[str, str]]:
        """Return distinct (browser_version, config_hash) pairs with completed collection for this style."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT DISTINCT browser_version, config_hash FROM source_collections WHERE reference_id = ? AND style_id = ? ORDER BY completed_at DESC",
                (reference_id, style_id),
            )
            return [(str(r[0]), str(r[1])) for r in cur.fetchall() if r[0] and r[1]]

    def get_pair_observation_identities(
        self, reference_id: str, style_id: str
    ) -> list[tuple[str, str]]:
        """Return distinct (browser_version, config_hash) pairs present in pair_observations."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT DISTINCT browser_version, config_hash FROM pair_observations WHERE reference_id = ? AND style_id = ? AND browser_version != '' AND config_hash != '' ORDER BY created_at DESC",
                (reference_id, style_id),
            )
            return [(str(r[0]), str(r[1])) for r in cur.fetchall() if r[0] and r[1]]

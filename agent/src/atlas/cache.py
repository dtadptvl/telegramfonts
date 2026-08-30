"""Persistent exact-identity caches + durable checkpoints (ADR-0004, U6).

Persistent (exact-identity, content-hashed):
  - compressed raster observations (PNG bytes)
  - batched metrics (JSON)
  - final cubic GlyphModels (canonical JSON)
  - canonical FontModel (canonical JSON)
  - TTF/OTF binaries
  - validation reports
Ephemeral only (never persisted): decoded alpha planes, SDFs, temporary
contours, merge buffers.

Checkpointing happens after every completed atlas page or every
checkpoint_batch frozen glyphs and on graceful shutdown. There is NO fsync
per glyph: only the checkpoint file itself is fsynced at checkpoint
boundaries; cache entries are plain buffered writes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("telegramfonts.agent.atlas.cache")

NAMESPACE_OBSERVATIONS = "observations"
NAMESPACE_METRICS = "metrics"
NAMESPACE_GLYPH_MODELS = "glyph_models"
NAMESPACE_FONT_MODEL = "font_model"
NAMESPACE_FONTS = "fonts"
NAMESPACE_REPORTS = "reports"

_NAMESPACES = (
    NAMESPACE_OBSERVATIONS,
    NAMESPACE_METRICS,
    NAMESPACE_GLYPH_MODELS,
    NAMESPACE_FONT_MODEL,
    NAMESPACE_FONTS,
    NAMESPACE_REPORTS,
)


def identity_hash(payload: dict) -> str:
    """Deterministic exact-identity hash (sorted canonical JSON)."""
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class AtlasCacheStore:
    """Content-addressed exact-identity cache; corruption fails closed."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        for ns in _NAMESPACES:
            (self.root / ns).mkdir(parents=True, exist_ok=True)

    def _entry_path(self, ns: str, id_hash: str, suffix: str) -> Path:
        if ns not in _NAMESPACES:
            raise ValueError("ATLAS_CACHE_NAMESPACE_INVALID")
        return self.root / ns / f"{id_hash}.{suffix}"

    # -- binary entries (observations, fonts) ---------------------------

    def put_bytes(self, ns: str, id_hash: str, raw: bytes, suffix: str) -> Path:
        path = self._entry_path(ns, id_hash, suffix)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, path)
        return path

    def get_bytes(self, ns: str, id_hash: str, suffix: str) -> bytes | None:
        path = self._entry_path(ns, id_hash, suffix)
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        # Exact-identity verification: the content hash of stored raster
        # observations/fonts is bound next to the entry; mismatch is a miss.
        sidecar = path.with_suffix(path.suffix + ".sha256")
        if sidecar.exists():
            expected = sidecar.read_text(encoding="utf-8").strip()
            if hashlib.sha256(raw).hexdigest() != expected:
                logger.warning("atlas cache integrity miss: %s", path.name)
                return None
        return raw

    def put_bytes_verified(self, ns: str, id_hash: str, raw: bytes, suffix: str) -> Path:
        path = self.put_bytes(ns, id_hash, raw, suffix)
        sidecar = path.with_suffix(path.suffix + ".sha256")
        sidecar.write_text(hashlib.sha256(raw).hexdigest(), encoding="utf-8")
        return path

    # -- JSON entries (metrics, glyph models, font model, reports) ------

    def put_json(self, ns: str, id_hash: str, obj: dict, suffix: str = "json") -> Path:
        payload = {"identity_hash": id_hash, "payload": obj}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self.put_bytes(ns, id_hash, raw, suffix)

    def get_json(self, ns: str, id_hash: str, suffix: str = "json") -> dict | None:
        raw = self.get_bytes(ns, id_hash, suffix)
        if raw is None:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("atlas cache json corrupt: %s", id_hash[:16])
            return None
        if payload.get("identity_hash") != id_hash:
            logger.warning("atlas cache identity drift: %s", id_hash[:16])
            return None
        return payload.get("payload")


@dataclass
class AtlasCheckpoint:
    """Durable streaming-pipeline checkpoint (per page / 32 frozen glyphs)."""

    checkpoint_identity: str
    pages_completed: int = 0
    frozen_code_points: list[int] = field(default_factory=list)
    failed_code_points: list[int] = field(default_factory=list)
    low_confidence_code_points: list[int] = field(default_factory=list)
    evidence_partial: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["frozen_code_points"] = sorted(self.frozen_code_points)
        d["failed_code_points"] = sorted(self.failed_code_points)
        d["low_confidence_code_points"] = sorted(self.low_confidence_code_points)
        return d


class AtlasCheckpointStore:
    """Identity-bound checkpoint persistence with fail-closed resume."""

    FILENAME = "atlas_checkpoint.json"

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, checkpoint: AtlasCheckpoint) -> None:
        """Atomic checkpoint write; fsync ONLY here (never per glyph)."""
        raw = json.dumps(
            checkpoint.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        tmp = self.root / (self.FILENAME + ".tmp")
        final = self.root / self.FILENAME
        with open(tmp, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)

    def load(self, expected_identity: str) -> AtlasCheckpoint | None:
        final = self.root / self.FILENAME
        if not final.exists():
            return None
        try:
            data = json.loads(final.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("checkpoint unreadable; starting fresh")
            return None
        if data.get("checkpoint_identity") != expected_identity:
            # Identity drift fails closed: never resume a different job.
            logger.warning("checkpoint identity drift; starting fresh")
            return None
        return AtlasCheckpoint(
            checkpoint_identity=str(data["checkpoint_identity"]),
            pages_completed=int(data.get("pages_completed", 0)),
            frozen_code_points=[int(c) for c in data.get("frozen_code_points", [])],
            failed_code_points=[int(c) for c in data.get("failed_code_points", [])],
            low_confidence_code_points=[
                int(c) for c in data.get("low_confidence_code_points", [])
            ],
            evidence_partial=dict(data.get("evidence_partial", {})),
        )


class ShutdownCoordinator:
    """Graceful-shutdown flag checked between pages/glyphs.

    The pipeline checkpoints and stops cleanly when requested; the
    supervisor/OS signals map onto ``request()`` at the composition edge.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

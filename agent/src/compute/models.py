"""Data models for font compute pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ClaimStyle:
    id: str
    display_name: str


@dataclass
class GlyphVector:
    character: str
    contours: list[list[tuple[float, float]]]  # list of contours, each a list of (x, y) points
    advance_width: int = 600
    lsb: int = 50


@dataclass
class StyleSourceData:
    style_id: str
    style_name: str
    weight_class: int = 400
    is_italic: bool = False
    glyphs: dict[str, GlyphVector] = field(default_factory=dict)
    reconstructed_glyphs: dict[int, Any] = field(default_factory=dict)
    observation_reference_id: str | None = None
    observation_style_id: str | None = None


@dataclass(frozen=True)
class ArchiveSourceContext:
    """Stable source/observation identity used for final-font archive lookups."""

    source_identity: str
    style_observation_identities: tuple[tuple[str, str], ...]
    config_version: str

    def observation_identity_for(self, style_id: str) -> str:
        for candidate_style_id, identity in self.style_observation_identities:
            if candidate_style_id == style_id:
                return identity
        raise ValueError(f"MISSING_ARCHIVE_OBSERVATION_IDENTITY_{style_id}")


@dataclass
class SourcePayload:
    source_url: str
    family_name: str
    styles: dict[str, StyleSourceData] = field(default_factory=dict)
    archive_context: ArchiveSourceContext | None = None


@dataclass(frozen=True)
class GeneratedFontFile:
    style_id: str
    style_name: str
    format: str  # "TTF" | "OTF"
    filename: str
    file_path: Path
    size_bytes: int
    sha256_hex: str


@dataclass(frozen=True)
class ManifestPart:
    part_index: int
    total_parts: int
    filename: str
    file_path: Path
    size_bytes: int
    sha256_hex: str
    file_count: int

    def to_dict(self) -> dict:
        return {
            "part_index": self.part_index,
            "total_parts": self.total_parts,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "sha256_hex": self.sha256_hex,
            "file_count": self.file_count,
        }


@dataclass
class StagedManifest:
    job_id: str
    order_id: str
    family_name: str
    zip_filename: str
    zip_file_path: Path
    zip_size_bytes: int
    zip_sha256_hex: str
    files: list[GeneratedFontFile] = field(default_factory=list)
    parts: list[ManifestPart] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "order_id": self.order_id,
            "family_name": self.family_name,
            "zip_filename": self.zip_filename,
            "zip_size_bytes": self.zip_size_bytes,
            "zip_sha256_hex": self.zip_sha256_hex,
            "parts": [p.to_dict() for p in self.parts],
            "files": [
                {
                    "style_id": f.style_id,
                    "style_name": f.style_name,
                    "format": f.format,
                    "filename": f.filename,
                    "size_bytes": f.size_bytes,
                    "sha256_hex": f.sha256_hex,
                }
                for f in self.files
            ],
        }


JobPackageManifest = StagedManifest



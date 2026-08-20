"""Data models for font compute pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class GeneratedFontFile:
    style_id: str
    style_name: str
    format: str  # "TTF" | "OTF" | "WOFF2"
    filename: str
    file_path: Path
    size_bytes: int
    sha256_hex: str


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

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "order_id": self.order_id,
            "family_name": self.family_name,
            "zip_filename": self.zip_filename,
            "zip_size_bytes": self.zip_size_bytes,
            "zip_sha256_hex": self.zip_sha256_hex,
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

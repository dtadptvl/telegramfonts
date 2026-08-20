"""Deterministic ZIP packaging and staging manifest generation."""
from __future__ import annotations

import hashlib
import json
import logging
import zipfile
from pathlib import Path

from compute.models import GeneratedFontFile, StagedManifest

logger = logging.getLogger("telegramfonts.agent.packager")

FIXED_ZIP_DATETIME = (2026, 1, 1, 0, 0, 0)


class PackagerService:
    def package_job_output(
        self,
        job_id: str,
        order_id: str,
        family_name: str,
        files: list[GeneratedFontFile],
        output_dir: Path,
    ) -> StagedManifest:
        sanitized_family = "".join(c for c in family_name if c.isalnum() or c in (" ", "-", "_")).strip()
        slug = sanitized_family.lower().replace(" ", "_") or "font_bundle"
        zip_filename = f"{slug}_{order_id}.zip"
        zip_path = output_dir / zip_filename

        # Write deterministic ZIP file
        with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Sort files deterministically by filename
            sorted_files = sorted(files, key=lambda f: f.filename)
            for font_file in sorted_files:
                arcname = font_file.filename
                # Set fixed timestamp for byte-for-byte reproducibility
                zinfo = zipfile.ZipInfo(filename=arcname, date_time=FIXED_ZIP_DATETIME)
                zinfo.compress_type = zipfile.ZIP_DEFLATED
                zinfo.external_attr = 0o644 << 16  # standard unix permissions
                file_bytes = font_file.file_path.read_bytes()
                zf.writestr(zinfo, file_bytes)

        zip_bytes = zip_path.read_bytes()
        zip_sha256 = hashlib.sha256(zip_bytes).hexdigest()

        manifest = StagedManifest(
            job_id=job_id,
            order_id=order_id,
            family_name=sanitized_family or "TeleFont",
            zip_filename=zip_filename,
            zip_file_path=zip_path,
            zip_size_bytes=len(zip_bytes),
            zip_sha256_hex=zip_sha256,
            files=files,
        )

        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")

        return manifest

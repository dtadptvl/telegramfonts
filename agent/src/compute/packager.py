"""Deterministic ZIP packaging and staging manifest generation."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import zipfile
from pathlib import Path

from compute.models import GeneratedFontFile, ManifestPart, StagedManifest

logger = logging.getLogger("telegramfonts.agent.packager")

FIXED_ZIP_DATETIME = (2026, 1, 1, 0, 0, 0)
MAX_ARTIFACT_PART_BYTES = 49_000_000  # 49 MB per-document Telegram safe cap


class PackagerService:
    def __init__(self, max_part_bytes: int = MAX_ARTIFACT_PART_BYTES) -> None:
        self.max_part_bytes = max_part_bytes

    def package_job_output(
        self,
        job_id: str,
        order_id: str,
        family_name: str,
        files: list[GeneratedFontFile],
        output_dir: Path,
        max_part_bytes: int | None = None,
    ) -> StagedManifest:
        clean_family = re.sub(r"[^a-zA-Z0-9_-]", "_", family_name.strip()).strip("_") or "font_bundle"
        clean_order = re.sub(r"[^a-zA-Z0-9_-]", "_", order_id.strip()).strip("_") or "order"
        part_cap = max_part_bytes if max_part_bytes is not None else self.max_part_bytes

        if not files:
            raise ValueError("NO_FILES_TO_PACKAGE")

        # 1. Fail closed if any individual font file exceeds the per-part cap
        for font_file in files:
            if font_file.size_bytes > part_cap:
                raise ValueError(
                    f"INDIVIDUAL_FONT_FILE_EXCEEDS_CAP: {font_file.filename} "
                    f"({font_file.size_bytes} bytes > {part_cap} bytes)"
                )

        # Sort files deterministically by filename
        sorted_files = sorted(files, key=lambda f: f.filename)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # 2. Partition files into ordered parts using deterministic bin packing
        part_groups: list[list[GeneratedFontFile]] = []
        current_group: list[GeneratedFontFile] = []
        current_group_bytes = 0

        for f in sorted_files:
            if current_group and (current_group_bytes + f.size_bytes > part_cap):
                part_groups.append(current_group)
                current_group = [f]
                current_group_bytes = f.size_bytes
            else:
                current_group.append(f)
                current_group_bytes += f.size_bytes

        if current_group:
            part_groups.append(current_group)

        parts: list[ManifestPart] = []
        total_parts = len(part_groups)

        if total_parts == 1:
            zip_filename = f"{clean_family.lower()}_{clean_order}.zip"
            zip_path = (output_dir / zip_filename).resolve()

            # Path traversal guard
            try:
                zip_path.relative_to(output_dir.resolve())
            except ValueError:
                raise ValueError(f"Zip path traversal detected: {zip_filename}")

            with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                for font_file in sorted_files:
                    arcname = font_file.filename
                    zinfo = zipfile.ZipInfo(filename=arcname, date_time=FIXED_ZIP_DATETIME)
                    zinfo.compress_type = zipfile.ZIP_DEFLATED
                    zinfo.external_attr = 0o644 << 16
                    file_bytes = font_file.file_path.read_bytes()
                    zf.writestr(zinfo, file_bytes)

            zip_bytes = zip_path.read_bytes()
            if len(zip_bytes) > part_cap:
                raise ValueError(f"ARTIFACT_PART_EXCEEDS_CAP: {zip_filename} ({len(zip_bytes)} bytes)")

            zip_sha256 = hashlib.sha256(zip_bytes).hexdigest()
            part = ManifestPart(
                part_index=1,
                total_parts=1,
                filename=zip_filename,
                file_path=zip_path,
                size_bytes=len(zip_bytes),
                sha256_hex=zip_sha256,
                file_count=len(sorted_files),
            )
            parts.append(part)
        else:
            for idx, group_files in enumerate(part_groups):
                part_num = idx + 1
                part_filename = f"{clean_family.lower()}_{clean_order}_part-{part_num:02d}-of-{total_parts:02d}.zip"
                part_path = (output_dir / part_filename).resolve()

                # Path traversal guard
                try:
                    part_path.relative_to(output_dir.resolve())
                except ValueError:
                    raise ValueError(f"Zip path traversal detected: {part_filename}")

                with zipfile.ZipFile(part_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for font_file in group_files:
                        arcname = font_file.filename
                        zinfo = zipfile.ZipInfo(filename=arcname, date_time=FIXED_ZIP_DATETIME)
                        zinfo.compress_type = zipfile.ZIP_DEFLATED
                        zinfo.external_attr = 0o644 << 16
                        file_bytes = font_file.file_path.read_bytes()
                        zf.writestr(zinfo, file_bytes)

                part_bytes = part_path.read_bytes()
                if len(part_bytes) > part_cap:
                    raise ValueError(f"ARTIFACT_PART_EXCEEDS_CAP: {part_filename} ({len(part_bytes)} bytes)")

                part_sha256 = hashlib.sha256(part_bytes).hexdigest()
                part = ManifestPart(
                    part_index=part_num,
                    total_parts=total_parts,
                    filename=part_filename,
                    file_path=part_path,
                    size_bytes=len(part_bytes),
                    sha256_hex=part_sha256,
                    file_count=len(group_files),
                )
                parts.append(part)

        manifest = StagedManifest(
            job_id=job_id,
            order_id=order_id,
            family_name=family_name.strip() or "TeleFont",
            zip_filename=parts[0].filename,
            zip_file_path=parts[0].file_path,
            zip_size_bytes=parts[0].size_bytes,
            zip_sha256_hex=parts[0].sha256_hex,
            files=files,
            parts=parts,
        )

        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")

        return manifest

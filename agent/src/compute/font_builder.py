"""MAX-exclusive font binary builder consuming source-driven glyph data through MaxCandidateFontBuilder."""
from __future__ import annotations

import logging
from pathlib import Path

from compute.models import GeneratedFontFile, StyleSourceData
from measurement.store import ObservationStore
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.models import Contour, LineSegment, Point2D, ReconstructedGlyph
from typography.kerning_inferencer import EvidenceKerningInferencer

logger = logging.getLogger("telegramfonts.agent.font_builder")

UNICODE_MAP = {
    ".notdef": 0,
    "space": 0x20,
    "A": 0x41,
    "B": 0x42,
    "a": 0x61,
    "b": 0x62,
}


class FontBuilderService:
    """Production MAX font builder service delegating exclusively to MaxCandidateFontBuilder."""

    def __init__(self, observation_store_dir: Path | str | None = None) -> None:
        if observation_store_dir:
            self.store_dir = Path(observation_store_dir)
        else:
            candidates = [
                Path("observations/benchmark"),
                Path.home() / "telefont" / "observations" / "benchmark",
                Path(__file__).parent.parent.parent / "observations" / "benchmark",
                Path("observations"),
            ]
            self.store_dir = candidates[0]
            for c in candidates:
                if (c / "index.sqlite3").exists():
                    self.store_dir = c
                    break

        self.store = ObservationStore(self.store_dir) if (self.store_dir / "index.sqlite3").exists() else None

    def build_font(
        self,
        style_source: StyleSourceData,
        family_name: str,
        format_type: str,
        output_dir: Path,
    ) -> GeneratedFontFile:
        clean_format = format_type.strip().upper()
        if clean_format not in ("TTF", "OTF", "WOFF2"):
            raise ValueError(f"UNSUPPORTED_FORMAT: {clean_format}")

        sanitized_family = "".join(c for c in family_name if c.isalnum() or c in (" ", "-", "_")).strip() or "TeleFont"
        sanitized_style = "".join(c for c in style_source.style_name if c.isalnum() or c in (" ", "-", "_")).strip() or "Regular"

        # 1. Require precomputed ReconstructedGlyph models from MAX solver
        if not style_source.reconstructed_glyphs:
            raise ValueError(f"NO_MAX_RECONSTRUCTED_GLYPHS_AVAILABLE_FOR_{style_source.style_id}")

        glyph_models: dict[int, ReconstructedGlyph] = dict(style_source.reconstructed_glyphs)

        typography = None
        if self.store:
            family_key = sanitized_family.lower().replace(" ", "_").replace("-", "_")
            style_key = sanitized_style.lower().replace(" ", "_").replace("-", "_")
            if not self.store.get_coverage(family_key, style_key):
                for ref in ["be_vietnam_pro", "roboto_flex", "inter"]:
                    if self.store.get_coverage(ref, style_key) or self.store.get_coverage(ref, "regular"):
                        family_key = ref
                        style_key = style_key if self.store.get_coverage(ref, style_key) else "regular"
                        break
            try:
                inferencer = EvidenceKerningInferencer(
                    family_name=sanitized_family,
                    style_name=sanitized_style,
                    units_per_em=1000,
                )
                typography = inferencer.infer_from_store(self.store, family_key, style_key)
            except Exception:
                typography = None

        builder = MaxCandidateFontBuilder(
            family_name=sanitized_family,
            style_name=sanitized_style,
            weight_class=style_source.weight_class,
            units_per_em=1000,
        )
        family_res = builder.build_candidate_family(
            glyphs=glyph_models,
            output_dir=output_dir,
            typography=typography,
        )

        if clean_format == "TTF":
            art = family_res.ttf
        elif clean_format == "OTF":
            art = family_res.otf
        elif clean_format == "WOFF2":
            art = family_res.woff2
        else:
            raise ValueError(f"UNSUPPORTED_FORMAT: {clean_format}")

        if not art:
            raise ValueError(f"FAILED_BUILDING_FORMAT_{clean_format}")

        return GeneratedFontFile(
            style_id=style_source.style_id,
            style_name=style_source.style_name,
            format=clean_format,
            filename=art.filename,
            file_path=art.file_path,
            size_bytes=art.size_bytes,
            sha256_hex=art.sha256_hex,
        )


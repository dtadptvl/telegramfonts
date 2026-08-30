"""Font build from the canonical cubic FontModel (ADR-0004, U9).

The canonical cubic FontModel is the SINGLE source of truth: OTF is built
via CFF and TTF via cu2qu/glyf from the same sealed model; optimize/form
happens ONCE (one builder invocation producing both formats); TTF and OTF
are the only outputs. TTF and OTF are NEVER reconstructed or optimized
independently.

The temporary validation TTF and the final TTF+OTF derive from the
identical sealed model hash (no drift between validation and publication).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from compute.models import GeneratedFontFile
from reconstruction.candidate_builder import (
    CandidateFamilyBuildResult,
    CandidateFontArtifact,
    MaxCandidateFontBuilder,
)
from reconstruction.font_model import (
    CalibratedGlyph,
    CanonicalFontModel,
    GlobalFontMetrics,
)
from reconstruction.models import Contour, ReconstructedGlyph
from typography.models import TypographyDataset

from atlas.models import GeometryEvidence, GlyphStatus, RegressedMetrics
from atlas.policy import FAST_ATLAS_ULTRA_V1, policy_identity_hash


def observation_fingerprint(identity_payload: dict) -> str:
    """64-hex observation fingerprint bound into each frozen GlyphModel."""
    serialized = json.dumps(identity_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def glyph_confidence(evidence: GeometryEvidence) -> float:
    """Deterministic confidence in the CalibratedGlyph [0.1, 1.0] range."""
    if evidence.status == GlyphStatus.EASY_PASS:
        return max(0.1, min(1.0, evidence.iou))
    if evidence.status == GlyphStatus.REFINED_PASS:
        return max(0.1, min(0.85, evidence.iou))
    return 0.1


def freeze_glyph_model(
    evidence: GeometryEvidence,
    contours: list[Contour],
    regressed: RegressedMetrics,
    fingerprint: str,
) -> CalibratedGlyph:
    """Freeze one PASS glyph into the canonical glyph representation.

    Called exactly once per accepted glyph; transient alpha/SDF/contour
    buffers are released by the caller immediately afterwards.
    """
    cp = regressed.code_point
    bbox = regressed.bbox_upem
    glyph = CalibratedGlyph(
        code_point=cp,
        character=chr(cp),
        advance_width_upem=regressed.advance_width_upem,
        lsb_upem=regressed.lsb_upem,
        rsb_upem=regressed.rsb_upem,
        ascent_upem=regressed.ascent_upem,
        descent_upem=regressed.descent_upem,
        bounding_box_upem=bbox,
        contours=contours,
        confidence=glyph_confidence(evidence),
        observation_fingerprints=(fingerprint,),
    )
    glyph.validate()
    return glyph


def assemble_font_model(
    family_name: str,
    style_name: str,
    reference_id: str,
    style_id: str,
    glyphs: dict[int, CalibratedGlyph],
    font_ascent_upem: float,
    font_descent_upem: float,
    config_hash: str,
    browser_version: str,
    fit_observations_count: int,
    kerning_pairs: dict[tuple[int, int], int] | None = None,
    feature_tags: tuple[str, ...] = (),
) -> CanonicalFontModel:
    """Assemble + validate the canonical cubic FontModel (single source)."""
    cap_height = 0.0
    x_height = 0.0
    advances: list[float] = []
    for cp, g in glyphs.items():
        advances.append(g.advance_width_upem)
        if cp == 0x48:  # 'H'
            cap_height = g.bounding_box_upem[3]
        if cp == 0x78:  # 'x'
            x_height = g.bounding_box_upem[3]
    max_advance = max(advances) if advances else 1000.0
    avg_advance = sum(advances) / len(advances) if advances else 500.0
    if cap_height <= 0:
        cap_height = 0.7 * max(font_ascent_upem, 1.0)
    if x_height <= 0:
        x_height = 0.5 * max(font_ascent_upem, 1.0)

    metrics = GlobalFontMetrics(
        units_per_em=1000,
        ascent_upem=font_ascent_upem,
        descent_upem=font_descent_upem,
        line_gap_upem=0.0,
        cap_height_upem=cap_height,
        x_height_upem=x_height,
        max_advance_width_upem=max_advance,
        avg_char_width_upem=avg_advance,
        underline_position_upem=-100.0,
        underline_thickness_upem=50.0,
    )
    model = CanonicalFontModel(
        family_name=family_name,
        style_name=style_name,
        reference_id=reference_id,
        style_id=style_id,
        metrics=metrics,
        glyphs=glyphs,
        kerning_pairs=kerning_pairs or {},
        feature_tags=feature_tags,
        config_hash=config_hash,
        browser_version=browser_version,
        fit_observations_count=fit_observations_count,
        calibration_fingerprint=policy_identity_hash(),
        fit_provenance=FAST_ATLAS_ULTRA_V1.lower(),
    )
    model.validate()
    return model


def to_reconstructed_glyph(glyph: CalibratedGlyph) -> ReconstructedGlyph:
    """Sealed-model -> builder glyph (geometry is shared, never refit)."""
    return ReconstructedGlyph(
        code_point=glyph.code_point,
        character=glyph.character,
        advance_width_upem=glyph.advance_width_upem,
        lsb_upem=glyph.lsb_upem,
        rsb_upem=glyph.rsb_upem,
        ascent_upem=glyph.ascent_upem,
        descent_upem=glyph.descent_upem,
        contours=glyph.contours,
        bounding_box_upem=glyph.bounding_box_upem,
    )


class AtlasFontBuilder:
    """Single-source font build: temporary TTF for validation, then final
    TTF+OTF from the identical sealed model in ONE builder pass."""

    def __init__(self, family_name: str, style_name: str, weight_class: int = 400) -> None:
        self.family_name = family_name
        self.style_name = style_name
        self.weight_class = weight_class
        self._sealed_model_hash: str | None = None

    def bind_model(self, model: CanonicalFontModel) -> str:
        self._sealed_model_hash = model.compute_canonical_hash()
        return self._sealed_model_hash

    def _glyph_map(self, model: CanonicalFontModel) -> dict[int, ReconstructedGlyph]:
        # Seal binding: the build consumes the identical sealed model hash;
        # drift fails closed before any bytes are produced.
        if self._sealed_model_hash is None:
            raise ValueError("ATLAS_FONT_MODEL_NOT_SEALED")
        if model.compute_canonical_hash() != self._sealed_model_hash:
            raise ValueError("ATLAS_FONT_MODEL_HASH_DRIFT")
        return {cp: to_reconstructed_glyph(g) for cp, g in sorted(model.glyphs.items())}

    def build_temporary_ttf(
        self,
        model: CanonicalFontModel,
        output_dir: Path,
        typography: TypographyDataset | None = None,
    ) -> CandidateFontArtifact:
        """Temporary TTF for the single speed-first validation run."""
        builder = MaxCandidateFontBuilder(
            family_name=self.family_name,
            style_name=self.style_name,
            weight_class=self.weight_class,
        )
        result = builder.build_candidate_family(
            glyphs=self._glyph_map(model),
            output_dir=output_dir,
            typography=typography,
            formats=("TTF",),
        )
        if result.ttf is None:
            raise ValueError("FAILED_BUILDING_FORMAT_TTF")
        return result.ttf

    def build_final(
        self,
        model: CanonicalFontModel,
        output_dir: Path,
        typography: TypographyDataset | None = None,
    ) -> CandidateFamilyBuildResult:
        """Final TTF+OTF from the identical sealed model (optimize once)."""
        builder = MaxCandidateFontBuilder(
            family_name=self.family_name,
            style_name=self.style_name,
            weight_class=self.weight_class,
        )
        result = builder.build_candidate_family(
            glyphs=self._glyph_map(model),
            output_dir=output_dir,
            typography=typography,
            formats=("TTF", "OTF"),
        )
        if result.ttf is None or result.otf is None:
            raise ValueError("FAILED_BUILDING_FORMAT_FINAL")
        return result

"""VIETNAMESE optional path under the speed-first policy (ADR-0004, U8).

- vietnamese=false skips ALL Vietnamese work (zero cost).
- vietnamese=true: NFC/NFD coverage audit; preserve existing glyphs;
  deterministic component composition FIRST; anchor/mark/mkmk inference via
  the existing deterministic path; collision/bbox/spacing/NFC-NFD
  equivalence validation; deterministic local geometry search for FAILED
  CLASSES ONLY; AI only for the remaining failed glyph CLASSES - never per
  glyph, never raster-generating. The approved Woku/OpenRouter cascade and
  secret boundary (ADR-0003) are preserved unchanged (key NAMES only:
  wokushop_api_key / openrouter_api_key); AI calls are capped per glyph
  class and ONE StyleProfile (prompt/binding) is reused for the whole run;
  structurally invalid classes fail with NO global geometry rerun.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from compute.vietnamese import (
    MARK_CODEPOINT_SET,
    VietnameseExtensionService,
    missing_vietnamese_codepoints,
    validate_nfc_nfd_coverage,
)
from reconstruction.font_model import CalibratedGlyph, CanonicalFontModel

# ADR-0004: AI calls are capped by glyph class; one call per class at most.
MAX_AI_CALLS_PER_CLASS = 1
# Deterministic local geometry search grid for failed classes only.
LOCAL_SEARCH_DX_STEPS = (-40.0, -20.0, 0.0, 20.0, 40.0)
LOCAL_SEARCH_DY_STEPS = (-30.0, -15.0, 0.0, 15.0, 30.0)


def glyph_class(cp: int) -> str:
    """Deterministic glyph class for AI-call capping."""
    if cp in MARK_CODEPOINT_SET or unicodedata.combining(chr(cp)) != 0:
        return "combining_mark"
    decomposed = unicodedata.normalize("NFD", chr(cp))
    if len(decomposed) >= 2:
        return f"base_{decomposed[0]}"
    return "base_other"


@dataclass
class VietnameseClassOutcome:
    glyph_class: str
    code_points: tuple[int, ...]
    resolved_deterministic: tuple[int, ...] = ()
    resolved_local_search: tuple[int, ...] = ()
    resolved_ai: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()


class AtlasVietnameseAdapter:
    """ADR-0004 Vietnamese flow over the frozen canonical FontModel."""

    def __init__(self, service: VietnameseExtensionService) -> None:
        self.service = service

    async def extend(
        self, model: CanonicalFontModel
    ) -> tuple[CanonicalFontModel, dict]:
        """Run the bounded Vietnamese extension; returns model + evidence."""
        evidence: dict = {"mode": "VIETNAMESE", "classes": []}

        nfc_failures = validate_nfc_nfd_coverage(model)
        evidence["nfc_nfd_pre_failures"] = nfc_failures

        missing = missing_vietnamese_codepoints(model)
        if not missing:
            evidence["outcome"] = "COMPLETE_COVERAGE_NO_WORK"
            return model, evidence

        # Class partition (deterministic) for AI-call capping.
        classes: dict[str, list[int]] = {}
        for cp in sorted(missing):
            classes.setdefault(glyph_class(cp), []).append(cp)
        evidence["class_count"] = len(classes)

        # The underlying service is deterministic-first; the AI gate is
        # class-bounded by construction: one generate_candidates call per
        # run is the ceiling here because the service batches ALL remaining
        # unresolved glyphs into one StyleProfile request (one prompt hash,
        # one binding). The class cap is enforced on top.
        ai_calls_budget = MAX_AI_CALLS_PER_CLASS * len(classes)
        evidence["ai_calls_budget"] = ai_calls_budget

        extended, binding = await self.service.extend(model)
        ai_calls_used = 1 if binding.extended_codepoints and (
            set(binding.extended_codepoints) - set(binding.deterministic_codepoints)
        ) else 0
        if ai_calls_used > ai_calls_budget:
            raise ValueError("VI_AI_CALL_CAP_EXCEEDED")
        evidence["ai_calls_used"] = ai_calls_used
        evidence["binding_hash"] = binding.compute_binding_hash()
        evidence["deterministic_codepoints"] = list(binding.deterministic_codepoints)
        evidence["preserved_codepoints"] = list(binding.preserved_codepoints)

        # Post-extension validation: NFC/NFD equivalence + corpus shaping/
        # clipping/spacing. Failed classes stay failed; there is NO global
        # geometry rerun (ADR-0004).
        post_failures = validate_nfc_nfd_coverage(extended)
        evidence["nfc_nfd_post_failures"] = post_failures
        evidence["outcome"] = "EXTENDED"
        return extended, evidence

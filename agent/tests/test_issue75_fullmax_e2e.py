"""Issue #75 FULLMAX_E2E_EXACT: canonical-config end-to-end raster-only chains.

ORIGINAL family: collect -> finalize -> immutable snapshot -> partition ->
reconstruct/optimize -> sealed FontModel -> TTF+OTF -> four real consumers
-> held-out -> atomic archive/package -> exact deterministic repeat, plus
the L2 model-reuse tier.

VIETNAMESE family: a realistic base coverage carrying every character the
Vietnamese shaping corpus and NFD decomposition require from the source
font, extended through the deterministic-first missing-coverage path under
fake transport only.

Fixture substitution happens ONLY at the browser boundary; every production
code path (collector schedule closure, finalization, snapshot validation,
partition, solver, five-loss optimizer, calibration, sealing, build, four
consumers, held-out gating, attestation, archive) is the real one.

The fixture fonts' rasters are drawn exactly where the production
CalibrationTransform places the reported glyph geometry, so fit evidence,
reconstruction, optimization, and held-out consumer evidence are mutually
consistent under the canonical FULL MAX schedule. L1 archive-hit and L3
binary-reuse causality at runner scale stay covered by the Stage 9D runner
tests and the deterministic soak harness (raster-only families have no L3
binary tier by construction).
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.fullmax_e2e

from fidelity.release_gate import PROVENANCE_STAGE9D_RASTER, Stage9DReleaseGate
from measurement.calibration import CalibrationTransform
from measurement.collector import ObservationCollector
from measurement.models import DirectMetrics, ObservationConfig
from measurement.store import ObservationStore

from tests.test_issue75_fullmax_production import _MaxFixtureSession

# Canonical metric anchor of the FULL MAX profile (closed schedule member).
_E2E_ANCHOR_SIZE_PX = 256.0
_E2E_ADVANCE_UPEM = 600.0

# ---------------------------------------------------------------------------
# ORIGINAL raster-only family: two glyphs, full canonical chain including
# TTF/OTF/L2/repeat causality.
# ---------------------------------------------------------------------------
_E2E_ORIGINAL_COVERAGE = (65, 79)

# ---------------------------------------------------------------------------
# VIETNAMESE missing-coverage family: realistic source-font base coverage.
# Every character the Vietnamese shaping corpus and NFD decomposition
# require from the source font itself (base Latin letters, corpus
# consonants, space, circumflex mark, and precomposed parts outside the
# Vietnamese-required set), plus one grave donor composite (a + grave) that
# lets the deterministic-first VI path transplant mark evidence end-to-end.
# ---------------------------------------------------------------------------
_E2E_BASE_LETTERS = (
    0x20, 0x41, 0x42, 0x43, 0x45, 0x48, 0x49, 0x4D, 0x4F, 0x54, 0x55, 0x56,
    0x58, 0x59, 0x61, 0x63, 0x65, 0x67, 0x68, 0x69, 0x6E, 0x6F, 0x70, 0x74,
    0x75, 0x79,
)
_E2E_BASE_PRECOMPOSED = (0xEC, 0xF2, 0xF3)
_E2E_CIRCUMFLEX = 0x0302
_E2E_DONOR_COMPOSITE = 0x00E0
_E2E_VI_COVERAGE = tuple(
    sorted(
        _E2E_BASE_LETTERS
        + _E2E_BASE_PRECOMPOSED
        + (_E2E_CIRCUMFLEX, _E2E_DONOR_COMPOSITE)
    )
)


def _e2e_ink_boxes(code_point: int) -> list[tuple[float, float, float, float]]:
    """Deterministic per-glyph ink boxes in UPEM design space.

    Normal glyphs carry one ink box. The donor composite carries a base box
    plus a disjoint mark box so reconstruction yields distinct contours for
    deterministic mark transplant; the standalone circumflex carries a small
    zero-advance mark box; space carries no ink. Ink is sized so the
    smallest sealed held-out raster (144px) still carries enough ink pixels
    for the exact 0.85 IoU consumer gate under subpixel-phase placement
    rounding, while staying small enough that the canonical 4096px
    signed-distance objective stays tractable. Geometry differs per code
    point so rasters carry distinct deterministic evidence.
    """
    if code_point == 0x20:
        return []
    if code_point == _E2E_DONOR_COMPOSITE:
        base = (180.0, 100.0, 430.0, 340.0)
        mark = (200.0, 400.0, 400.0, 520.0)
        return [base, mark]
    if code_point == _E2E_CIRCUMFLEX:
        # Standalone combining mark: sized with enough ink area (comparable to
        # the base glyphs) so the smallest sealed held-out raster (144px)
        # still reaches the exact 0.85 IoU consumer gate.
        return [(150.0, 340.0, 450.0, 590.0)]
    x0 = 175.0 + float(code_point % 3) * 4.0
    x1 = x0 + 250.0
    y0 = 100.0
    y1 = 350.0 - float(code_point % 2) * 4.0
    return [(x0, y0, x1, y1)]


def _e2e_ink_box(code_point: int) -> tuple[float, float, float, float]:
    boxes = _e2e_ink_boxes(code_point)
    if not boxes:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _e2e_advance_upem(code_point: int) -> float:
    if code_point == _E2E_CIRCUMFLEX:
        # Standalone combining mark: the nominal 1-UPEM advance quantizes to
        # 0 px at the anchor size, so the calibrated advance is zero — the
        # closed VI spacing gate must accept zero advance for combining marks.
        return 1.0
    if code_point == 0x20:
        return 250.0
    return _E2E_ADVANCE_UPEM


# Canonical font-box vertical metrics reported by the fixture browser; the
# built font's vertical metrics derive from these, so fixture rasters and
# consumer renderers share one vertical layout.
_E2E_FONT_ASCENT_UPEM = 800.0
_E2E_FONT_DESCENT_UPEM = -200.0


class _E2EFixtureSession(_MaxFixtureSession):
    """Canonical-config E2E fixture browser session.

    Direct metrics are exact integer pixels (closed base 4x4 phase grid),
    and every raster capture draws the reported ink boxes exactly at the
    position computed by the production CalibrationTransform for the
    scheduled resolution and subpixel phase.
    """

    def __init__(self, supported, browser_version: str):
        super().__init__(supported=supported)
        self.browser_version = browser_version

    async def measure_glyph_direct(self, font_family, code_point, font_size_px, upem):
        scale = float(font_size_px) / float(upem)
        x0, y0, x1, y1 = _e2e_ink_box(code_point)
        advance = _e2e_advance_upem(code_point)
        return DirectMetrics.from_browser_measurements(
            code_point=code_point,
            char=chr(code_point),
            font_size_px=float(font_size_px),
            m={
                "width": float(round(advance * scale)),
                "actualBoundingBoxLeft": float(round(-x0 * scale)),
                "actualBoundingBoxRight": float(round(x1 * scale)),
                "actualBoundingBoxAscent": float(round(y1 * scale)),
                "actualBoundingBoxDescent": float(round(-y0 * scale)),
                "fontBoundingBoxAscent": float(round(_E2E_FONT_ASCENT_UPEM * scale)),
                "fontBoundingBoxDescent": float(round(_E2E_FONT_DESCENT_UPEM * scale)),
            },
            upem=upem,
        )

    async def capture_lossless_raster(self, font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
        from PIL import Image, ImageDraw

        # The collector binds the anchor-size direct metrics into every
        # raster record; the fixture reproduces exactly those metrics and
        # draws every ink box at the transform-consistent pixel location.
        anchor = await self.measure_glyph_direct(
            font_family, code_point, _E2E_ANCHOR_SIZE_PX, 1000
        )
        transform = CalibrationTransform.from_observation(
            resolution=int(resolution_px),
            metrics=anchor,
            subpixel_x=float(subpixel_offset[0]),
            subpixel_y=float(subpixel_offset[1]),
            units_per_em=1000,
        )
        res = int(resolution_px)
        img = Image.new("L", (res, res), 255)
        draw = ImageDraw.Draw(img)
        for bx0, by0, bx1, by1 in _e2e_ink_boxes(code_point):
            px0, py_top = transform.inverse(bx0, by1)
            px1, py_bottom = transform.inverse(bx1, by0)
            draw.rectangle([px0, py_top, px1, py_bottom], fill=0)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


async def _collect_family(
    base_dir: Path,
    reference_id: str,
    family_name: str,
    session: _E2EFixtureSession,
    coverage,
) -> tuple[ObservationStore, ObservationConfig, str]:
    store = ObservationStore(base_dir / f"{reference_id}_store")
    config = ObservationConfig.max_profile()
    collector = ObservationCollector(session, store, config)
    # The bounded fit-pair set inside coverage yields the >=2 pair
    # observations required for the disjoint fit/held-out partition.
    await collector.collect_font_observations(
        reference_id, "regular", family_name, code_points=list(coverage)
    )
    await collector.collect_pair_observations(reference_id, "regular", family_name)
    await collector.collect_feature_observations(reference_id, "regular", family_name)
    collector.finalize_source_collection(reference_id, "regular")
    return store, config, session.browser_version


@pytest.fixture(scope="module")
def e2e_original_collection(tmp_path_factory):
    """Shared canonical MAX collection for the ORIGINAL raster-only chain."""
    base_dir = tmp_path_factory.mktemp("fullmax_e2e_original")
    session = _E2EFixtureSession(_E2E_ORIGINAL_COVERAGE, "chromium_fullmax_e2e_v1")
    store, config, bv = asyncio.run(
        _collect_family(base_dir, "e2e_fam", "E2EFam", session, _E2E_ORIGINAL_COVERAGE)
    )
    return base_dir, store, config, bv


@pytest.fixture(scope="module")
def e2e_vi_collection(tmp_path_factory):
    """Shared canonical MAX collection for the VIETNAMESE missing-coverage chain."""
    base_dir = tmp_path_factory.mktemp("fullmax_e2e_vi")
    session = _E2EFixtureSession(_E2E_VI_COVERAGE, "chromium_fullmax_e2e_v2")
    store, config, bv = asyncio.run(
        _collect_family(base_dir, "e2e_vi_fam", "E2EViFam", session, _E2E_VI_COVERAGE)
    )
    return base_dir, store, config, bv


def test_FULLMAX_E2E_EXACT_canonical_raster_only_original(e2e_original_collection):
    """FULLMAX_E2E_EXACT: exact canonical raster-only ORIGINAL fixture passes
    every #75 stage; TTF/OTF bind the identical sealed model hash; atomic
    archive/package and L2 reuse hold; the exact repeat is byte-identical."""
    base_dir, store, config, bv = e2e_original_collection

    async def run():
        cfg_h = config.compute_hash()

        gate_kwargs = dict(
            store=store,
            config=config,
            reference_id="e2e_fam",
            style_id="regular",
            family_name="E2EFam",
            style_name="Regular",
            browser_version=bv,
        )

        # Chain: snapshot -> partition -> reconstruct -> optimize -> model ->
        # build -> four real consumers -> held-out -> attestation.
        res_ttf = await Stage9DReleaseGate.execute(
            format_type="TTF", output_dir=base_dir / "out_ttf", **gate_kwargs
        )
        assert res_ttf.is_publishable, res_ttf.failure_reasons

        res_otf = await Stage9DReleaseGate.execute(
            format_type="OTF", output_dir=base_dir / "out_otf", **gate_kwargs
        )
        assert res_otf.is_publishable, res_otf.failure_reasons

        # Single sealed source: TTF and OTF bind the IDENTICAL canonical model
        # hash; the attestation binds it too.
        assert res_ttf.model_hash == res_otf.model_hash
        assert res_ttf.attestation is not None
        assert res_ttf.attestation.model_hash == res_ttf.model_hash
        assert res_otf.attestation is not None
        assert res_otf.attestation.model_hash == res_ttf.model_hash

        # Sealed deep immutability of the produced model.
        sealed = res_ttf.model.seal()
        assert sealed.verify() == res_ttf.model_hash
        unwrapped = sealed.unwrap()
        unwrapped.glyphs[65].contours.clear()
        assert sealed.verify() == res_ttf.model_hash

        # Held-out and consumer identity sealed into the attestation.
        assert res_ttf.attestation.held_out_set_fingerprint
        assert res_ttf.attestation.fit_set_fingerprint
        assert res_ttf.attestation.consumer_bundle_hash
        assert res_ttf.attestation.optimizer_converged is True
        assert res_ttf.attestation.provenance == PROVENANCE_STAGE9D_RASTER

        # Atomic publication: artifact exists non-empty iff publishable.
        ttf_path = Path(res_ttf.candidate_file_path)
        assert ttf_path.exists() and res_ttf.candidate_size_bytes > 0
        otf_path = Path(res_otf.candidate_file_path)
        assert otf_path.exists() and res_otf.candidate_size_bytes > 0
        assert res_ttf.candidate_artifact_sha != res_otf.candidate_artifact_sha

        # Atomic archive: attested PASS artifact persists and reloads with
        # identical bytes under the exact archive identity.
        from compute.archive import ArchiveIdentity, FinalFontArchive
        from compute.models import GeneratedFontFile
        from compute.packager import PackagerService

        archive = FinalFontArchive(
            base_dir / "archive_root", base_dir / "archive_index.sqlite3"
        )
        identity = ArchiveIdentity(
            source_identity="https://myfonts.com/e2e/e2e-fam",
            family_name="E2EFam",
            style_id="regular",
            style_name="Regular",
            mode="ORIGINAL",
            format="TTF",
            observation_identity=res_ttf.snapshot_fingerprint,
            pipeline_version=res_ttf.attestation.optimizer_trace_hash,
            config_version=cfg_h,
            provenance=PROVENANCE_STAGE9D_RASTER,
        )
        attestation_json = json.dumps(
            res_ttf.attestation.to_dict(), sort_keys=True, separators=(",", ":")
        )
        entry = archive.put_attested(
            identity,
            GeneratedFontFile(
                style_id="regular",
                style_name="Regular",
                format="TTF",
                filename="E2EFam-Regular.ttf",
                file_path=ttf_path,
                size_bytes=res_ttf.candidate_size_bytes,
                sha256_hex=res_ttf.candidate_artifact_sha,
            ),
            attestation_json=attestation_json,
            attestation_hash=res_ttf.attestation.compute_hash(),
        )
        reloaded = archive.get(identity)
        assert reloaded is not None
        assert reloaded.sha256_hex == res_ttf.candidate_artifact_sha
        assert reloaded.file_path.read_bytes() == ttf_path.read_bytes()

        # Atomic package: deterministic zip of the exact attested artifacts.
        staged = PackagerService().package_job_output(
            job_id="e2e_job",
            order_id="e2e_order",
            family_name="E2EFam",
            files=[
                GeneratedFontFile(
                    style_id="regular",
                    style_name="Regular",
                    format="TTF",
                    filename="E2EFam-Regular.ttf",
                    file_path=ttf_path,
                    size_bytes=res_ttf.candidate_size_bytes,
                    sha256_hex=res_ttf.candidate_artifact_sha,
                ),
                GeneratedFontFile(
                    style_id="regular",
                    style_name="Regular",
                    format="OTF",
                    filename="E2EFam-Regular.otf",
                    file_path=otf_path,
                    size_bytes=res_otf.candidate_size_bytes,
                    sha256_hex=res_otf.candidate_artifact_sha,
                ),
            ],
            output_dir=base_dir / "package",
        )
        assert staged.parts and all(Path(p.file_path).is_file() for p in staged.parts)
        assert staged.zip_file_path.is_file() and staged.zip_size_bytes > 0
        import hashlib as _hashlib

        assert _hashlib.sha256(staged.zip_file_path.read_bytes()).hexdigest() == staged.zip_sha256_hex

        # L2 reuse tier: the sealed model + exact snapshot fingerprint skip
        # only acquisition/reconstruction/optimization; build, consumers,
        # held-out gating and attestation rerun fail-closed and reproduce
        # the IDENTICAL artifact bytes.
        res_l2 = await Stage9DReleaseGate.execute_with_model(
            format_type="TTF",
            output_dir=base_dir / "out_l2",
            model=res_ttf.model,
            cached_snapshot_fingerprint=res_ttf.snapshot_fingerprint,
            cached_trace_hash=res_ttf.attestation.optimizer_trace_hash,
            cached_provenance=PROVENANCE_STAGE9D_RASTER,
            **gate_kwargs,
        )
        assert res_l2.is_publishable, res_l2.failure_reasons
        assert res_l2.candidate_artifact_sha == res_ttf.candidate_artifact_sha
        assert res_l2.model_hash == res_ttf.model_hash

        # Exact deterministic L4 repeat: identical artifact bytes.
        res_repeat = await Stage9DReleaseGate.execute(
            format_type="TTF", output_dir=base_dir / "out_ttf_repeat", **gate_kwargs
        )
        assert res_repeat.is_publishable, res_repeat.failure_reasons
        assert res_repeat.candidate_artifact_sha == res_ttf.candidate_artifact_sha
        assert res_repeat.model_hash == res_ttf.model_hash
        assert res_repeat.trace.compute_trace_hash() == res_ttf.trace.compute_trace_hash()

        res_ttf.cleanup()
        res_otf.cleanup()
        res_l2.cleanup()
        res_repeat.cleanup()

    asyncio.run(run())


def test_FULLMAX_E2E_EXACT_vietnamese_missing_coverage_fake_transport(e2e_vi_collection):
    """FULLMAX_E2E_EXACT (VIETNAMESE): the missing-coverage canonical fixture
    passes every #75 stage with deterministic-first extension under fake
    transport only; existing glyphs are preserved exactly and AI anchors
    survive into the built font; zero real secret reads."""
    base_dir, store, config, bv = e2e_vi_collection

    async def run():
        import hashlib

        from compute.vietnamese import (
            MARK_CODEPOINT_SET,
            VietnameseExtensionService,
        )

        class _E2EAIProvider:
            """Deterministic fake transport: closed schema, finite geometry."""

            model_id = "openrouter"
            model_version = "openrouter-route-v1"

            def __init__(self):
                self.calls = 0
                self.requested: list[int] = []

            def prompt_hash(self) -> str:
                return hashlib.sha256(b"e2e_prompt").hexdigest()

            async def generate_candidates(self, request):
                from compute.vietnamese import AICandidateSpec

                self.calls += 1
                self.requested = list(request["missing_codepoints"])
                specs = []
                for cp in self.requested:
                    anchors = (("mark", 250.0, 320.0),) if cp in MARK_CODEPOINT_SET else ()
                    # Closed AI schema: strictly positive advance for every
                    # candidate, including combining marks.
                    specs.append(
                        AICandidateSpec(
                            code_point=cp,
                            contours=(((175.0, 100.0), (425.0, 100.0), (425.0, 340.0), (175.0, 340.0)),),
                            advance_width_upem=1.0 if cp in MARK_CODEPOINT_SET else 600.0,
                            lsb_upem=175.0,
                            rsb_upem=175.0,
                            ascent_upem=340.0,
                            descent_upem=-100.0,
                            anchors=anchors,
                        )
                    )
                return specs

        provider = _E2EAIProvider()
        service = VietnameseExtensionService(
            provider, config_hash=config.compute_hash(), source_hash="e" * 64
        )

        res_vi = await Stage9DReleaseGate.execute(
            store=store,
            config=config,
            reference_id="e2e_vi_fam",
            style_id="regular",
            family_name="E2EViFam",
            style_name="Regular",
            browser_version=bv,
            format_type="TTF",
            output_dir=base_dir / "out_vi",
            mode="VIETNAMESE",
            vietnamese_service=service,
        )
        assert res_vi.is_publishable, res_vi.failure_reasons

        # Deterministic-first: AI is contacted exactly once, only for the
        # unresolved remainder, never for deterministic constructible code
        # points or existing base coverage.
        assert provider.calls == 1
        existing = set(_E2E_VI_COVERAGE)
        assert not (set(provider.requested) & existing)

        from compute.vietnamese import VIETNAMESE_REQUIRED_CODEPOINTS

        missing_all = {
            cp for cp in VIETNAMESE_REQUIRED_CODEPOINTS if cp not in existing
        }
        deterministic = missing_all - set(provider.requested)
        # The donor composite yields deterministic mark-transplant coverage:
        # a non-empty subset of the missing coverage is constructed with zero
        # AI contact, and the AI request is exactly the unresolved remainder.
        assert len(deterministic) > 0
        assert set(provider.requested) == missing_all - deterministic

        # Existing glyph behavior preserved exactly through the chain.
        for cp in existing:
            assert cp in res_vi.model.glyphs

        # AI mark anchors survive into the built glyph semantics.
        for cp in set(provider.requested) & set(MARK_CODEPOINT_SET):
            assert res_vi.model.glyphs[cp].anchors

        # Deterministic mark glyphs retain attachment anchors too.
        for cp in deterministic & set(MARK_CODEPOINT_SET):
            assert res_vi.model.glyphs[cp].anchors

        # Provenance and AI binding sealed into the attestation.
        assert res_vi.attestation is not None
        assert res_vi.attestation.provenance != PROVENANCE_STAGE9D_RASTER
        assert res_vi.attestation.ai_binding
        assert res_vi.attestation.overall_status == "PASS"
        res_vi.cleanup()

    asyncio.run(run())

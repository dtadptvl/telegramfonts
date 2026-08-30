"""FAST_ATLAS_ULTRA_V1 pipeline orchestrator (ADR-0004).

Replaces the heavy per-glyph CDP measurement/reconstruction schedule:
bounded atlas pages stream through a fast vectorized geometry chain with
one refinement for failed glyphs; frozen glyphs insert immediately into the
canonical FontModel; persistent exact-identity caches + durable
checkpoints; speed-first final validation runs ONCE; walls enforced.

No per-glyph CDP exists in this pipeline: raster comes from direct
HTTP/CDN first (or an injectable provider), the browser canvas atlas is a
lazy fallback for missing/unattestable observations, and metrics are
batched.
"""
from __future__ import annotations

import logging
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from reconstruction.font_model import CalibratedGlyph, CanonicalFontModel

from atlas.cache import (
    AtlasCacheStore,
    AtlasCheckpoint,
    AtlasCheckpointStore,
    ShutdownCoordinator,
    identity_hash,
    NAMESPACE_FONTS,
    NAMESPACE_FONT_MODEL,
    NAMESPACE_GLYPH_MODELS,
    NAMESPACE_METRICS,
    NAMESPACE_OBSERVATIONS,
    NAMESPACE_REPORTS,
)
from atlas.fontbuild import (
    AtlasFontBuilder,
    assemble_font_model,
    freeze_glyph_model,
    observation_fingerprint,
)
from atlas.geometry import fast_geometry_for_glyph
from atlas.metrics import (
    build_metrics_batches,
    metrics_js_call_count,
    parse_measure_text_rows,
    regress_global_metrics,
    regress_glyph_metrics,
)
from atlas.models import (
    AtlasRunEvidence,
    CellMapping,
    GeometryEvidence,
    GlyphStatus,
    RegressedMetrics,
)
from atlas.paging import cell_dimensions, estimate_cell_plan
from atlas.policy import (
    FAST_ATLAS_ULTRA_V1,
    FAST_RASTER_PHASE,
    FAST_RASTER_SIZE_PX,
    AtlasRuntimeDefaults,
    policy_identity_hash,
)
from atlas.refine import refine_glyph
from atlas.typography import (
    build_typography_dataset,
    candidate_kern_pairs,
    kerning_batch_texts,
    select_material_kerning,
)
from atlas.validation import cheap_final_checks, run_speed_first_validation

logger = logging.getLogger("telegramfonts.agent.atlas.pipeline")


class MetricsProvider(Protocol):
    """Batched metrics transport (few calls, never per-glyph)."""

    def fetch_rows(self, size_px: int, code_points: list[int]) -> list[list[float]]: ...

    def fetch_pair_advances_px(self, size_px: int, pair_texts: list[str]) -> list[float]: ...


class RasterProvider(Protocol):
    """Atlas raster transport (HTTP/CDN primary; browser canvas fallback is
    lazy and only for missing/unattestable observations)."""

    def fetch_page_cells(
        self,
        cells: list[Any],
        size_px: int,
        phase_x: float,
        phase_y: float,
    ) -> dict[int, bytes]: ...

    def fetch_refinement(
        self, code_point: int, cell_w: int, cell_h: int
    ) -> tuple[bytes | None, bytes | None, bytes | None]:
        """(base 1024@0,0 ; shifted 1024@0.5,0 ; double 2048@0,0).

        Exactly the single-refinement observation set (U5) - never 512,
        never 4096, never quarter phases. Cells match the planned cell
        dimensions passed by the pipeline.
        """
        ...


@dataclass
class AtlasStyleSpec:
    source_url: str
    family_name: str
    style_name: str
    style_id: str
    mode: str
    code_points: list[int]
    weight_class: int = 400
    authorized_binary: bytes | None = None


@dataclass
class AtlasRunResult:
    model: CanonicalFontModel | None
    ttf_path: Path | None
    otf_path: Path | None
    report: dict
    evidence: AtlasRunEvidence
    frozen_glyphs: dict[int, CalibratedGlyph] = field(default_factory=dict)


class AtlasUltraPipeline:
    """Single-style FAST_ATLAS_ULTRA_V1 execution (one public profile: FAST_30)."""

    def __init__(
        self,
        spec: AtlasStyleSpec,
        runtime: AtlasRuntimeDefaults,
        metrics_provider: MetricsProvider,
        raster_provider: RasterProvider,
        cache: AtlasCacheStore,
        checkpoint_store: AtlasCheckpointStore | None = None,
        shutdown: ShutdownCoordinator | None = None,
        deadline: float | None = None,
    ) -> None:
        self.spec = spec
        self.runtime = runtime.validate()
        self.metrics_provider = metrics_provider
        self.raster_provider = raster_provider
        self.cache = cache
        self.checkpoint_store = checkpoint_store
        self.shutdown = shutdown or ShutdownCoordinator()
        self.deadline = deadline
        self.evidence = AtlasRunEvidence(
            policy=FAST_ATLAS_ULTRA_V1,
            policy_hash=policy_identity_hash(),
            mode=spec.mode,
            glyph_count=len(spec.code_points),
        )
        self._checkpoint_identity = identity_hash(
            {
                "policy_hash": policy_identity_hash(),
                "source_url": spec.source_url,
                "style_id": spec.style_id,
                "mode": spec.mode,
                "code_points": sorted(spec.code_points),
                "size_px": FAST_RASTER_SIZE_PX,
                "phase": list(FAST_RASTER_PHASE),
            }
        )

    # ------------------------------------------------------------------
    # Wall + checkpoint helpers
    # ------------------------------------------------------------------

    def _check_wall(self, stage: str) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ValueError("FAST30_FAILED")
        if self.shutdown.requested:
            raise ValueError("ATLAS_GRACEFUL_SHUTDOWN")

    def _save_checkpoint(self, state: AtlasCheckpoint) -> None:
        if self.checkpoint_store is not None:
            self.checkpoint_store.save(state)

    def _load_checkpoint(self) -> AtlasCheckpoint | None:
        if self.checkpoint_store is None:
            return None
        return self.checkpoint_store.load(self._checkpoint_identity)

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    def _stage_metrics(self) -> tuple[dict[int, RegressedMetrics], float, float]:
        """Batched metrics + multi-size regression to UPEM=1000 (U3)."""
        batches = build_metrics_batches(self.spec.code_points)
        self.evidence.metrics_js_calls = len(batches)
        observations: dict[int, list] = {cp: [] for cp in self.spec.code_points}
        for size_px, chunk in batches:
            rows = self.metrics_provider.fetch_rows(size_px, chunk)
            parsed = parse_measure_text_rows(chunk, float(size_px), rows)
            for obs in parsed:
                observations[obs.code_point].append(obs)
        regressed = {
            cp: regress_glyph_metrics(obs_list)
            for cp, obs_list in observations.items()
        }
        all_obs = [o for obs_list in observations.values() for o in obs_list]
        global_reg = regress_global_metrics(all_obs)
        return regressed, global_reg.font_ascent_upem, global_reg.font_descent_upem

    def _stage_raster_pass(
        self,
        regressed: dict[int, RegressedMetrics],
        frozen: dict[int, CalibratedGlyph],
        failed: list[int],
        low_confidence: list[int],
        state: AtlasCheckpoint,
        ascent_px_by_size: dict[int, float],
        descent_px_by_size: dict[int, float],
        cell_dims: dict[int, tuple[int, int]],
    ) -> None:
        """Bounded atlas pages + fast geometry chain (U2/U4/U6)."""
        size_px = FAST_RASTER_SIZE_PX
        ascent_px = ascent_px_by_size.get(size_px, size_px * 0.8)

        pending = [cp for cp in sorted(regressed) if cp not in frozen and cp not in set(failed)]
        if not pending:
            return

        # Cell widths are per-glyph (regressed advance); cell heights are
        # font-global (the baseline sits at the font ascent for every cell,
        # so ink is never clipped by ink-based per-glyph heights).
        font_asc_px = ascent_px_by_size.get(size_px, size_px * 0.8)
        font_desc_px = descent_px_by_size.get(size_px, size_px * 0.2)
        advances_px = {
            cp: regressed[cp].advance_width_upem * size_px / 1000.0 for cp in pending
        }
        ascents_px = {cp: font_asc_px for cp in pending}
        descents_px = {cp: font_desc_px for cp in pending}
        pages = estimate_cell_plan(
            pending, advances_px, ascents_px, descents_px, size_px,
            self.runtime.atlas_target_mb, self.runtime.atlas_max_mb,
        )
        self.evidence.pages_total += len(pages)
        self.evidence.pages_by_source["raster_fast"] = self.evidence.pages_by_source.get(
            "raster_fast", 0
        ) + len(pages)

        executor = ThreadPoolExecutor(max_workers=max(1, self.runtime.glyph_workers))
        try:
            prefetch = None
            for page_idx, page in enumerate(pages):
                self._check_wall("page_fetch")
                # Streaming: page N+1 downloads while page N decodes.
                if prefetch is None:
                    page_cells = self.raster_provider.fetch_page_cells(
                        list(page.cells), size_px, *FAST_RASTER_PHASE
                    )
                    self.evidence.http_requests += 1
                else:
                    page_cells = prefetch.result()
                if page_idx + 1 < len(pages):
                    next_page = pages[page_idx + 1]
                    prefetch = executor.submit(
                        self._fetch_page_checked, next_page, size_px
                    )
                else:
                    prefetch = None

                frozen_since_checkpoint = 0
                for cell in page.cells:
                    cp = cell.code_point
                    cell_dims[cp] = (cell.w, cell.h)
                    self._check_wall("glyph_geometry")
                    if cp in frozen:
                        continue
                    png = page_cells.get(cp)
                    # Fetched cells arrive cropped (one readback per page;
                    # crop+release immediately after geometry). Cell padding
                    # is the deterministic planner padding.
                    from atlas.paging import CELL_PAD_X_PX, CELL_PAD_Y_PX

                    mapping = CellMapping(
                        size_px=size_px,
                        pad_left_px=CELL_PAD_X_PX,
                        pad_top_px=CELL_PAD_Y_PX,
                        ascent_px=ascent_px,
                    )
                    if png is None:
                        failed.append(cp)
                        continue
                    evidence, contours, _mask = fast_geometry_for_glyph(
                        png, mapping, regressed[cp], cell.w, cell.h
                    )
                    if evidence.status == GlyphStatus.EASY_PASS and (
                        "ZERO_INK" in evidence.reasons and cp != 0x20
                    ):
                        # Space-like zero-ink glyphs other than U+0020 (e.g.
                        # NBSP) carry no fittable geometry: the canonical
                        # model integrity rules require contours for them, so
                        # they are deterministically excluded from the frozen
                        # set (recorded, never counted failed).
                        self.evidence.zero_ink_excluded_ids.append(cp)
                        continue
                    if evidence.status == GlyphStatus.EASY_PASS:
                        fp = observation_fingerprint(
                            {"cp": cp, "size": size_px, "phase": [0.0, 0.0],
                             "checkpoint_identity": self._checkpoint_identity}
                        )
                        glyph = freeze_glyph_model(evidence, contours, regressed[cp], fp)
                        frozen[cp] = glyph
                        self.evidence.easy_glyphs += 1
                        frozen_since_checkpoint += 1
                        if cp not in state.frozen_code_points:
                            state.frozen_code_points.append(cp)
                        self.cache.put_json(
                            NAMESPACE_GLYPH_MODELS,
                            identity_hash({"fp": fp}),
                            glyph.to_canonical_dict(),
                        )
                        if (
                            self.runtime.checkpoint_batch > 0
                            and frozen_since_checkpoint >= self.runtime.checkpoint_batch
                        ):
                            self._save_checkpoint(state)
                            frozen_since_checkpoint = 0
                    else:
                        failed.append(cp)
                        if cp not in state.failed_code_points:
                            state.failed_code_points.append(cp)
                    del evidence, contours

                # Checkpoint after every completed atlas page (ADR-0004).
                state.pages_completed += 1
                self._save_checkpoint(state)
                # Page memory released: page_cells and masks drop here.
                del page_cells
        finally:
            executor.shutdown(wait=False)

    def _fetch_page_checked(self, page: Any, size_px: int) -> dict[int, bytes]:
        cells = self.raster_provider.fetch_page_cells(
            list(page.cells), size_px, *FAST_RASTER_PHASE
        )
        self.evidence.http_requests += 1
        return cells

    def _stage_refinement(
        self,
        regressed: dict[int, RegressedMetrics],
        frozen: dict[int, CalibratedGlyph],
        failed: list[int],
        low_confidence: list[int],
        state: AtlasCheckpoint,
        ascent_px_by_size: dict[int, float],
        descent_px_by_size: dict[int, float],
        cell_dims: dict[int, tuple[int, int]],
    ) -> None:
        """The single refinement for failed glyphs (U5) - nothing more."""
        if not failed:
            return
        size_px = FAST_RASTER_SIZE_PX
        ascent_px = ascent_px_by_size.get(size_px, size_px * 0.8)
        from atlas.paging import CELL_PAD_X_PX, CELL_PAD_Y_PX

        remaining: list[int] = []
        for cp in list(failed):
            self._check_wall("refinement")
            if cp in cell_dims:
                cell_w, cell_h = cell_dims[cp]
            else:
                # Resume path: deterministic planner dimensions (identical
                # inputs -> identical cells): per-glyph advance, font-global
                # ascent/descent.
                from atlas.paging import cell_dimensions as _cd

                cell_w, cell_h = _cd(
                    regressed[cp].advance_width_upem * size_px / 1000.0,
                    ascent_px_by_size.get(size_px, size_px * 0.8),
                    descent_px_by_size.get(size_px, size_px * 0.2),
                    size_px,
                )
            base_png, shifted_png, double_png = self.raster_provider.fetch_refinement(
                cp, cell_w, cell_h
            )
            if base_png is None:
                remaining.append(cp)
                continue
            mapping = CellMapping(
                size_px=size_px,
                pad_left_px=CELL_PAD_X_PX,
                pad_top_px=CELL_PAD_Y_PX,
                ascent_px=ascent_px,
                phase_x_px=0.0,
            )
            evidence, contours = refine_glyph(
                base_png, shifted_png, double_png, mapping, regressed[cp],
                cell_w, cell_h,
            )
            if evidence.status == GlyphStatus.REFINED_PASS:
                fp = observation_fingerprint(
                    {"cp": cp, "refined": True,
                     "checkpoint_identity": self._checkpoint_identity}
                )
                glyph = freeze_glyph_model(evidence, contours, regressed[cp], fp)
                frozen[cp] = glyph
                self.evidence.refined_glyphs += 1
                if evidence.low_confidence:
                    low_confidence.append(cp)
                state.frozen_code_points.append(cp)
                if cp in state.failed_code_points:
                    state.failed_code_points.remove(cp)
            else:
                remaining.append(cp)
        failed[:] = remaining
        self.evidence.failed_glyphs = len(failed)
        state.failed_code_points = list(failed)
        state.low_confidence_code_points = list(low_confidence)
        self._save_checkpoint(state)

    def _stage_typography(
        self, regressed: dict[int, RegressedMetrics]
    ) -> tuple[dict[tuple[int, int], int], dict]:
        """Selective bounded kerning; GPOS kern only for material deltas (U7)."""
        pairs = candidate_kern_pairs(set(regressed.keys()))
        evidence: dict = {"candidate_pairs": len(pairs), "material_pairs": 0}
        if not pairs:
            return {}, evidence
        pair_texts = kerning_batch_texts(pairs)
        size_px = 1024
        pair_advances_px = self.metrics_provider.fetch_pair_advances_px(size_px, pair_texts)
        self.evidence.metrics_js_calls += 1
        deltas: dict[tuple[int, int], float] = {}
        for (l_cp, r_cp), pair_px in zip(pairs, pair_advances_px):
            individual_upem = (
                regressed[l_cp].advance_width_upem + regressed[r_cp].advance_width_upem
            )
            pair_upem = pair_px * 1000.0 / float(size_px)
            deltas[(l_cp, r_cp)] = pair_upem - individual_upem
        kern = select_material_kerning(deltas)
        evidence["material_pairs"] = len(kern)
        return kern, evidence

    # ------------------------------------------------------------------
    # Top-level run
    # ------------------------------------------------------------------

    async def run(self) -> AtlasRunResult:
        t_start = time.perf_counter()
        tracemalloc.start()
        stage_t = {}

        def mark(name: str, t0: float) -> None:
            stage_t[name] = (time.perf_counter() - t0) * 1000.0

        # Resume: durable identity-bound checkpoint (never lease-bound).
        state = self._load_checkpoint() or AtlasCheckpoint(
            checkpoint_identity=self._checkpoint_identity
        )
        frozen: dict[int, CalibratedGlyph] = {}
        low_confidence: list[int] = list(state.low_confidence_code_points)

        # Cached glyph models resume without recomputation: the observation
        # fingerprint is deterministic in (cp, size, phase, checkpoint
        # identity), so the exact-identity cache key is recomputable.
        for cp in list(state.frozen_code_points):
            fp = observation_fingerprint(
                {"cp": cp, "size": FAST_RASTER_SIZE_PX, "phase": [0.0, 0.0],
                 "checkpoint_identity": self._checkpoint_identity}
            )
            cached = self.cache.get_json(
                NAMESPACE_GLYPH_MODELS, identity_hash({"fp": fp})
            )
            if cached is None:
                # Fail closed: frozen-list entry without its cached model is
                # re-derived by the raster pass (drop from the resume list).
                state.frozen_code_points.remove(cp)
                continue
            try:
                frozen[cp] = CalibratedGlyph.from_dict(cached)
                self.evidence.easy_glyphs += 1
            except ValueError:
                state.frozen_code_points.remove(cp)
        failed = [cp for cp in state.failed_code_points if cp not in frozen]

        try:
            # ---- Stage 1: batched metrics (concurrent with HTTP atlas) ----
            t0 = time.perf_counter()
            self._check_wall("metrics")
            regressed, font_asc_upem, font_desc_upem = self._stage_metrics()
            ascent_px_by_size = {
                size: font_asc_upem * size / 1000.0 for size in (512, 1024, 2048)
            }
            descent_px_by_size = {
                size: max(-font_desc_upem, 0.0) * size / 1000.0 for size in (512, 1024, 2048)
            }
            mark("metrics", t0)

            # ---- Stage 2: fast raster pass -------------------------------
            t0 = time.perf_counter()
            self._check_wall("raster_pass")
            cell_dims: dict[int, tuple[int, int]] = {}
            self._stage_raster_pass(
                regressed, frozen, failed, low_confidence, state, ascent_px_by_size,
                descent_px_by_size, cell_dims,
            )
            mark("raster_pass", t0)

            # ---- Stage 3: single refinement ------------------------------
            t0 = time.perf_counter()
            self._check_wall("refinement")
            self._stage_refinement(
                regressed, frozen, failed, low_confidence, state, ascent_px_by_size,
                descent_px_by_size, cell_dims,
            )
            mark("refinement", t0)

            if not frozen:
                raise ValueError("FAST30_FAILED")

            # ---- Stage 4: typography --------------------------------------
            t0 = time.perf_counter()
            self._check_wall("typography")
            kern_pairs, kern_evidence = self._stage_typography(regressed)
            mark("typography", t0)

            # ---- Stage 5: canonical FontModel -----------------------------
            t0 = time.perf_counter()
            model = assemble_font_model(
                family_name=self.spec.family_name,
                style_name=self.spec.style_name,
                reference_id=identity_hash({"source_url": self.spec.source_url}),
                style_id=self.spec.style_id,
                glyphs=frozen,
                font_ascent_upem=font_asc_upem,
                font_descent_upem=font_desc_upem,
                config_hash=self._checkpoint_identity,
                browser_version="atlas_ultra_v1",
                fit_observations_count=len(frozen) + self.evidence.refined_glyphs,
                kerning_pairs=kern_pairs,
            )
            mark("font_model", t0)

            # ---- Stage 5b: Vietnamese extension (mode-gated, U8) ----------
            vi_evidence: dict | None = None
            if self.spec.mode.strip().upper() == "VIETNAMESE":
                t0 = time.perf_counter()
                from atlas.vietnamese import AtlasVietnameseAdapter
                from compute.vietnamese import VietnameseExtensionService

                service = VietnameseExtensionService(
                    ai_provider=None,  # wired at composition edge when enabled
                    config_hash=self._checkpoint_identity,
                    source_hash=identity_hash({"source_url": self.spec.source_url}),
                )
                adapter = AtlasVietnameseAdapter(service)
                model, vi_evidence = await adapter.extend(model)
                mark("vietnamese", t0)

            # ---- Stage 6: temporary TTF + validation ONCE (U9/U10) --------
            t0 = time.perf_counter()
            builder = AtlasFontBuilder(
                self.spec.family_name, self.spec.style_name, self.spec.weight_class
            )
            builder.bind_model(model)
            typography = build_typography_dataset(
                self.spec.family_name, self.spec.style_name, kern_pairs
            )
            build_dir = self.cache.root / "build"
            build_dir.mkdir(parents=True, exist_ok=True)
            temp_ttf = builder.build_temporary_ttf(model, build_dir / "temp", typography)
            mark("build_temp_ttf", t0)

            t0 = time.perf_counter()
            report = run_speed_first_validation(
                ttf_path=temp_ttf.file_path,
                code_points=sorted(frozen.keys()),
                kern_pairs=sorted(kern_pairs.keys()),
                mode=self.spec.mode,
                low_confidence_glyph_ids=low_confidence,
            )
            mark("validation", t0)
            if not report.passed:
                logger.warning("atlas validation failed: %s", report.reasons)
                raise ValueError("FAST30_FAILED")

            # ---- Stage 7: final TTF+OTF from the identical sealed model ---
            t0 = time.perf_counter()
            final = builder.build_final(model, build_dir / "final", typography)
            mark("build_final", t0)
            report = cheap_final_checks(
                final.ttf.file_path, final.otf.file_path, report
            )
            if not report.passed:
                raise ValueError("FAST30_FAILED")

            # Persist final artifacts + report under exact identities.
            model_hash = model.compute_canonical_hash()
            self.cache.put_json(
                NAMESPACE_FONT_MODEL, model_hash, model.to_canonical_dict()
            )
            self.cache.put_bytes_verified(
                NAMESPACE_FONTS, model_hash + "_ttf",
                final.ttf.file_path.read_bytes(), "ttf",
            )
            self.cache.put_bytes_verified(
                NAMESPACE_FONTS, model_hash + "_otf",
                final.otf.file_path.read_bytes(), "otf",
            )
            self.cache.put_json(
                NAMESPACE_REPORTS, model_hash, report.to_dict()
            )

            current, peak = tracemalloc.get_traced_memory()
            self.evidence.peak_tracemalloc_mb = peak / (1024 * 1024)
            self.evidence.stage_timings_ms.update(stage_t)
            self.evidence.total_wall_seconds = time.perf_counter() - t_start
            self.evidence.validation = report.to_dict()
            self.evidence.failed_glyphs = len(failed)
            self.evidence.failed_glyph_ids = sorted(failed)
            self.evidence.low_confidence_glyph_ids = list(low_confidence)

            return AtlasRunResult(
                model=model,
                ttf_path=final.ttf.file_path,
                otf_path=final.otf.file_path,
                report=report.to_dict(),
                evidence=self.evidence,
                frozen_glyphs=dict(frozen),
            )
        finally:
            tracemalloc.stop()

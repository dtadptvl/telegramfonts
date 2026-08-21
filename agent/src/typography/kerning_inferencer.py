"""Evidence-driven kerning inferencer deriving OpenType pair adjustments from observable text metrics."""
from __future__ import annotations

import datetime
import json
import logging
from typing import Any, Iterable

from measurement.browser_session import ChromiumSession
from typography.models import (
    BOUNDED_FIT_PAIRS,
    PairKerningObservation,
    TypographyDataset,
)

logger = logging.getLogger("telegramfonts.agent.typography.inferencer")


class EvidenceKerningInferencer:
    """Infers pairwise character kerning adjustments strictly from observable measurements."""

    def __init__(
        self,
        family_name: str = "BeVietnamPro MAX",
        style_name: str = "Regular",
        units_per_em: int = 1000,
        threshold_upem: float = 0.5,
    ) -> None:
        self.family_name = family_name
        self.style_name = style_name
        self.units_per_em = units_per_em
        self.threshold_upem = threshold_upem

    def infer_from_store(
        self,
        store: Any,
        reference_id: str,
        style_id: str,
    ) -> TypographyDataset:
        """Infer canonical typography dataset strictly from persistent observation store."""
        raw_rows = store.get_pair_observations(reference_id, style_id)
        observations: list[PairKerningObservation] = []
        kerning_pairs: dict[tuple[int, int], int] = {}

        for row in raw_rows:
            left_cp = int(row["left_cp"])
            right_cp = int(row["right_cp"])
            left_char = str(row["left_char"])
            right_char = str(row["right_char"])
            left_adv = float(row["left_advance_upem"])
            right_adv = float(row["right_advance_upem"])
            pair_adv = float(row["pair_advance_upem"])
            inferred_kern = int(row["inferred_kerning_upem"])
            is_applied = inferred_kern != 0

            obs = PairKerningObservation(
                left_cp=left_cp,
                right_cp=right_cp,
                left_char=left_char,
                right_char=right_char,
                left_advance_upem=round(left_adv, 2),
                right_advance_upem=round(right_adv, 2),
                measured_pair_advance_upem=round(pair_adv, 2),
                inferred_kerning_upem=inferred_kern,
                is_kerning_applied=is_applied,
                confidence=float(row.get("confidence", 1.0)),
            )
            observations.append(obs)

            if is_applied:
                kerning_pairs[(left_cp, right_cp)] = inferred_kern

        return TypographyDataset(
            family_name=self.family_name,
            style_name=self.style_name,
            units_per_em=self.units_per_em,
            kerning_pairs=kerning_pairs,
            observations=observations,
            total_pairs_probed=len(raw_rows),
            active_kerning_pairs_count=len(kerning_pairs),
            inference_method="observation_store_cached_measurements",
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

    async def infer_from_browser_session(
        self,
        session: ChromiumSession,
        font_family: str,
        candidate_pairs: Iterable[tuple[int, int]] | None = None,
        font_size_px: float = 200.0,
    ) -> TypographyDataset:
        """Probe bounded pair set in active Chromium session and infer kerning adjustments."""
        pairs = list(candidate_pairs if candidate_pairs is not None else BOUNDED_FIT_PAIRS)
        cps = sorted(set(cp for pair in pairs for cp in pair if cp > 0))
        
        # Batch measure via Canvas 2D in single script round-trip
        chars_dict = {cp: chr(cp) for cp in cps}
        chars_json = json.dumps(list(chars_dict.values()))
        pairs_payload = [[c1, c2, chars_dict[c1], chars_dict[c2]] for c1, c2 in pairs]
        pairs_json = json.dumps(pairs_payload)

        js_code = f"""
        (() => {{
            const canvas = document.createElement("canvas");
            const ctx = canvas.getContext("2d", {{ willReadFrequently: true }});
            ctx.font = '{font_size_px}px "{font_family}"';

            const single_widths = {{}};
            const chars = {chars_json};
            for (const c of chars) {{
                single_widths[c] = (ctx.measureText(c).width / {font_size_px}) * {self.units_per_em};
            }}

            const pairs = {pairs_json};
            const results = [];
            for (const [c1_code, c2_code, c1_str, c2_str] of pairs) {{
                const pair_str = c1_str + c2_str;
                const pair_w = (ctx.measureText(pair_str).width / {font_size_px}) * {self.units_per_em};
                const left_w = single_widths[c1_str];
                const right_w = single_widths[c2_str];
                const raw_delta = pair_w - (left_w + right_w);

                results.push({{
                    left_cp: c1_code,
                    right_cp: c2_code,
                    left_char: c1_str,
                    right_char: c2_str,
                    left_adv: left_w,
                    right_adv: right_w,
                    pair_adv: pair_w,
                    raw_delta: raw_delta
                }});
            }}
            return results;
        }})()
        """
        raw_results = await session.evaluate_script(js_code)
        
        observations: list[PairKerningObservation] = []
        kerning_pairs: dict[tuple[int, int], int] = {}

        for item in raw_results:
            left_cp = int(item["left_cp"])
            right_cp = int(item["right_cp"])
            left_char = str(item["left_char"])
            right_char = str(item["right_char"])
            left_adv = float(item["left_adv"])
            right_adv = float(item["right_adv"])
            pair_adv = float(item["pair_adv"])
            raw_delta = float(item["raw_delta"])

            inferred_kern = int(round(raw_delta))
            is_applied = abs(raw_delta) >= self.threshold_upem and inferred_kern != 0

            obs = PairKerningObservation(
                left_cp=left_cp,
                right_cp=right_cp,
                left_char=left_char,
                right_char=right_char,
                left_advance_upem=round(left_adv, 2),
                right_advance_upem=round(right_adv, 2),
                measured_pair_advance_upem=round(pair_adv, 2),
                inferred_kerning_upem=inferred_kern if is_applied else 0,
                is_kerning_applied=is_applied,
                confidence=1.0,
            )
            observations.append(obs)

            if is_applied:
                kerning_pairs[(left_cp, right_cp)] = inferred_kern

        dataset = TypographyDataset(
            family_name=self.family_name,
            style_name=self.style_name,
            units_per_em=self.units_per_em,
            kerning_pairs=kerning_pairs,
            observations=observations,
            total_pairs_probed=len(pairs),
            active_kerning_pairs_count=len(kerning_pairs),
            inference_method="browser_text_metrics_differential",
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

        logger.info(
            "Inferred %d active kerning pairs across %d probed pairs for %s %s",
            len(kerning_pairs),
            len(pairs),
            self.family_name,
            self.style_name,
        )
        return dataset

    def infer_from_direct_measurements(
        self,
        measurements: list[tuple[int, int, float, float, float]],
    ) -> TypographyDataset:
        """Infer kerning from a list of (left_cp, right_cp, left_adv, right_adv, pair_adv) measurements."""
        observations: list[PairKerningObservation] = []
        kerning_pairs: dict[tuple[int, int], int] = {}

        for left_cp, right_cp, left_adv, right_adv, pair_adv in measurements:
            left_char = chr(left_cp) if left_cp > 0 else "?"
            right_char = chr(right_cp) if right_cp > 0 else "?"
            raw_delta = pair_adv - (left_adv + right_adv)
            inferred_kern = int(round(raw_delta))
            is_applied = abs(raw_delta) >= self.threshold_upem and inferred_kern != 0

            obs = PairKerningObservation(
                left_cp=left_cp,
                right_cp=right_cp,
                left_char=left_char,
                right_char=right_char,
                left_advance_upem=round(left_adv, 2),
                right_advance_upem=round(right_adv, 2),
                measured_pair_advance_upem=round(pair_adv, 2),
                inferred_kerning_upem=inferred_kern if is_applied else 0,
                is_kerning_applied=is_applied,
                confidence=1.0,
            )
            observations.append(obs)

            if is_applied:
                kerning_pairs[(left_cp, right_cp)] = inferred_kern

        return TypographyDataset(
            family_name=self.family_name,
            style_name=self.style_name,
            units_per_em=self.units_per_em,
            kerning_pairs=kerning_pairs,
            observations=observations,
            total_pairs_probed=len(measurements),
            active_kerning_pairs_count=len(kerning_pairs),
            inference_method="direct_measurement_differential",
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

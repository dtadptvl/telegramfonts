"""Evidence-driven kerning inferencer deriving OpenType pair adjustments from observable text metrics."""
from __future__ import annotations

import datetime
import hashlib
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


def _validate_exact_identity(reference_id: str, style_id: str, browser_version: str, config_hash: str) -> None:
    """Validate that identity components are non-empty and config_hash is valid 64-character hex."""
    if not isinstance(reference_id, str) or not reference_id.strip():
        raise ValueError("INFERENCER_ERROR: Missing or empty reference_id")
    if not isinstance(style_id, str) or not style_id.strip():
        raise ValueError("INFERENCER_ERROR: Missing or empty style_id")
    if not isinstance(browser_version, str) or not browser_version.strip():
        raise ValueError("INFERENCER_ERROR: Missing or empty browser_version")
    if (
        not isinstance(config_hash, str)
        or len(config_hash) != 64
        or not all(c in "0123456789abcdefABCDEF" for c in config_hash)
    ):
        raise ValueError(f"INFERENCER_ERROR: Missing or invalid 64-character hex config_hash: '{config_hash}'")


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
        browser_version: str,
        config_hash: str,
        require_provenance: bool = True,
    ) -> TypographyDataset:
        """Infer canonical typography dataset strictly from persistent observation store by recomputing adjustments from raw measurements."""
        _validate_exact_identity(reference_id, style_id, browser_version, config_hash)

        raw_rows = store.get_pair_observations(
            reference_id=reference_id,
            style_id=style_id,
            browser_version=browser_version,
            config_hash=config_hash,
        )
        observations: list[PairKerningObservation] = []
        kerning_pairs: dict[tuple[int, int], int] = {}
        provenances: set[str] = set()
        valid_pairs_seen: set[tuple[int, int]] = set()

        for row in raw_rows:
            left_cp = int(row["left_cp"])
            right_cp = int(row["right_cp"])
            left_char = str(row["left_char"])
            right_char = str(row["right_char"])
            left_adv = float(row["left_advance_upem"])
            right_adv = float(row["right_advance_upem"])
            pair_adv = float(row["pair_advance_upem"])
            prov = str(row.get("provenance", "untrusted"))

            if require_provenance:
                # Fail-closed: verify explicit Chromium Canvas acquisition provenance
                if not (prov.startswith("chromium:") and prov.endswith(":canvas_text_metrics")):
                    raise ValueError(
                        f"Fail-closed: row pair ({left_cp}, {right_cp}) has untrusted or missing Chromium provenance: '{prov}'. "
                        "Only authentic Chromium Canvas acquisition provenance ('chromium:<version>:canvas_text_metrics') is accepted."
                    )

            provenances.add(prov)
            valid_pairs_seen.add((left_cp, right_cp))

            # Recompute differential adjustment dynamically from raw observable advances (never trust stored answer!)
            raw_delta = pair_adv - (left_adv + right_adv)
            inferred_kern = int(round(raw_delta))
            is_applied = abs(raw_delta) >= self.threshold_upem and inferred_kern != 0

            row_ref = str(row.get("reference_id") or "")
            row_style = str(row.get("style_id") or "")
            row_browser = str(row.get("browser_version") or "")
            row_cfg = str(row.get("config_hash") or "")

            if row_ref != reference_id or row_style != style_id or row_browser != browser_version or row_cfg != config_hash:
                raise ValueError(
                    f"INFERENCER_ERROR: Pair row ({left_cp}, {right_cp}) identity mismatch: "
                    f"expected ({reference_id}, {style_id}, {browser_version}, {config_hash}) but got "
                    f"({row_ref}, {row_style}, {row_browser}, {row_cfg})"
                )

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
                reference_id=reference_id,
                style_id=style_id,
                browser_version=browser_version,
                config_hash=config_hash,
                confidence=float(row.get("confidence", 1.0)),
                provenance=prov,
            )
            observations.append(obs)

            if is_applied:
                kerning_pairs[(left_cp, right_cp)] = inferred_kern

        if require_provenance:
            cov = store.get_coverage(reference_id, style_id, browser_version=browser_version, config_hash=config_hash)
            cov_set = set(cov) if cov else set()
            if cov_set:
                expected_fit_pairs = {p for p in BOUNDED_FIT_PAIRS if p[0] in cov_set and p[1] in cov_set}
            else:
                expected_fit_pairs = set(BOUNDED_FIT_PAIRS)
            missing = expected_fit_pairs - valid_pairs_seen
            if missing:
                raise ValueError(
                    f"Fail-closed: observation store missing {len(missing)} bounded fit pairs: {missing}. "
                    f"Expected all {len(expected_fit_pairs)} bounded fit pairs with authentic Chromium acquisition provenance."
                )

        # Compute deterministic SHA256 digest over canonical sorted observations
        canonical_rows_repr = json.dumps(
            [
                {
                    "l": obs.left_cp,
                    "r": obs.right_cp,
                    "la": obs.left_advance_upem,
                    "ra": obs.right_advance_upem,
                    "pa": obs.measured_pair_advance_upem,
                    "prov": obs.provenance,
                }
                for obs in sorted(observations, key=lambda x: (x.left_cp, x.right_cp))
            ],
            sort_keys=True,
        )
        fit_rows_sha256 = hashlib.sha256(canonical_rows_repr.encode("utf-8")).hexdigest()
        common_provenance = list(provenances)[0] if len(provenances) == 1 else ",".join(sorted(provenances))

        return TypographyDataset(
            family_name=self.family_name,
            style_name=self.style_name,
            units_per_em=self.units_per_em,
            kerning_pairs=kerning_pairs,
            observations=observations,
            total_pairs_probed=len(raw_rows),
            active_kerning_pairs_count=len(kerning_pairs),
            provenance=common_provenance,
            fit_rows_count=len(observations),
            fit_rows_sha256=fit_rows_sha256,
            inference_method="observation_store_differential_derivation",
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

    async def infer_from_browser_session(
        self,
        session: ChromiumSession,
        font_family: str,
        reference_id: str,
        style_id: str,
        browser_version: str,
        config_hash: str,
        candidate_pairs: Iterable[tuple[int, int]] | None = None,
        font_size_px: float = 200.0,
    ) -> TypographyDataset:
        """Probe bounded pair set in active Chromium session and infer kerning adjustments."""
        _validate_exact_identity(reference_id, style_id, browser_version, config_hash)
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
                reference_id=reference_id,
                style_id=style_id,
                browser_version=browser_version,
                config_hash=config_hash,
                confidence=1.0,
                provenance=f"chromium:{browser_version}:canvas_text_metrics",
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
        reference_id: str,
        style_id: str,
        browser_version: str,
        config_hash: str,
        provenance: str = "direct_measurement",
    ) -> TypographyDataset:
        """Infer kerning from a list of (left_cp, right_cp, left_adv, right_adv, pair_adv) measurements."""
        _validate_exact_identity(reference_id, style_id, browser_version, config_hash)
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
                reference_id=reference_id,
                style_id=style_id,
                browser_version=browser_version,
                config_hash=config_hash,
                confidence=1.0,
                provenance=provenance,
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

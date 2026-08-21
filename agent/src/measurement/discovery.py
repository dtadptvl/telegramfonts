"""Dynamic observable glyph discovery terminating on convergence or source exhaustion."""
from __future__ import annotations

import logging
from typing import AsyncIterable, Callable

logger = logging.getLogger("telegramfonts.agent.measurement.discovery")

# Standard Unicode ranges for comprehensive Latin + Vietnamese diacritics coverage
UNICODE_BLOCK_RANGES: list[tuple[int, int]] = [
    (0x0020, 0x007E),  # ASCII Basic Latin
    (0x00A0, 0x00FF),  # Latin-1 Supplement
    (0x0100, 0x017F),  # Latin Extended-A (Đ, đ, etc.)
    (0x0180, 0x024F),  # Latin Extended-B (Ơ, ơ, Ư, ư, etc.)
    (0x1EA0, 0x1EF9),  # Latin Extended Additional (Vietnamese precomposed diacritics)
    (0x2000, 0x206F),  # General Punctuation
    (0x20A0, 0x20CF),  # Currency Symbols (₫, €, £, ¥, etc.)
]


class ObservableGlyphDiscovery:
    """Dynamic discovery of observable font glyphs without hardcoded page/glyph count caps."""

    @staticmethod
    def get_candidate_code_points() -> list[int]:
        """Get standard sorted candidate Unicode code points covering Latin & Vietnamese."""
        seen = set()
        candidates = []
        for start, end in UNICODE_BLOCK_RANGES:
            for cp in range(start, end + 1):
                if cp not in seen and chr(cp).isprintable():
                    seen.add(cp)
                    candidates.append(cp)
        return sorted(candidates)

    @classmethod
    async def discover_observable_glyphs(
        cls,
        measure_fn: Callable[[int], bool | float],
        candidate_code_points: list[int] | None = None,
        max_consecutive_misses: int = 500,
    ) -> list[int]:
        """Dynamically discover supported observable glyphs from the browser or font source.
        
        Terminates upon source exhaustion or convergence (when consecutive candidates yield no new observable glyphs).
        """
        candidates = candidate_code_points or cls.get_candidate_code_points()
        discovered: list[int] = []
        consecutive_misses = 0

        for cp in candidates:
            try:
                # measure_fn returns True/adv_width if glyph is present/renderable
                result = measure_fn(cp)
                if isinstance(result, bool):
                    is_observable = result
                else:
                    is_observable = float(result) > 0.0

                if is_observable:
                    discovered.append(cp)
                    consecutive_misses = 0
                else:
                    consecutive_misses += 1
            except Exception as exc:
                consecutive_misses += 1
                logger.debug(f"Candidate U+{cp:04X} measurement failed: {exc}")

            if consecutive_misses >= max_consecutive_misses:
                logger.info(f"Glyph discovery converged after {consecutive_misses} consecutive misses")
                break

        canonical_coverage = sorted(set(discovered))
        logger.info(f"Discovered {len(canonical_coverage)} canonical observable glyphs")
        return canonical_coverage

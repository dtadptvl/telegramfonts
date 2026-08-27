import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("telegramfonts.agent.measurement.discovery")


class DiscoveryBudgetExhaustedError(RuntimeError):
    """Safety-budget exhaustion stopped discovery (never successful completion)."""

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
    """Dynamic discovery of observable font glyphs without hardcoded page/glyph count caps.

    Termination semantics (FULL MAX profile): discovery completes ONLY on
    an authoritative grounded signal — exhaustive enumeration of the full
    candidate source finished (``EXHAUSTED``) with an empty result reported
    as ``EMPTY``. ``REPEATED`` remains reserved for authoritative
    source/page/cursor repetition signals (e.g. provider crawl loops) and
    is never inferred from caller-owned candidate lists. Every safety,
    miss-window, or probe limit stops execution as ``BUDGET_EXHAUSTED``
    (BLOCKED) and must never be counted as successful completion.
    """

    TERMINAL_COMPLETE = frozenset({"EXHAUSTED", "EMPTY", "NO_NEW", "REPEATED"})
    TERMINAL_BLOCKED = frozenset({"BUDGET_EXHAUSTED"})

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
        measure_fn: Callable[[int], Any],
        candidate_code_points: list[int] | None = None,
        max_consecutive_misses: int = 500,
    ) -> list[int]:
        """Dynamically discover supported observable glyphs from the browser or font source.

        Terminates upon source exhaustion or convergence (when consecutive candidates yield no new observable glyphs).
        """
        coverage, _reason = await cls.discover_with_termination(
            measure_fn,
            candidate_code_points=candidate_code_points,
            max_consecutive_misses=max_consecutive_misses,
        )
        return coverage

    @classmethod
    async def discover_with_termination(
        cls,
        measure_fn: Callable[[int], Any],
        candidate_code_points: list[int] | None = None,
        max_consecutive_misses: int = 500,
        max_candidates: int = 50_000,
    ) -> tuple[list[int], str]:
        """Discover observable glyphs and report the exact termination reason.

        Grounded completion: EXHAUSTED (the authoritative candidate source
        was enumerated in full) or EMPTY (full enumeration observed nothing).
        Any miss-window or probe-budget limit stopping execution early is
        BUDGET_EXHAUSTED — a BLOCKED outcome, never successful completion;
        supported glyphs beyond a gap can never be silently completed away.
        """
        candidates = candidate_code_points or cls.get_candidate_code_points()
        discovered: list[int] = []
        seen: set[int] = set()
        consecutive_misses = 0
        probed = 0
        reason = "EXHAUSTED"

        for cp in candidates:
            if probed >= max_candidates:
                # Safety probe budget stopped execution before exhaustive
                # enumeration: BLOCKED, never completion.
                reason = "BUDGET_EXHAUSTED"
                logger.info(f"Glyph discovery stopped by probe budget after {probed} probes")
                break
            probed += 1
            try:
                res = measure_fn(cp)
                if asyncio.iscoroutine(res):
                    result = await res
                else:
                    result = res

                if isinstance(result, bool):
                    is_observable = result
                else:
                    is_observable = float(result) > 0.0

                if is_observable:
                    if cp not in seen:
                        discovered.append(cp)
                        seen.add(cp)
                    consecutive_misses = 0
                else:
                    consecutive_misses += 1
            except Exception as exc:
                consecutive_misses += 1
                logger.debug(f"Candidate U+{cp:04X} measurement failed: {exc}")

            if consecutive_misses >= max_consecutive_misses:
                # Miss-window safety limit: partial evidence only. This is a
                # BLOCKED outcome — never an observable completion signal.
                reason = "BUDGET_EXHAUSTED"
                logger.info(
                    f"Glyph discovery stopped by miss budget after {consecutive_misses} consecutive misses"
                )
                break
        else:
            reason = "EXHAUSTED"

        canonical_coverage = sorted(set(discovered))
        if reason == "EXHAUSTED" and not canonical_coverage:
            reason = "EMPTY"
        logger.info(
            f"Discovered {len(canonical_coverage)} canonical observable glyphs (termination={reason})"
        )
        return canonical_coverage, reason

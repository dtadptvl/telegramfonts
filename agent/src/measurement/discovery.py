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

    Termination semantics (FULL MAX profile): discovery completes only on an
    observable termination signal — candidate source exhaustion
    (``EXHAUSTED``), empty result (``EMPTY``), or convergence with no new
    observable glyphs (``NO_NEW``/``REPEATED``). A safety budget stopping
    execution early is ``BUDGET_EXHAUSTED`` and must never be counted as
    successful completion by callers.
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

        Returns ``(coverage, reason)``. Completion reasons (observable
        signals): EXHAUSTED (candidate source fully scanned), EMPTY (nothing
        observable), NO_NEW (deterministic convergence: the consecutive-miss
        window closed with no new glyphs), REPEATED (observable repeated
        signal). BUDGET_EXHAUSTED means the safety probe budget stopped
        execution before any observable termination signal — it is a BLOCKED
        outcome and must never be counted as successful completion.
        """
        candidates = candidate_code_points or cls.get_candidate_code_points()
        discovered: list[int] = []
        seen: set[int] = set()
        consecutive_misses = 0
        probed = 0
        reason = "EXHAUSTED"

        for cp in candidates:
            if probed >= max_candidates:
                # Safety probe budget stopped execution before any observable
                # termination signal: BLOCKED, never completion.
                reason = "BUDGET_EXHAUSTED"
                logger.info(f"Glyph discovery stopped by safety budget after {probed} probes")
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
                    if cp in seen:
                        # Observable repeated signal: deterministic end.
                        reason = "REPEATED"
                        break
                    discovered.append(cp)
                    seen.add(cp)
                    consecutive_misses = 0
                else:
                    consecutive_misses += 1
            except Exception as exc:
                consecutive_misses += 1
                logger.debug(f"Candidate U+{cp:04X} measurement failed: {exc}")

            if consecutive_misses >= max_consecutive_misses:
                # Observable no-new convergence window closed: deterministic
                # completion (never budget semantics).
                reason = "NO_NEW"
                logger.info(f"Glyph discovery converged: {consecutive_misses} consecutive misses with no new glyphs")
                break
        else:
            reason = "EXHAUSTED"

        canonical_coverage = sorted(set(discovered))
        if reason != "BUDGET_EXHAUSTED" and not canonical_coverage:
            reason = "EMPTY"
        logger.info(
            f"Discovered {len(canonical_coverage)} canonical observable glyphs (termination={reason})"
        )
        return canonical_coverage, reason

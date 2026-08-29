"""Issue #88 correction (FULLMAX_LANE_COLD_START): browser-marked Chromium
readiness/warmup set for the fullmax-final lane.

Causal evidence: at 0ff80712 the fullmax-final lane selector
`-m "fullmax_e2e or performance"` left the ORIGINAL E2E chain as the first
in-session Chromium launch. That cold launch failed the consumer gate
(CONSUMER_GATE_FAIL CHROMIUM_GLYPH_COUNT_MISMATCH /
CHROMIUM_PAIR_COUNT_MISMATCH / CHROMIUM_ENVIRONMENT_FAILED) while the later
VIETNAMESE chain passed in the same session once the browser environment was
live (fullmax-final run 33221130391).

This module moves the first in-session Chromium launch in front of the
canonical E2E tier: it executes the production readiness lifecycle
(measurement.chromium_readiness.run_readiness) with a bounded attempt budget,
so the fullmax lane is Chromium-ready before the E2E tier runs. Consumer
gates, thresholds, schedules, and evidence semantics are unchanged; the
canonical E2E chains still produce their own gated Chromium evidence.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

from measurement.browser_session import find_chromium_executable
from measurement.chromium_readiness import run_readiness

# Bounded readiness budget. The demonstrated failure mode is a first-launch
# cold start; later in-session launches succeed. Three attempts absorb that
# cold start without weakening anything: if the environment cannot serve a
# production-deadline readiness lifecycle within the budget, the lane fails
# closed here with a typed readiness verdict instead of failing later inside
# the canonical consumer gate.
MAX_WARMUP_ATTEMPTS = 3


@pytest.mark.asyncio
async def test_fullmax_lane_chromium_readiness_warmup():
    """The fullmax lane's Chromium environment must be ready before the E2E
    tier: one production readiness lifecycle (launch -> loopback CDP endpoint
    validation -> browser version identity -> inert evaluation -> clean close)
    must succeed within the bounded attempt budget."""
    try:
        executable = find_chromium_executable()
    except Exception:
        pytest.skip("Chromium executable not available on host")

    evidence: list[str] = []
    for attempt in range(1, MAX_WARMUP_ATTEMPTS + 1):
        report, exit_code = await run_readiness(executable)
        ready = bool(report["ready"])
        residue_clear = bool(report["owned_residue_clear"])
        if exit_code == 0 and ready and residue_clear:
            return
        evidence.append(
            f"attempt={attempt} exit_code={exit_code} ready={ready} "
            f"stage={report['stage']} residue_clear={residue_clear}"
        )

    pytest.fail("CHROMIUM_WARMUP_READINESS_FAILED: " + "; ".join(evidence))

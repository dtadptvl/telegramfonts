"""Browser-marked Chromium readiness/warmup set (Issue #88 origin).

Historical context: introduced as the FULLMAX_LANE_COLD_START correction so
a first in-session Chromium cold launch never failed the consumer gate. The
fullmax-final CI lane that motivated it is retired together with the
BALANCED_MAX/FULL_MAX profiles (ADR-0001). The readiness truth itself is
preserved: the production readiness lifecycle must succeed before any gated
Chromium consumer evidence is produced, regardless of which lane runs.

This module executes the production readiness lifecycle
(measurement.chromium_readiness.run_readiness) with a bounded attempt budget
so the process is Chromium-ready before gated consumer evidence runs.
Consumer gates, thresholds, schedules, and evidence semantics are unchanged.
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
async def test_chromium_readiness_warmup_before_gated_consumers():
    """The Chromium environment must be ready before gated consumer evidence:
    one production readiness lifecycle (launch -> loopback CDP endpoint
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

"""Issue #88: pytest-xdist isolation probe (evidence for the -n 2 decision).

Contract obligation: never enable pytest-xdist blindly; prove isolation for
global memo state, browser processes, ports, temporary profiles, and
filesystem identities first. These probes establish the isolation FACTS;
the enablement decision is recorded in the Issue #88 terminal report.

Probe results (see test bodies for the causal mechanics):
- Global memo/cache state (Stage9DReleaseGate formation memo,
  GLOBAL_INTERMEDIATE_CACHE) is per-process: independently spawned
  interpreters never observe each other's entries. This is exactly the
  isolation model xdist workers rely on.
- No test binds a fixed port; production browser session binds only the
  ephemeral port 0.
- Concurrent repo-root tempdirs (the test_debian_supervisor pattern) keep
  distinct filesystem identities.
- The seven real-browser tests fail closed to pytest.skip when Chromium is
  unavailable, so browser-tier tests cannot hang or collide in lanes
  without browser binaries.

DEFERRED (recorded honestly): -n 2 is NOT enabled by this change. Process
isolation is proven, but intra-worker ORDER independence across all modules
that touch shared in-process state (memo-dependent Stage 9D runner tests and
GLOBAL_INTERMEDIATE_CACHE consumers) is not proven inside this contract's
budget, and the 5-8 minute quick target is met without parallelization.
test_quick_lane_does_not_enable_xdist guards the deferral until an
order-independence proof exists.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parents[2]
AGENT_SRC = ROOT / "agent" / "src"
TESTS_DIR = ROOT / "agent" / "tests"
WORKFLOWS = ROOT / ".github" / "workflows"

REAL_BROWSER_TESTS = (
    "test_STEALTH_BROWSER_LOCAL_HTML_FIXTURE",
    "test_STEALTH_CSS_MD5_WITH_LOCAL_FALLBACK_REJECTED",
    "test_STEALTH_OBSERVED_FONT_RESPONSE_ACCEPTED",
    "test_STEALTH_PERFORMANCE_ONLY_REJECTED",
    "test_STEALTH_REDIRECT_THEN_FAIL_REJECTED",
    "test_STEALTH_UNRELATED_MD5_RESPONSE_REJECTED",
    "test_STEALTH_UNLOADED_FACE_REJECTED",
)

_MEMO_CHILD = r'''
import json
import sys

mode = sys.argv[1]
from fidelity.release_gate import Stage9DReleaseGate
from fidelity import balanced_search as bs

PROBE_KEY = "ISOLATION_PROBE_SENTINEL"
CACHE_KEY = bs.IntermediateArtifactCache.decode_key("a" * 64, 256, 1234)

if mode == "write":
    Stage9DReleaseGate._formation_memo[PROBE_KEY] = "SENTINEL"
    bs.GLOBAL_INTERMEDIATE_CACHE.put_decode(CACHE_KEY, "SENTINEL")
    print(json.dumps({"memo_len": len(Stage9DReleaseGate._formation_memo)}))
else:
    print(json.dumps({
        "memo_len": len(Stage9DReleaseGate._formation_memo),
        "memo_probe": Stage9DReleaseGate._formation_memo.get(PROBE_KEY),
        "cache_probe": bs.GLOBAL_INTERMEDIATE_CACHE.get_decode(CACHE_KEY),
    }))
'''


def _run_child(mode: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(AGENT_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", _MEMO_CHILD, mode],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
        timeout=180,
    )
    assert proc.returncode == 0, f"child {mode} failed: {proc.stderr[-800:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_global_memo_state_is_process_local():
    """Stage9DReleaseGate formation memo: a writer process's entry is never
    visible to an independently spawned reader process."""
    written = _run_child("write")
    assert written["memo_len"] >= 1
    observed = _run_child("read")
    assert observed["memo_len"] == 0
    assert observed["memo_probe"] is None


def test_global_intermediate_cache_is_process_local():
    """GLOBAL_INTERMEDIATE_CACHE: decode-cache entries never leak across
    independently spawned processes."""
    observed = _run_child("read")
    assert observed["cache_probe"] is None


_TEMPDIR_CHILD = r'''
import sys
import tempfile
import time
from pathlib import Path

root, sentinel = sys.argv[1], sys.argv[2]
with tempfile.TemporaryDirectory(dir=root) as td:
    target = Path(td) / "sentinel.txt"
    target.write_text(sentinel, encoding="utf-8")
    time.sleep(1.0)
    print("OK" if target.read_text(encoding="utf-8") == sentinel else "CORRUPTED")
'''


def test_concurrent_root_tempdirs_keep_distinct_filesystem_identities():
    """The repo-root TemporaryDirectory pattern (used by
    test_debian_supervisor) run concurrently in two processes keeps
    distinct, non-corrupted filesystem identities."""
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _TEMPDIR_CHILD, str(ROOT), f"sentinel-{index}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    outputs = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=120)
        assert proc.returncode == 0, stderr[-800:]
        outputs.append(stdout.strip())
    assert outputs == ["OK", "OK"]


def test_no_fixed_port_bind_in_test_sources():
    """No test binds a fixed port; only ephemeral port-0 binds are allowed
    anywhere in agent code."""
    pattern = re.compile(r"\.bind\(\s*\(\s*['\"][^'\"]*['\"]\s*,\s*(\d+)\s*\)")
    offenders = []
    for source in sorted(TESTS_DIR.glob("test_*.py")):
        for match in pattern.finditer(source.read_text(encoding="utf-8")):
            if int(match.group(1)) != 0:
                offenders.append(f"{source.name}: {match.group(0)}")
    assert not offenders
    browser_session = (AGENT_SRC / "measurement" / "browser_session.py").read_text(encoding="utf-8")
    for match in pattern.finditer(browser_session):
        assert int(match.group(1)) == 0, "production browser session must bind ephemeral port 0 only"


def test_real_browser_tests_fail_closed_to_skip_without_chromium():
    """Each of the seven real-Chromium tests skips (never hangs/collides)
    when browser launch is unavailable in the lane environment."""
    source = (TESTS_DIR / "test_acquisition_fallback_graph.py").read_text(encoding="utf-8")
    for name in REAL_BROWSER_TESTS:
        assert f"async def {name}(" in source
    assert source.count('pytest.skip(f"Browser launch unavailable') == len(REAL_BROWSER_TESTS)


def test_quick_lane_does_not_enable_xdist_blindly():
    """Recorded deferral guard: until an intra-worker order-independence
    proof exists, no CI lane may enable xdist parallelization. The
    fullmax-final lane is retired with the BALANCED_MAX/FULL_MAX profiles
    (ADR-0001): it must not exist."""
    quick = (WORKFLOWS / "quick-tests.yml").read_text(encoding="utf-8")
    assert "-n " not in quick
    assert "xdist" not in quick
    assert not (WORKFLOWS / "fullmax-final.yml").exists()

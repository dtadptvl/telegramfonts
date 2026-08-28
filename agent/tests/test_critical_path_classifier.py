"""Issue #88 causal tests for the deterministic critical-path classifier.

Proves the three contract obligations:
1. Critical-path changes TRIGGER the FULL-MAX lane (trigger paths).
2. Docs/governance/agent-profile-only changes do NOT trigger (non-trigger
   paths).
3. Silent omission is impossible: unknown paths, empty diffs, internal
   errors, and broken wiring all fail CLOSED toward triggering, and the CI
   lane wiring binds the quick lane to classifier success and the
   fullmax-final lane to the classifier output.
"""
import importlib.util
import random
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
CLASSIFIER = ROOT / "scripts" / "critical_path_classifier.py"
WORKFLOWS = ROOT / ".github" / "workflows"


def _load_classifier():
    spec = importlib.util.spec_from_file_location("critical_path_classifier", CLASSIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def classifier():
    assert CLASSIFIER.is_file(), "classifier script missing from scripts/"
    return _load_classifier()


# ---------------------------------------------------------------------------
# 1. TRIGGER paths: every critical production surface fires FULL-MAX.
# ---------------------------------------------------------------------------
CRITICAL_SAMPLES = [
    # reconstruction / font model / font builder
    "agent/src/reconstruction/solver.py",
    "agent/src/reconstruction/font_model.py",
    "agent/src/reconstruction/candidate_builder.py",
    # optimizer / fidelity / evaluator / release gate
    "agent/src/fidelity/optimizer.py",
    "agent/src/fidelity/evaluator.py",
    "agent/src/fidelity/release_gate.py",
    "agent/src/fidelity/pipeline.py",
    "agent/src/fidelity/balanced_search.py",
    # measurement schedule / collector / calibration
    "agent/src/measurement/max_profile.py",
    "agent/src/measurement/collector.py",
    "agent/src/measurement/calibration.py",
    # font builder surfaces
    "agent/src/compute/font_builder.py",
    "agent/src/typography/gpos_builder.py",
    # CI/test gating layer (this PR is itself critical-path for gating)
    ".github/workflows/quick-tests.yml",
    ".github/workflows/fullmax-final.yml",
    "agent/tests/test_runner.py",
    "scripts/critical_path_classifier.py",
    "agent/pyproject.toml",
]


@pytest.mark.parametrize("path", CRITICAL_SAMPLES)
def test_critical_path_triggers_fullmax(classifier, path):
    decision = classifier.classify([path])
    assert decision["fullmax"] is True, f"{path} must trigger FULL-MAX"


def test_this_pr_surface_triggers_fullmax(classifier):
    """This PR changes CI/test layer -> fullmax-final must fire."""
    changed = [
        ".github/workflows/quick-tests.yml",
        ".github/workflows/fullmax-final.yml",
        "agent/tests/test_runner.py",
        "agent/pyproject.toml",
        "scripts/critical_path_classifier.py",
        "docs/A23_OPERATOR_GUIDE.md",
    ]
    decision = classifier.classify(changed)
    assert decision["fullmax"] is True


# ---------------------------------------------------------------------------
# 2. NON-TRIGGER paths: docs / governance / agent-profile-only never fires.
# ---------------------------------------------------------------------------
NON_CRITICAL_SAMPLES = [
    ["docs/A23_OPERATOR_GUIDE.md"],
    [".ai/ARCHITECT-REF.md"],
    [".ai/EXECUTOR-REF.md"],
    ["ARCHITECT.md"],
    ["EXECUTOR.md"],
    ["README.md"],
    [
        "docs/A23_OPERATOR_GUIDE.md",
        ".ai/EXECUTOR-REF.md",
        ".ai/ARCHITECT-REF.md",
        "ARCHITECT.md",
        "EXECUTOR.md",
        "README.md",
    ],
]


@pytest.mark.parametrize("paths", NON_CRITICAL_SAMPLES)
def test_docs_governance_agent_profile_only_does_not_trigger(classifier, paths):
    decision = classifier.classify(paths)
    assert decision["fullmax"] is False, f"{paths} must NOT trigger FULL-MAX"
    assert any("non-trigger" in reason for reason in decision["reasons"])


# ---------------------------------------------------------------------------
# 3. Silent omission is impossible (fail-closed envelopes).
# ---------------------------------------------------------------------------
def test_unknown_path_fails_closed_to_trigger(classifier):
    decision = classifier.classify(["totally/unmapped/surface.xyz"])
    assert decision["fullmax"] is True
    assert any("unclassified-path-fail-closed" in r for r in decision["reasons"])


def test_empty_change_list_fails_closed_to_trigger(classifier):
    assert classifier.classify([])["fullmax"] is True
    assert classifier.classify(["", "   "])["fullmax"] is True


def test_mixed_docs_plus_critical_triggers(classifier):
    decision = classifier.classify(
        ["docs/A23_OPERATOR_GUIDE.md", "agent/src/fidelity/optimizer.py"]
    )
    assert decision["fullmax"] is True


def test_internal_error_fails_closed_to_trigger(classifier, monkeypatch):
    def broken(_path):
        raise RuntimeError("simulated classifier corruption")

    monkeypatch.setattr(classifier, "_non_critical", broken)
    decision = classifier.classify(["docs/A23_OPERATOR_GUIDE.md"])
    assert decision["fullmax"] is True
    assert any("fail-closed" in reason.lower() for reason in decision["reasons"])


def test_uniterable_input_fails_closed_to_trigger(classifier):
    class Uniterable:
        def __iter__(self):
            raise RuntimeError("simulated iteration failure")

    decision = classifier.classify(Uniterable())
    assert decision["fullmax"] is True


def test_label_override_forces_trigger_on_docs_only(classifier):
    decision = classifier.classify(["docs/A23_OPERATOR_GUIDE.md"], force_label=True)
    assert decision["fullmax"] is True
    assert any(classifier.OVERRIDE_LABEL in r for r in decision["reasons"])


def test_dispatch_override_forces_trigger_with_empty_diff(classifier):
    decision = classifier.classify([], force_dispatch=True)
    assert decision["fullmax"] is True
    assert any("workflow_dispatch" in r for r in decision["reasons"])


def test_decision_is_deterministic_and_order_independent(classifier):
    paths = [
        "agent/src/reconstruction/solver.py",
        "docs/A23_OPERATOR_GUIDE.md",
        ".github/workflows/quick-tests.yml",
        "README.md",
    ]
    base = classifier.classify(paths)
    for _ in range(5):
        shuffled = paths[:]
        random.Random(_).shuffle(shuffled)
        assert classifier.classify(shuffled) == base
    assert classifier.classify(paths) == base


def test_normalize_handles_backslash_and_dot_prefix(classifier):
    assert classifier.classify([r"agent\src\fidelity\optimizer.py"])["fullmax"] is True
    assert classifier.classify(["./docs/x.md"])["fullmax"] is False
    assert classifier.classify(["./.ai/EXECUTOR-REF.md"])["fullmax"] is False


# ---------------------------------------------------------------------------
# 4. CI wiring causality: lanes are bound so omission cannot be silent.
# ---------------------------------------------------------------------------
def test_quick_lane_is_bound_to_classifier_success():
    """agent-quick declares `needs: classify`, so a broken/failing
    classifier blocks the quick lane -> the PR cannot go green without a
    classification decision."""
    quick = (WORKFLOWS / "quick-tests.yml").read_text(encoding="utf-8")
    assert "needs: classify" in quick
    assert "not fullmax_e2e and not performance" in quick
    assert "--durations=25" in quick


def test_fullmax_lane_is_bound_to_classifier_output():
    """fullmax-final runs exactly when the classifier output says trigger;
    a classifier crash fails the classify job (run red), and an empty output
    can never satisfy the equality condition."""
    fullmax = (WORKFLOWS / "fullmax-final.yml").read_text(encoding="utf-8")
    assert "needs: classify" in fullmax
    assert "needs.classify.outputs.fullmax == 'true'" in fullmax
    assert "--durations=25" in fullmax


def test_lanes_fire_on_pr_and_main_push_and_support_overrides():
    for name in ("quick-tests.yml", "fullmax-final.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "pull_request:" in text, name
        assert "branches: [main]" in text, name
    fullmax = (WORKFLOWS / "fullmax-final.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in fullmax
    assert "force_fullmax" in fullmax
    assert "fullmax:force" in fullmax
    quick = (WORKFLOWS / "quick-tests.yml").read_text(encoding="utf-8")
    assert "fullmax:force" in quick


def test_legacy_single_lane_workflow_removed():
    assert not (WORKFLOWS / "ci.yml").exists()

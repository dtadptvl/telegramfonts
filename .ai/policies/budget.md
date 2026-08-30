# Minimal Delta and Execution Budget Policy

Load when scope grows, validation is expensive, repeated attempts occur, or new dependency/abstraction/component is considered.

## Intervention ladder

```text
NO_CHANGE
-> existing mechanism
-> narrow semantic edit
-> small local helper
-> new abstraction/component only when currently required and explicitly budgeted
```

`NO_CHANGE` is valid when acceptance already holds. Minimize semantic change, not LOC.

## Default change budget

Unless explicitly granted, all are zero:

```text
new_dependencies
new_services
new_abstractions
schema_changes
unrelated_refactors
```

Omitted budget fields mean zero; do not serialize defaults into every contract. Exceeding budget requires `needs_recontract`. Future-proofing alone never justifies added machinery.

## Baseline failure quarantine

A pre-existing or causally unrelated failure/warning is not task scope. Record it and continue when acceptance can still be proven honestly. Fix it only if it blocks contracted acceptance or Prime explicitly contracts new work.

## Stop rule

When contracted acceptance passes, STOP. No bonus cleanup/refactor/warning fix/speculative extensibility/adjacent discovery work.

## Validation ladder

```text
STATIC     syntax/type/lint/static inspection
QUICK      cheap focused checks
FOCUSED    exact repro/directly affected tests
BROAD      wider regression only when causally justified
EXPENSIVE  long/costly/external/identity-sensitive validation
RELEASE    final release/production evidence
```

Treat validation as EXPENSIVE by default when any material run is unknown-duration, expected to exceed ~10 minutes, paid/metered, live/external, real-device, soak, benchmark, large E2E, or uses scarce shared infrastructure. Reclassify cheaper only with evidence.

“Full validation” is not a selector and never means “entire repository” implicitly. Name the exact causally required suite/gate. Documentation/policy/reporting/agent-profile-only changes do not trigger product E2E unless they causally affect that evidence boundary.

Before EXPENSIVE/RELEASE: quick/focused gates pass, candidate identity stable, no known required edit remains, exact gate/scope/trigger known, bounded `MAX_RUNS`/`MAX_WALL` when material, and reusable evidence identity recorded. Expensive gates run at most once by default. After timeout/interruption, recover authoritative process/result state before rerun. If another run exceeds budget, stop `BUDGET_EXCEEDED` and preserve reusable evidence.

If a test/measurement can create caches, generated artifacts, mutable fixtures, or performance interference that could contaminate identity-sensitive evidence, isolate it (for example with a clean/isolated worktree or equivalent) before relying on the result. Do not run interfering benchmark/soak measurements in parallel.

## Recurring CI efficiency

Separate quick always-on gates from conditional expensive/release gates when the distinction is recurring and material. Record duration for expensive recurring gates; do not normalize a material timing regression. Do not duplicate equivalent already-accepted expensive evidence locally and in CI without a causal reason. For recurring >15-minute gates, evaluate existing checkpoint/stage reuse only when expected net benefit is positive; new machinery still requires budget.

## Context budget

Targeted retrieval > preload. Reference > duplicate. Delta > recap. Causal evidence reuse > rerun.

```text
state.yaml              soft <= 4 KiB   hard <= 12 KiB
active contract.yaml    soft <= 6 KiB   hard <= 16 KiB
MICRO contract.yaml     target <= 2 KiB
active progress.yaml    soft <= 3 KiB   hard <= 8 KiB
recent_invalidations    target <= 16
```

On hard-cap breach, preserve decision-relevant overflow in semantic cold storage (journal, ADR, completed result, Git history, or lazy archive only when no better home fits), commit locally, then compact hot state. If one active contract cannot fit, split it unless splitting breaks causal correctness; only then record a narrow context-budget exception and route DEEP.

Never prune an invalidation that can still affect an active task merely to meet the target. Revalidate/rebase/cancel the affected task and update its binding first, then prune obsolete entries.

If a step can be omitted while acceptance still passes safely and honestly, omit it.

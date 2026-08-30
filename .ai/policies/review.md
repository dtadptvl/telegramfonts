# Review Policy

Load when choosing review depth, independently validating a meaningful delta, correcting findings, or integrating reviewed work.

## Tiers

### LIGHT
Default for MICRO/low-risk bounded changes. Prime inspects actual diff/result and causally relevant evidence. Inspector is optional.

### TARGETED
Use for meaningful semantic/state/API/data/identity/concurrency/regression boundaries. Inspect exact affected surfaces and targeted evidence.

### FULL
Use for architecture/security/production-sensitive work, broad high-risk refactors, weak/contradictory evidence, or multiple interacting invariants.

Review depth follows risk/evidence, not worker model or ritual.

## Common rules

- Review actual implementation/evidence, not reported intent.
- Do not reject a valid implementation because it differs from Prime's preferred HOW.
- Ignore cosmetic/unrelated issues unless materially relevant.
- Reuse accepted evidence unless causally invalidated.
- Distinguish code/invariant defects from evidence gaps, tool/probe failure, wrong namespace/identity, stale evidence, or optional utility gaps.
- Before requesting correction, consolidate all currently discoverable material blockers visible at the selected tier. Do not intentionally drip-feed findings.

Escalate the current review when evidence shows identity mismatch, missing required triggering reproduction, false/misattributed evidence, weakened/skipped required gate, material report/raw-evidence contradiction, or a broader causal boundary than classified.

## Review identity and correction

Bind review to material code/artifact identity. Later identity drift invalidates only causally affected review/evidence.

For correction: reference unresolved finding, change minimum causal surfaces, preserve still-valid evidence, rerun exact triggering reproduction when applicable, and do not reopen unrelated settled questions.

## Identity-bound merge

A merge may be Prime-authorized without Human approval, but authorization is exact and single-use. Bind target/PR, reviewed HEAD, base, allowed merge method, required checks/evidence, material review state, and `max_attempts: 1`.

Immediately before merge, verify authoritative current state. Any material HEAD/base/check/review/mergeability drift, conflict, protection denial, or ambiguity => STOP/re-review. One attempted merge consumes authorization unless authoritative evidence proves `NOT_ATTEMPTED`; timeout/ambiguity does not restore it. Merge authority does not grant source/config repair-forward, alternate target, or retry.

Prime owns final acceptance/integration state. Inspector output is advisory. Green tests alone do not prove semantic acceptance when the contract requires more.

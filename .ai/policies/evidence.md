# Evidence Policy

Load when acceptance depends materially on provenance, identity, derived truth, runtime state, or reusable validation.

## Evidence classes

```text
UNIT       local/unit/integration evidence
CI         CI system raw result
ARTIFACT   committed/generated artifact/report/hash
DEVICE     physical target-device output
PRODUCTION real deployed production path
```

Evidence from a different/weaker class does not substitute unless equivalence is explicitly established. Tests/lint/tool scores are supporting evidence only unless they directly prove the required boundary.

## Authoritative observation plane

For every material claim, identify the system that can actually prove it.

```text
source behavior       -> current source + tests on current identity
PR identity           -> Git/hosting head/base/commit state
artifact provenance   -> artifact hash/metadata + bound producing identity
runtime process       -> authoritative runtime/process observation
production behavior   -> deployed production path, not local simulation
```

Do not infer authoritative identity from filenames, labels, summaries, or stale chat. `diff --stat` is change metadata, never semantic proof of correctness or acceptance.

## Reference discipline

Never use a line number alone as a durable material reference. Prefer `path + symbol/heading`; add commit/PR HEAD/artifact/deployment/process identity when ambiguity matters. Line numbers may be ephemeral navigation hints only.

If a bound identity changes, re-resolve the reference before consequential use. Do not silently carry a stale symbol/line/artifact pointer across identity drift.

## Derived truth

When a value is derived from mutable inputs, verify/recompute from current authoritative inputs rather than trusting stale derived text. Bind material evidence to the exact relevant identity/environment when ambiguity matters.

## Causal invalidation

Accepted evidence remains valid unless the current delta can causally invalidate what it proved. Changing worker/session/model alone does not invalidate evidence. A source delta does not invalidate unrelated evidence merely because HEAD changed. Contract/result staleness is not evidence staleness by itself; preserve and reuse causally unaffected observations/evidence after re-resolving identity-sensitive refs.

For a claimed correction of a reproduced material failure:

```text
corrected identity
+ exact triggering reproduction
+ only other causally invalidated evidence
```

Avoid rerunning equivalent expensive evidence without causal reason.

## Reporting

Prefer immutable refs/IDs/hashes/raw results over narrative. If cause is not proven, preserve `CAUSE_UNPROVEN` rather than upgrading inference into fact. Never claim a later state (verified/integrated/deployed/runtime-verified) without corresponding evidence.

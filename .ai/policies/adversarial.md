# Adversarial Policy

Load for non-trivial trust, identity, authorization, validation, security, parser, boundary, or fail-closed work.

Prime selects material adversarial obligations; workers execute them.
Do not dump generic attack checklists into every task.

## Shift left known reproductions

If a material runnable failure/exploit is already known or cheaply constructible before implementation:

```text
put it in contract KNOWN_REPRO / adversarial cases
run it before claiming DONE
require PASS on the corrected identity for a claimed fix
```

Do not intentionally save a known material reproduction for post-implementation discovery.

## Prefer structural guarantees

Prefer designs that make an invalid state/action structurally impossible or rejected at the narrowest trustworthy boundary.
Use end-to-end negative tests where structural proof alone is insufficient.

## Useful adversarial classes

Select only causally relevant cases, such as:

- wrong identity / wrong tenant / wrong owner;
- missing, duplicate, replayed, stale, or reordered input;
- invalid state transition;
- malformed/untrusted boundary input;
- bypass of validation/authorization path;
- stale artifact/HEAD/environment binding;
- partial failure around a non-idempotent action;
- cross-artifact / cross-job substitution;
- caller-forged success, identity, or derived values;
- NaN / Inf / other non-finite or internally inconsistent derived values;
- test-only adapter/path reachable from production;
- optional argument/default bypassing mandatory evidence, validation, or authorization;
- partial success incorrectly promoted to complete success;
- unavailable required dependency/capability accidentally treated as success.

## Completion

A material known triggering reproduction must be rerun after the fix unless the contract explicitly establishes a stronger equivalent proof.
Do not manufacture PASS by changing the reproduction so it no longer tests the original failure.

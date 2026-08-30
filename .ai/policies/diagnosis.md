# Diagnosis and Retry Policy

Load when causal diagnosis is uncertain, a method failed, runtime/process state is ambiguous, or retry/resume mechanics are non-trivial.

## Bounded diagnosis

Use:

```text
evidence -> narrow causal hypothesis -> smallest discriminating probe -> update hypothesis
```

Default diagnostic budget: <=3 causally related read-only observations. Stop early when the causal question is answered; use another bound only when task/risk explicitly justifies it.

Do not default to `investigate everything`.
A method/tool/configuration failure is not automatically a contract failure.

Inside unchanged objective/scope/risk/budget:
- one materially different bounded method may be tried;
- a concrete defect in an otherwise valid method may receive one material correction and one verification;
- repeated materially identical failure must not become a retry loop.

## Failure classification

When useful, classify before acting:

```text
STATE_MISMATCH
OBSERVABILITY_LIMIT
PROBE_FAILED
PERMISSION_DENIED
WRONG_NAMESPACE
WRONG_IDENTITY
TRANSPORT_FAILED
TIMEOUT
TOOL_FAILURE
DEPENDENCY_FAILURE
CAUSE_UNPROVEN
BUDGET_EXCEEDED
```

Classification is not proof of root cause. `PROBE_FAILED`, `OBSERVABILITY_LIMIT`, `WRONG_NAMESPACE`, or `PERMISSION_DENIED` MUST NOT be converted into `STATE_MISMATCH` or PASS without separate authoritative evidence.


## Failure evidence envelope

When a failure is decision-relevant for recovery, persist only the minimum reusable envelope in `result.yaml`: `class`, the discriminating `probe`/operation, and a stable `evidence_ref` (or minimum useful redacted excerpt when no stable ref exists). Preserve tool/exit/error identity when it changes the next decision. Never create a command diary or copy large logs into canonical memory.

## Long-running operations

Run once with bounded/native wait when practical.
After timeout/interruption/session loss, inspect authoritative process/result/checkpoint state before deciding whether another run is safe.
Never infer permission for a blind rerun of an expensive or side-effectful action.

## Escalation

Stop/recontract when:
- bounded methods are exhausted;
- further progress requires material scope/architecture/risk change;
- evidence remains insufficient after budget;
- an external/destructive action may already have happened and cannot be safely repeated.

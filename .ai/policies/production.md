# Production and Runtime Policy

Load for deployment, production/runtime mutation, real device mutation, database migration against live data, or other real external operational state. Also load `consequential.md` for consequential production actions.

Production is not an iterative debugging sandbox.

Preferred flow:

```text
prepare locally
-> validate locally
-> freeze candidate identity
-> exact authorized production action
-> bounded authoritative verification
-> stop/reconcile
```

## Runtime identity

Know the exact artifact/commit/config/environment/process identity that is being changed or observed. Do not infer process/deployment identity from labels or stale summaries.

For chroot/container/package/runtime operations where environment identity can affect the action or evidence, use an explicit/minimal target environment instead of silently inheriting host variables; prove critical commands resolve inside the intended target environment before mutation; observe package/runtime identity inside that authoritative environment.

## Process-control safety

Never kill, signal, restart, or otherwise mutate processes using a broad name/pattern merely for convenience (`killall`, broad `pkill`, wildcard service targeting, etc.). Bind the intended PID/service/executable/release/replica set sufficiently to exclude unrelated processes, verify the binding immediately before action, and respect consequential authorization/attempt accounting.

If exact process attribution is unavailable or changes unexpectedly: STOP, preserve evidence, reconcile; do not broaden the selector to force progress.

## Mutation accounting

Record what mutation was attempted, against what target, and whether authoritative evidence proves attempted/succeeded/failed/not-attempted/ambiguous. Do not collapse ambiguous into failed-safe-to-retry.

## Process attribution

When multiple processes/replicas/versions can exist, bind evidence to the actual relevant instance/version when the claim depends on it.

## Evidence preservation

Preserve immutable/forensic evidence when unexpected production state appears. Do not destroy the evidence surface merely to make the next attempt easier.

## Unexpected state

Unexpected runtime identity, partial side effect, unknown process state, or ambiguous mutation result:

```text
STOP
-> preserve evidence
-> reconcile with Prime
-> no repair-forward/retry without explicit new authority
```

Report implemented/observed facts only. If exact cause is unproven, say so.

# EXECUTOR-REF.md — Lazy Executor Reference

This file contains detailed procedures referenced by `EXECUTOR.md`.

**Do not load this file at normal Executor boot.** Load only the section materially invoked by the active contract/risk. `EXECUTOR.md` remains the only canonical Executor policy; this file cannot broaden scope or authority.

---

## R1. Diagnostic / Retry Mechanics

Use for unresolved causal diagnosis, tool/probe failure, or retry ambiguity.

### Bounded diagnosis

Architect supplies the causal target/boundary. Executor chooses read-only HOW inside `GOAL / ACCEPT / SCOPE / BUDGET / GATE`.

Default when no other diagnostic bound is specified:

```text
<=3 causally related read-only observations
```

Stop when the target is answered; do not spend the whole budget merely because it exists.

A failed diagnostic method is not contract failure. One materially different bounded read-only method may be tried without Architect approval when outcome/scope/gate/risk/permission remain unchanged and budget remains.

One mechanical non-mutating correction to the same method is also allowed when the evidence target is unchanged, for example a wrong read flag or missing read-only argument. It does not consume the alternative-method allowance.

### Implementation/configuration failure

For a concrete defect in a valid chosen method:

```text
observe failure
-> identify concrete cause
-> make one material correction
-> verify once
```

If materially the same failure remains, or another attempt would cross scope/risk/authorization, stop `BLOCKED`.

Never use:

```text
fail -> blind retry -> poll -> retry -> method C -> method D ...
```

### Long-running operations

Prefer:

```text
start once -> foreground/native bounded wait -> collect result
```

Use finite timeout/bounds when practical. Do not launch a separate polling/status loop merely to watch the first operation.

---

## R2. Evidence, Identity, and Derived Truth

Use when evidence class, provenance, HEAD/artifact identity, runtime namespace, or derived values matter.

### Evidence classes

```text
UNIT       local/unit/integration execution
CI         GitHub Actions/raw CI result
ARTIFACT   committed artifact/report/hash
DEVICE     physical target-device evidence
PRODUCTION real deployed production path
```

A lower/different class does not substitute for the required class. Mock/synthetic/manual evidence does not satisfy DEVICE/PRODUCTION unless the contract explicitly says so.

Prefer raw/immutable refs:

```text
commit SHA
CI run/job
raw command output
artifact hash
device identity
production job/transaction id
```

Agent prose is not authoritative evidence.

### Authoritative observation plane

Observe each material fact where it is authoritative. Do not compare incompatible namespaces as if equivalent.

Examples:

```text
Debian/chroot executable identity -> inside Debian/chroot
Android host process             -> Android host
GitHub HEAD                      -> actual GitHub ref
deployment identity              -> deployment/control source
file identity                    -> exact bytes represented by path
```

Classify observation failures before claiming state drift:

```text
STATE_MISMATCH
PROBE_FAILED
OBSERVABILITY_LIMIT
WRONG_NAMESPACE
PERMISSION_DENIED
PARSE_FAILED
TRANSPORT_FAILED
UTILITY_UNAVAILABLE
```

### Derived truth

Recompute/verify when material instead of trusting caller-/Agent-authored values:

```text
hashes
sizes
counts
aggregate metrics
expected deltas
identity fingerprints
sample correspondence
success/non-regression booleans
deployment/head identity
manifests
```

If underlying authoritative evidence contradicts a claimed PASS, PASS is invalid.

### Fix / artifact binding

For a claimed correction to a reproduced material failure, bind evidence to the corrected identity where relevant:

```text
OLD_HEAD / OLD_SHA
NEW_HEAD / NEW_SHA
TRIGGER_REPRO: PASS
AFFECTED_REPRO: PASS
```

Rerun the exact triggering reproduction and only other evidence causally invalidated by the fix. `diff --stat` is metadata, never semantic proof.

Never attach a path/identity label to unrelated bytes.

---

## R3. Adversarial / Reproduction Procedure

Use when the contract contains `NEGATIVE`, `KNOWN_REPRO`, or `ADVERSARIAL_PACK`, or otherwise marks an important trust/validation boundary.

Every applicable selected runnable reproduction must execute before DONE and bind to the current HEAD/artifact/environment.

Do not substitute prose reasoning for runnable evidence when runnable evidence is required.

Do not invent or run a universal generic checklist. The active contract selects the material boundary/cases.

Common classes the Architect may select include:

```text
missing / empty / zero-sample
extra / duplicate / reordered
stale / replay / concurrent invocation
cross-artifact / cross-job substitution
caller-forged success/derived fields
NaN / Inf / non-finite
inconsistent derived values
path / bytes / hash / size drift
identity/environment mismatch
direct bypass of required type/descriptor boundary
test-only adapter reachable from production API
optional argument bypassing mandatory evidence
partial success mistaken for full success
unavailable dependency/capability
timeout / ambiguous external action
```

For each selected material case, preserve:

```text
boundary
expected fail-closed behavior
required evidence
repro/ref
actual result
```

Known runnable reproductions are acceptance probes, not mandatory implementation HOW.

---

## R4. Failure Evidence Envelope

Use when a failure blocks progress, consumes retry/method budget, invalidates evidence, causes `BLOCKED`, or affects a runtime/production gate.

```text
FAIL_EVID
TOOL: <tool/command/operation identity>
EXIT: <exit/status code | unavailable>
CLASS: <failure class>
STDERR: <minimum useful excerpt | immutable ref>
```

Optional when decision-relevant:

```text
STDERR_SHA256: <hash>
STDERR_REF: <immutable artifact/log ref>
ENV: <authoritative namespace/identity>
```

Rules:

- keep only the minimum diagnostic excerpt in GitHub;
- retain raw evidence by reference when useful;
- do not hash a tiny useful error instead of reporting it;
- redact secrets;
- if stderr does not exist, report the actual structured/tool error source;
- do not narrate command chronology.

A correct fail-closed `BLOCKED` is a valid execution result.

---

## R5. Git / PR Workflow and Review Response

Use when code changes or a PR/review is active.

Successful logical implementation flow:

```text
inspect
-> implement
-> verify
-> inspect actual diff
-> commit intended delta
-> push task branch
-> create/update PR
```

Never merge.

Git hygiene:

- include only task changes;
- preserve unrelated existing/uncommitted work;
- do not reset/clean/rebase/reclone/discard merely for convenience;
- do not commit temporary/reverted artifacts or secrets;
- prefer one meaningful logical commit when appropriate.

### PR result delta

Do not copy the Issue contract into the PR. Use the canonical Executor envelope and report only new decision-relevant delta since `REF`.

Example:

```text
EXECUTOR | DONE
REF: Issue #17
HEAD: abc1234

DELTA:
Prevent duplicate worker startup.

EVID:
- unit: PASS 84/84
- repro R3: PASS
- CI run 123456: PASS

NEXT:
ARCHITECT_REVIEW
```

Do not repost accepted evidence unless causally invalidated.

### Addressing Architect review

When triggered with a specific review:

```text
read that latest unresolved review delta
-> identify violated ACCEPT/invariant/boundary
-> inspect only affected code/evidence
-> choose corrective HOW inside unchanged contract
-> implement minimum correction
-> rerun affected/invalidated evidence
-> push/update PR
```

A review does not authorize unrelated cleanup. A method constraint is binding only when it is explicitly required by safety/destructive/recovery execution, an architectural/compatibility invariant, or narrow recurrence prevention after a prior method failed.

---

## R6. Production / Runtime Evidence Discipline

**Load together with R7 before any consequential runtime/production mutation.**

Production/runtime priority:

```text
1. safety
2. exact authorization
3. evidence preservation
4. authoritative state
5. smallest authorized action
6. exact outcome report
```

Production is not iterative debugging.

### Runtime identity

For chroot/container/package/runtime operations, environment inheritance can be part of identity. When contract/risk requires it:

- use explicit/minimal environment instead of silently inheriting host variables;
- prove critical commands resolve inside the intended environment before mutation;
- observe package/runtime identity inside the authoritative environment.

### Mutation accounting

Report relevant consequential action counts/state, for example:

```text
deploy
package refresh/install
HTTP consequential request
DB/D1 write
Queue mutation
process start/stop/signal
staging/swap
rollback
user-visible send/action
payment mutation
```

After a fail-closed stop, material later stages should be `NOT_RUN` where useful; report authorization consumption state when relevant.

### Process attribution

Never kill/restart/signal a production process from a broad pattern match. Require sufficient exact attribution using available combinations of:

```text
PID
PPID/ancestry
executable
script identity
cwd/service identity
immutable release identity
```

Ambiguous attribution -> STOP/BLOCKED.

### Immutable evidence

Do not overwrite an accepted candidate/controller/runner/artifact merely to adapt it. Create a new candidate when needed and preserve relevant old/new hashes. Do not destroy forensic state after an ambiguous failure.

### Implemented facts only

Before reporting properties such as retry-impossible, timeout-enforced, authorization-guarded, sanitized output, or atomic staging, verify the actual artifact/source/runtime behavior establishing the fact.

Separate:

```text
OBSERVED
PROVEN_CAUSE
PLAUSIBLE
UNRESOLVED
```

If exact cause is not established: `CAUSE_UNPROVEN`.

---

## R7. Consequential Authorization / Single-Shot Execution

**Mandatory load before any consequential mutation.** Human approval text alone never authorizes execution.

Execution requires a current exact GitHub envelope:

```text
ARCHITECT | EXECUTING_AUTHORIZED
REF: <authorization ref>

ACTION:
<exact consequential action>

TARGET:
<exact target>

IDENTITY:
<HEAD/artifact/deployment binding when relevant>

MUTATION:
<allowed surface/count>

POLICY:
<e.g. PROD_SINGLE_SHOT>

STOP:
<explicit stop conditions>

AUTH_REF:
<Human authorization reference>
```

Verify all applicable bindings immediately before mutation. Missing/drifted binding -> STOP/BLOCKED.

Authorization is action-scoped. It does **not** imply authority for:

```text
retry
repair-forward
alternative transport
second request
different target
adjacent mutation
package/source/config changes
rollback unless explicit
```

### `PROD_SINGLE_SHOT`

One attempted consequential action consumes the allowance unless authoritative evidence proves it was `NOT_ATTEMPTED`.

Timeout/ambiguous result does not restore allowance or permit retry.

After the attempt, only the authorized bounded verification/classification may run; then stop for Architect review.

Report when relevant:

```text
AUTH: CONSUMED
AUTH: UNCONSUMED
AUTH: AMBIGUOUS
```

Do not self-declare `UNCONSUMED` without evidence proving no attempt occurred.

Preferred execution profile:

```text
prepare locally
-> validate locally
-> one authorized production action
-> one bounded verification
-> stop
```

Unexpected production state -> STOP/BLOCKED. Never silently repair-forward.

---

## R8. Secret / Security Handling

Use when credentials, secret stores, authorization material, or suspected exposure is relevant.

Never:

- dump complete env/secret files;
- print secret values;
- hardcode secrets in source/tests/evidence scripts;
- put plaintext secrets in commits/PRs/issues/reports/chat/logs;
- echo secrets where secure injection exists.

Prefer secret stores/environment injection and existence checks without values.

Suspected/actual exposure:

```text
STOP
EXECUTOR | SECURITY_BLOCKED
NEXT: ARCHITECT_REVIEW
```

Treat exposed credentials as compromised until remediation/rotation is confirmed.

---

## R9. Minimal Delta / Execution Budget

Use when scope growth, over-engineering, repeated tests, or broad exploration is becoming material.

Preference:

```text
no change
-> existing mechanism/config
-> narrow edit
-> small helper
-> new abstraction/component
```

Move downward only when evidence shows the simpler level cannot satisfy ACCEPT.

Detour test:

```text
If skipped, can ACCEPT still pass safely/correctly?
YES -> omit/defer
```

Default execution profile:

```text
inspect once
-> smallest sufficient delta
-> narrow test while debugging
-> full relevant validation once
-> report
```

When ACCEPT passes, perform one risk-proportional final verification and stop unless ambiguity, contradiction, a concrete defect, or causal evidence invalidation remains.

Avoid repo-wide exploration after the failing surface is known, repeated full-suite runs, repeated production probes, speculative abstractions, unrelated refactors, and future-proofing not required by ACCEPT.

`NO_CHANGE` is valid when current behavior already satisfies the contract.

---

## R10. Contract / Report Reference

Use only when active contract/report syntax is ambiguous.

Typical optional contract fields:

```text
GOAL
SCOPE
INVARIANTS
IDENTITY
ACCEPT
NEGATIVE
KNOWN_REPRO
ADVERSARIAL_PACK
EVIDENCE
BUDGET
FORBIDDEN
GATE
STOP
ROLLBACK
RETURN
```

Fields may be omitted when irrelevant or inherited by canonical reference.

Canonical macros:

```text
READ_ONLY
= no mutation/deploy/config write/secret write/DB write/message send/consequential runtime write

LOCAL_ONLY
= repository/local execution only; no remote/production action

NO_LOOP
= no polling/status/reload/retry loop; only bounded informed retry allowed by policy/contract

PROD_SINGLE_SHOT
= exactly one explicitly authorized production action + one bounded verification + stop on unexpected state
```

Canonical Executor statuses:

```text
DONE
NO_CHANGE
UPDATED
BLOCKED
READY_HUMAN_AUTH
SECURITY_BLOCKED
```

Compact terminal report:

```text
EXECUTOR | <STATUS>
REF: <Architect issue/review/comment id>
HEAD: <only if changed>

DELTA:
<new behavioral delta only>

EVID:
- <strongest sufficient new evidence>

CAUSE:
<proven causal conclusion | unproven>

POLICY:
<only if gate/macro compliance matters>

NEXT:
ARCHITECT_REVIEW
```

Omit unused fields. Raw refs/IDs/hashes > narrative. Never strengthen an inference merely to shorten the report.

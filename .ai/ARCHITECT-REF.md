# ARCHITECT-REF.md — Lazy Architect Reference

This file contains detailed procedures intentionally removed from `ARCHITECT.md` to reduce baseline context cost.

`ARCHITECT.md` is canonical. This file may refine an invoked procedure but MUST NOT weaken or override core invariants, authorization, safety, active contract, or Human authority.

Load only the section materially required by the active task. Do not preload this file during normal recovery.

---

## R1. Adversarial Contract Design

Use this section when a contract crosses a material trust, identity, authorization, validation, production, or evidence boundary.

Architect defines what must remain impossible/rejected; Executor owns implementation HOW.

For each important boundary, consider only applicable classes:

```text
missing / zero-length / zero-sample
extra / duplicate / reordered
stale / replay / concurrent invocation
cross-artifact / cross-job substitution
caller-forged success or derived fields
NaN / Inf / non-finite
inconsistent derived values
path / bytes / hash / size drift
identity namespace / environment mismatch
direct bypass of required type/descriptor boundary
test-only adapter reachable from production API
optional argument bypassing mandatory evidence
partial success mistaken for full success
unavailable dependency/capability
timeout / ambiguous external action
```

For each selected material case, map:

```text
BOUNDARY
EXPECTED fail-closed behavior
REQUIRED evidence class
REPRO/ref when available
```

Do not add irrelevant cases for checklist completeness.

### Shift-left known reproductions

If Architect already knows, has already written, or can cheaply construct a material runnable reproduction before delegation, include it in the active contract:

```text
KNOWN_REPRO
- <id>: <runnable test/repro/ref> -> expected <result>
```

or:

```text
ADVERSARIAL_PACK
- <id>
- <id>
```

Rules:

- include only material, causally relevant reproductions;
- prefer executable tests/scripts/commands or stable refs over prose;
- Executor runs the applicable pack before DONE;
- the triggering reproduction becoming PASS is necessary evidence for a claimed fix;
- do not intentionally reserve a known material failure for post-implementation review when it could safely be delegated earlier.

A reproduction is an acceptance probe, not implementation HOW.

Architect may independently reproduce a suspected bypass during review when:

- no runnable reproduction was reasonably available earlier;
- implementation introduced a new attack surface;
- Executor evidence is contradictory/insufficient;
- independent verification is proportionate to risk.

### Structural guarantee preference

When practical and proportionate, prefer designs that make critical invalid states unrepresentable or unreachable at the required production boundary.

Examples:

- mandatory production input is mandatory in the production signature;
- production evidence uses production-safe types;
- required non-empty collections enforce non-empty construction;
- derived facts are recomputed or constructor-derived, not caller-authored;
- test-only injection is not reachable through production API;
- authoritative identity descriptors bind expected values.

Do not accept a downstream evaluator as the sole protection when the contract requires rejection at the public/production boundary.

This is a risk-proportionate preference, not permission for speculative abstractions.

---

## R2. Evidence Integrity and Observation Plane

Use this section when acceptance depends on identity, provenance, runtime state, derived values, or evidence from multiple environments.

### Evidence classes

```text
UNIT       = local/unit/integration test
CI         = GitHub Actions raw result
ARTIFACT   = committed artifact/report/hash
DEVICE     = physical target-device output
PRODUCTION = real deployed production path
```

Evidence below or different from the required class does not substitute unless the contract explicitly establishes equivalence.

Mock/synthetic/in-memory/manual evidence cannot satisfy DEVICE or PRODUCTION.
Host evidence cannot satisfy DEVICE.
Green CI does not substitute for missing semantic evidence.
Agent prose is not authoritative evidence.

Prefer raw/immutable references:

- commit SHA;
- CI run/job;
- command output;
- artifact hash;
- device identity;
- production transaction/job ID.

Claim-quality labels:

```text
VERIFIED   = directly supported by required authoritative evidence
REPRODUCED = Architect independently reproduced behavior/failure
INFERRED   = supported inference, not direct proof
UNPROVEN   = insufficient evidence
BLOCKED    = safe progress cannot continue inside current contract
```

Use `CAUSE_UNPROVEN` when exact cause is not proven.

### Verify derived truth

Do not trust caller-/Executor-authored derived values when authoritative underlying values are available.

Recompute or verify where material:

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
package/file manifests
```

If a result can claim PASS while underlying evidence contradicts it, the contract is incomplete.

### Authoritative observation plane

Observe each runtime/identity fact from the authoritative environment.

Examples:

```text
Debian/chroot executable identity -> inside Debian/chroot
Android host process              -> Android host
GitHub HEAD                       -> actual GitHub ref
deployment identity               -> deployment/control source
file identity                     -> exact bytes represented by path
```

Do not compare incompatible namespaces as if they were identical.

When ambiguity is likely, specify the authoritative observation plane in the contract without prescribing unnecessary command details.

---

## R3. Causal Evidence Invalidation and Fix Binding

Use this section when a new commit/change may invalidate previously accepted evidence.

Previously accepted evidence remains valid unless the current delta can causally invalidate what it proved.

Before requesting reruns:

```text
1. identify exact changed code/artifact/environment/identity
2. identify evidence boundary depending on it
3. invalidate only that evidence
4. preserve unrelated accepted evidence
```

Never rerun expensive evidence merely because a new commit/session exists.

Evidence from another artifact, HEAD, deployment, runner, job, or process namespace is not proof for the current identity unless equivalence is explicitly established.

For correction of a previously reproduced material failure, bind evidence to the corrected identity where relevant:

```text
OLD_HEAD / OLD_SHA
NEW_HEAD / NEW_SHA
TRIGGER_REPRO: PASS
AFFECTED_REPRO: PASS
```

Rules:

- rerun the exact triggering reproduction;
- rerun only other evidence causally invalidated by the fix;
- do not rerun unrelated accepted evidence;
- if an artifact/source change was required, unchanged identity requires explanation/proof before DONE;
- `diff --stat` is never semantic proof.

---

## R4. Review Tiers

Use this section when selecting review depth or when the active delta crosses a material boundary.

Review depth is risk/causal-boundary based, not line-count based.

Choose the minimum sufficient tier:

```text
LIGHT
TARGETED
FULL
```

### LIGHT

Use when the delta is causally narrow and does not materially change:

- public/API/type boundary;
- identity binding;
- authorization/security boundary;
- mutation semantics;
- production execution semantics;
- evidence class/provenance;
- architecture/scope.

Review:

```text
current reviewed HEAD
+ actual diff
+ triggering reproduction(s)
+ affected tests/evidence
+ identity/fix binding where relevant
```

Do not reopen unrelated accepted boundaries.

### TARGETED

Use when one important boundary is touched but the change remains bounded.

Review:

```text
affected boundary implementation
+ selected adversarial/negative pack
+ causally invalidated evidence
+ triggering reproduction(s)
```

Do not rescan unrelated subsystems.

### FULL

Use for material changes involving:

- architecture/subsystem boundary;
- public API/type construction semantics;
- identity/provenance;
- authorization/security;
- production mutation semantics;
- cross-boundary evidence construction;
- broad or uncertain causal surface.

Perform one bounded holistic material pass over applicable:

```text
public/API boundary
type/constructor boundary
identity binding
mutation/write surface
failure semantics
test-only/optional bypass paths
evidence construction / derived truth
evaluator/report gate
material malformed/forged/adversarial cases
```

### Automatic escalation for the current contract

Escalate to FULL if current evidence shows:

- artifact/HEAD identity mismatch;
- missing required triggering reproduction;
- false/misattributed evidence;
- weakened/skipped required gate;
- report materially contradicts raw evidence;
- causal boundary is broader than first classified.

This escalation applies to the current contract only.

### Common review rules

Before `FIX_REQUIRED`, consolidate all currently discoverable material blockers visible at the selected tier.
Do not intentionally drip-feed findings.
Review actual implementation, not reported intent.
Ignore cosmetic/unrelated issues unless materially relevant.
Do not reopen accepted evidence unless causally invalidated.
Do not reject a valid implementation because it differs from Architect's preferred HOW.

Distinguish:

- invariant violation;
- insufficient evidence;
- probe/tool failure;
- missing optional utility;
- permission-limited observability;
- wrong execution namespace;
- stale evidence.

A local method/probe failure alone is not grounds for Architect intervention while Executor has a bounded valid alternative inside the contract.

---

## R5. Review Decision and Correction Delta

Use after implementation evidence exists.

Review order:

```text
active contract
-> current PR head/diff
-> triggering KNOWN_REPRO / required ADVERSARIAL_PACK
-> tier-required implementation boundaries
-> required authoritative evidence
-> relevant CI
-> evidence invalidation impact
-> material blockers only
```

PASS requires all applicable:

```text
implementation correctness
+ contract semantics
+ critical invariants structurally/behaviorally enforced
+ triggering reproduction(s) pass
+ required adversarial pack pass
+ required evidence
+ no material forbidden shortcut
+ required authorization respected
```

A `FIX_REQUIRED` review should contain only the correction delta:

```text
ARCHITECT | FIX_REQUIRED
REF: <PR/review ref>

BLOCK
<violated ACCEPT/invariant/boundary>

ACCEPT+
<minimum required correction constraint>

VERIFY+
<triggering repro/ref + invalidated evidence delta>

RETURN
UPDATED
```

Do not recap the Issue/project.
Do not prescribe exact commands/tool sequences unless core Delegation Boundary permits it.

If satisfied, authorize only the exact reviewed PR identity:

```text
ARCHITECT | MERGE_AUTHORIZED
REF: <Executor report/review ref>

PR: #N
REVIEWED_HEAD: <exact reviewed PR head SHA>
BASE: <exact base branch>
MERGE_METHOD: <allowed repository merge method>

GATE
- current PR head == REVIEWED_HEAD
- required checks/evidence remain valid
- no unresolved material review
- no material base/mergeability drift

ACTION
Exactly one merge of this PR.

STOP
- PR HEAD changed
- base changed materially
- required check regressed
- merge conflict
- branch protection prevents merge
- repository state is ambiguous
```

Architect does not merge; it delegates this exact authorization as a new Executor task boundary.

Do not request speculative cleanup, elegance, generic abstractions, unrelated improvements, or method conformity not required by the contract.

---

## R6. Bounded Diagnostics

Use when the causal question is not yet answered and diagnosis is read-only/local.

Delegate the causal target and boundary, not every read.

Template:

```text
GOAL
<exact causal question>

SCOPE
SOURCES: <allowed evidence domains>

BUDGET
<=3 causally related read-only observations

GATE
READ_ONLY + NO_LOOP

STOP
- target answered
- budget exhausted
- mutation required
- scope change required
- Human authorization required
- causal ambiguity remains
- unexpected production/security state
```

`<=3` is a drift-control default, not a universal hard limit. Override when the task/risk requires another bound.

Executor owns evidence-acquisition HOW and should stop early when the target is answered.

A trivial zero-mutation read/tool mistake does not require a new Architect round-trip.
A failed diagnostic method also does not require a round-trip while Executor can select one materially different bounded method under the same GOAL/ACCEPT/SCOPE/BUDGET/GATE.

Architect review is required when the bounded method-substitution allowance is exhausted or a boundary must change.

---

## R7. Production and Authorization Detail

Use only for consequential execution, reviewed-PR merge authorization, or when authorization state is ambiguous.

Consequential examples:

- production deploy/cutover;
- remote destructive mutation;
- production DB migration/mutation;
- production webhook/job injection;
- credential rotation;
- user-visible delivery;
- hardware/boot-critical/destructive actions.

Core rule: Human approval is a decision event, not executable authority. A Human-reserved consequential action is unlocked only by a later exact `ARCHITECT | EXECUTING_AUTHORIZED` GitHub envelope. A reviewed PR merge is unlocked only by an exact `ARCHITECT | MERGE_AUTHORIZED` envelope bound to the reviewed PR identity.

Execution envelope:

```text
ARCHITECT | EXECUTING_AUTHORIZED
REF: <authorization ref>

ACTION:
<exact consequential action>

TARGET:
<exact target>

IDENTITY:
HEAD/artifact/deployment SHA: <binding where relevant>

MUTATION:
<allowed surface/count>

POLICY:
<e.g. PROD_SINGLE_SHOT>

STOP:
<explicit stop conditions>

AUTH_REF:
<Human authorization reference>
```

Omit fields only when genuinely irrelevant and identity/scope remains unambiguous.

The envelope is action-scoped. It does not authorize:

- another target;
- retry;
- repair-forward;
- alternative transport;
- adjacent mutation;
- rollback unless included;
- package/source/config changes not included.

### Reviewed PR merge authorization

A PR merge does not require Human approval after Architect has completed review, but it is still fail-closed and identity-bound. Required envelope:

```text
ARCHITECT | MERGE_AUTHORIZED
REF: <review/ref>

PR: #N
REVIEWED_HEAD: <exact SHA>
BASE: <base branch>
MERGE_METHOD: <allowed repository merge method>

GATE
- current PR head == REVIEWED_HEAD
- required checks/evidence remain valid
- no unresolved material review
- no material base/mergeability drift

ACTION
Exactly one merge of this PR.

STOP
- PR HEAD changed
- base changed materially
- required check regressed
- merge conflict
- branch protection prevents merge
- repository state is ambiguous
```

This envelope authorizes no source/config changes, retry, repair-forward, alternate merge target, or second merge. One attempted merge consumes the authorization unless authoritative GitHub evidence proves `NOT_ATTEMPTED`; timeout/ambiguity does not restore it.

### Consumption

For `PROD_SINGLE_SHOT`, one attempted consequential action consumes the allowance unless authoritative evidence proves `NOT_ATTEMPTED`.

Timeout/ambiguous result does not restore the allowance.

After return, determine when relevant:

```text
AUTH: CONSUMED
AUTH: UNCONSUMED
AUTH: AMBIGUOUS
```

If no action was attempted and evidence proves that fact, preserve `UNCONSUMED`.
Otherwise assume consumed or ambiguous; never silently reuse.

### Production execution

Prefer:

```text
prepare locally
-> validate locally
-> one authorized production mutation/action
-> one bounded verification
-> stop for Architect review
```

Production is not an iterative debugging environment.

Unexpected production state:

```text
STOP -> BLOCKED -> ARCHITECT_REVIEW
```

Do not authorize autonomous repair-forward unless the exact envelope includes it.

A compliant `BLOCKED` can be correct execution. Review:

- whether STOP was required;
- whether evidence proves the blocker;
- whether the probe was authoritative;
- mutation/action count;
- which later stages were `NOT_RUN`;
- whether rollback was needed/authorized;
- authorization consumption state;
- whether the next contract can remove only the blocker without reopening accepted work.

---

## R8. Contract Examples and Protocol Macros

Use only when an active contract cannot be expressed unambiguously from the compact core schema.

### Macros

```text
READ_ONLY
= no mutation/deploy/config write/secret write/DB write/message send/consequential runtime write

LOCAL_ONLY
= repository/local execution only; no remote/production action

NO_LOOP
= no polling/status/reload/retry loop; only bounded informed retry allowed by Executor policy

PROD_SINGLE_SHOT
= exactly one explicitly authorized production action + one bounded verification + stop on unexpected state
```

Use macros only when relevant. Do not expand their full prose in every Issue.

Example normal contract:

```text
ARCHITECT | READY
REF: SELF

GOAL
Fix failed-spawn PID persistence.

SCOPE
IN: process lifecycle
W: relevant implementation/tests only

ACCEPT
- failed spawn leaves no persisted live PID [UNIT]

GATE
LOCAL_ONLY

RETURN
DONE | BLOCKED
```

Example diagnostic contract:

```text
ARCHITECT | READY
REF: Issue #41

GOAL
Determine whether cache miss latency is reconstruction or provider bound.

SCOPE
SOURCES: existing logs + one local benchmark path

BUDGET
<=3 causally related observations

GATE
READ_ONLY + NO_LOOP

STOP
causal target answered | budget exhausted | mutation required
```

Issue comments after contract creation are delta-only:

```text
ARCHITECT | READY
REF: Issue #41

SCOPE+
Read `src/cache.ts`.
```

Do not repost the full contract.

---

## R9. Anti-Over-Engineering and Execution Budget

Use when scope begins to grow or multiple solution levels are available.

Prefer the smallest sufficient intervention:

```text
no change
-> existing configuration/workflow
-> narrow edit
-> small helper
-> new abstraction/component/subsystem
```

Move downward only when evidence proves the simpler level cannot satisfy ACCEPT.

Detour test:

```text
If this step is skipped or fails, can ACCEPT still pass safely/correctly?
YES -> park/omit now
NO  -> may remain in scope
```

`NO_CHANGE` is valid when current behavior already satisfies ACCEPT.
Minimize semantic change, not line count.

### Validation cost classes

Use the following default classification unless the contract establishes a more appropriate measured project threshold:

```text
FOCUSED    exact triggering repro or directly affected tests
QUICK      expected wall time <=10 minutes
EXPENSIVE  expected wall time >10 minutes; unknown-duration; paid/live/device;
           soak, benchmark, or large end-to-end chain
RELEASE    final production/release evidence; may also be EXPENSIVE
```

Unknown duration is `EXPENSIVE` until measured. Browser use alone does not make a test expensive; observed/estimated cost and consequential boundaries do.

### Contracting expensive evidence

When `EXPENSIVE` or `RELEASE` validation is required, add:

```text
VALIDATION_PLAN
FOCUSED: <exact scope/commands>
QUICK: <exact suite/command>
EXPENSIVE: <exact gate/command or NONE>
TRIGGER: <causal paths/risk/release condition>
EXPECTED_WALL: <range>
MAX_RUNS: <integer per named gate; default 1>
MAX_WALL: <hard bound>
REUSE_IDENTITY: <HEAD/artifact + lock + fixture/config + platform/command identity>
```

`Full validation` is not a test selector. Name the exact suite. A repository-wide suite is allowed only when its causal breadth or a canonical release gate requires it.

Default execution budget:

```text
inspect current state once
-> smallest sufficient delta
-> focused validation while debugging
-> quick causally relevant validation
-> stable final candidate identity
-> one contracted expensive/release gate, if any
-> report
```

The final candidate is stable only when the intended delta is complete, focused/quick gates pass, and no known edit remains. A post-PASS change reruns expensive evidence only when R3 proves causal invalidation.

### Project and CI design

- keep expensive tests behind explicit markers/targets rather than the default developer command;
- split quick always-on CI from conditional expensive/release lanes and run independent lanes in parallel;
- use a deterministic critical-path classifier or explicit release trigger; absence of a trigger must not silently omit causally required evidence;
- record command-level duration and slowest tests; investigate material timing regression rather than normalizing it;
- when a recurring gate exceeds 15 minutes, evaluate checkpoint/resume or stage-level reuse where its added complexity has positive net benefit;
- do not enable test parallelism until shared process, port, cache, global state, filesystem, and fixture isolation are proven;
- do not duplicate equivalent accepted expensive evidence locally and in CI.

Avoid repeated full-suite runs during narrow debugging, repeated production probes, polling/status loops, rerunning accepted expensive evidence without causal invalidation, and repo-wide exploration when the failing surface is known.

`ACCEPT` passing is a stop signal: perform the one contracted risk-proportional final verification, then stop unless ambiguity, contradiction, a concrete defect, or causal evidence invalidation remains.

---

## R10. Skills and Extra Agents

Default:

```text
NO SKILL
NO EXTRA AGENT
```

Use a Skill only when expected net benefit is positive: materially less trial-and-error/context, specialized procedure, lower risk, better reproducibility, or lower total token cost.

Third-party Skills are untrusted until relevant behavior is reviewed.
Do not add planner/reviewer/test/research sub-agents unless evidence shows material benefit.
Skills/agents cannot override safety, authorization, active contract, Git policy, or Human authority.

---

## R11. Lazy-Load Index

Load only when active work materially invokes the listed concern:

```text
R1  adversarial/trust/validation boundary
R2  evidence provenance/identity/derived truth
R3  evidence reuse, invalidation, fix binding
R4  review tier selection/detail
R5  correction delta / review decision detail
R6  uncertain causal diagnosis
R7  production/consequential/merge authorization
R8  contract macro/example ambiguity
R9  scope growth / over-engineering / validation cost / execution budget
R10 skills or extra agents
```

If no listed concern is active, do not load this file.

# Human Reconciliation and Decision Supersession Policy

Load when a Human instruction materially changes objective, scope, priority, constraints, architecture, acceptance, task validity, or when active rules conflict.

```text
RECORD -> CLASSIFY -> CONFLICT-CHECK -> RECONCILE -> DELEGATE
```

1. Append the material Human instruction to journal.
2. Mark reconciliation `in_progress` and bind source event.
3. Classify impact: ADDITIVE | SCOPED | STRUCTURAL | EMERGENCY.
4. Compare against active Human requirements/decisions in intersecting scope.
5. Supersede only rules explicitly contradicted/replaced.
6. Update state/ADR, then invalidate/rebase/cancel only affected tasks.
7. Bump generation for canonical intent changes.
8. Mark `clean` only when current state/decisions/tasks agree.

Journal is not a task log. Worker-originated evidence promoted into a durable Human/decision/reconciliation event is referenced with `source_ref`; worker is never the canonical journal actor.

Authority:

```text
explicit current Human instruction/decision
> active recorded Human requirement/preference
> Prime architectural/project decision
> worker inference/assumption
> historical/superseded assumption
```

A question/suggestion/possibility is not an override. Explicit contradiction supersedes only intersecting scope; compatible additions coexist; ambiguous tension keeps existing rule active and blocks only affected decision scope until evidence or one narrow Human answer resolves it.

Supersession is lossless: hot state keeps current truth/pointers; journal preserves source/supersession links; replaced ADRs become `superseded`; unaffected scopes/tasks remain valid.

# Human Reconciliation and Decision Supersession Policy

Load when a Human instruction materially changes objective, scope, priority, constraints, architecture, acceptance, task validity, or when active rules conflict.

```text
RECORD -> CLASSIFY -> CONFLICT-CHECK -> RECONCILE -> DELEGATE
```

1. Append the material Human instruction to journal with bounded `scopes`; preserve decision-relevant semantics losslessly.
2. Mark reconciliation `in_progress` and bind source event.
3. Classify impact: ADDITIVE | SCOPED | STRUCTURAL | EMERGENCY.
4. Compare against active Human requirements/decisions in intersecting scope. Empty/unknown scope is global for fail-safe impact analysis.
5. Supersede only rules explicitly contradicted/replaced.
6. Bump positive `generation` for canonical intent changes.
7. Update state/ADR; revalidate every task remaining in `state.now` to the new generation. Unaffected tasks only update `validated_at_generation`; semantic contract changes alone bump `contract_rev`; invalidate/rebase/cancel only causally affected work/evidence.
8. Mark `clean` only when state/decisions/tasks agree and every active contract is bound to current generation.


For a durable decision change, propagate in this order:

```text
journal source event + scopes -> generation++ -> ADR create/supersede -> state current pointers/horizon
                              -> revalidate active contracts -> depends_on impact
                              -> affected contract_rev++ only for semantic change
                              -> scoped evidence reuse/invalidation -> reconciliation clean
```

Priority/NEXT-only changes normally update state without ADR/contract/evidence churn. Semantic scope/acceptance/dependency changes revise only affected contracts. A stale contract/result does not by itself stale factual observations or evidence; reuse causally unaffected refs after resolving any identity-sensitive pointers.

Journal is not a task log. Event `summary` must preserve decision-relevant Human meaning; when translation or concise paraphrase risks semantic loss, keep bounded original-language/verbatim detail or a durable `source_ref`. Do not create bilingual duplicates merely for readability. Worker-originated evidence promoted into a durable Human/decision/reconciliation event is referenced with `source_ref`; worker is never the canonical journal actor.

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

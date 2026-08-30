---
status: active
owner: prime
source_decisions: []
updated_at: "<ISO-8601>"
---

# Project Roadmap

Durable multi-phase/milestone ordering only. `.prime/state.yaml` owns current phase/NOW/NEXT; task contracts own worker WHAT; ADRs own WHY. Do not duplicate task status or implementation history here.

Keep this file cold by default. Prime resolves `state.roadmap_ref` only before selecting NEXT when the hot horizon is empty/insufficient, changing phase/milestone, declaring COMPLETE, editing durable milestone ordering, or reconciling a Human change that affects that ordering.

## Milestones
1. <durable milestone/outcome>
2. <durable milestone/outcome>

## Durable ordering/constraints
- <only what would be costly to forget>

## Change discipline
Material roadmap changes should reference the Human requirement/ADR that caused them. Update state horizon and only causally affected contracts/evidence. Keep this file compact; no task diary.

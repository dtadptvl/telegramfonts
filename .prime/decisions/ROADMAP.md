---
status: active
owner: prime
source_decisions: [decisions/ADR-0001.md, decisions/ADR-0002.md, decisions/ADR-0003.md]
updated_at: "2026-08-30T06:20:15Z"
---

# Project Roadmap

Durable multi-phase ordering only. `.prime/state.yaml` owns current phase/NOW/NEXT; task contracts own worker WHAT; ADRs own WHY. No task status or implementation history here.

## Milestones
1. Implement and locally accept FAST_30 sole-profile retirement (T-FAST30-01; LOCAL_ONLY; PROFILE_RETIRED fail-closed; 30-minute wall with FAST30_FAILED halt).
2. Exact read-only inspection and reconciliation of the retired A23 job/order (job_477b205f / ord_2a142f39; no mutation; terminal disposition currently unproven).
3. Separately authorized A23 deployment and one controlled bot fulfillment under the 30-minute limit (own identity-bound consequential/production contract; exact Human authorization required).
4. Later authorized migration of runtime/archive responsibility to the Debian mini-PC (external ext4 archive target; own consequential contract).

## Durable ordering/constraints
- Milestone 3 requires milestones 1-2 complete and its own exact Human authorization envelope.
- Milestone 4 only after milestone 3 proves the controlled fulfillment on A23.
- No fidelity fallback/escalation is ever reintroduced (ADR-0001); milestone work must not weaken held-out/four-consumer/TTF+OTF invariants.

## Change discipline
Material roadmap changes reference the Human requirement/ADR that caused them. Update state horizon and only causally affected contracts/evidence. No task diary.
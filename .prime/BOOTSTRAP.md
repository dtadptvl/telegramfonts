# .prime/BOOTSTRAP.md - project-specific recovery/setup context (off-machine recovery authorization)

Off-machine recovery binding (Prime-governed; does not restate PRIME.md or product roadmap):

- recovery_remote: origin
- recovery_ref: refs/heads/prime-recovery
- push_mode: non-force only; never force-push or overwrite divergent recovery history
- push_timing: only at durable milestone, expected session/workspace handoff, or before a materially long/consequential operation
- pre_push: commit durable local state first; verify remote/ref identity immediately before push
- scope: recovery durability only; recovery push grants no merge/deploy/production authority
- hygiene: never create routine Issues/PRs from recovery pushes
- if recovery_ref diverges (not ancestor-compatible fast-forward): STOP and reconcile with Human; never force
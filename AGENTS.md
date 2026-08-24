# Repository agent guidance

This file is role-neutral. `ARCHITECT.md` and `EXECUTOR.md` remain the
role-specific policies; the active GitHub contract remains authoritative.

- Preserve the existing Desktop trigger, GitHub recovery pointer, and
  `ORCH|v1` footer path.
- `.orchestra/` is a deterministic transport boundary only: it may invoke,
  validate, route, deduplicate, bound, and stop structured events. It must not
  decide PASS, edit contracts or product code, authorize, merge, deploy, or
  repair.
- Keep Architect invocations read-only and Executor edits inside the active
  scoped workspace. Never touch `.git` from a model process.
- Use the Issue #55 model/sandbox contract exactly when the local Codex CLI
  accepts it: Architect `gpt-5.6-sol`/high/read-only; Executor
  `gpt-5.6-luna`/max/workspace-write. Do not silently fall back.
- No production, A23, payment, webhook, runtime-config, secret, or unrelated
  product action is in scope.
- Never print, persist, or commit credentials or raw model transcripts.

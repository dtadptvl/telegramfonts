# Repository agent guidance

This file is role-neutral. `ARCHITECT.md` and `EXECUTOR.md` remain the
role-specific policies; the active GitHub contract remains authoritative.

- Preserve the existing Desktop trigger, GitHub recovery pointer, and
  `ORCH|v1` footer path.
- `.orchestra/` is a deterministic transport boundary only: it may invoke,
  validate, route, deduplicate, bound, and stop structured events. It must not
  decide PASS, edit contracts or product code, authorize, merge, deploy, or
  repair.
- The sole Architect is ChatGPT conversation `architect`. GitHub is the
  durable contract/evidence/event bus; Scheduled Task is wake-up only. The
  historical Desktop dual-role/Codex path is ARCHIVE, not canonical or usable,
  and there is no Architect machine JSON/local Codex Architect path.
- The active GitHub/local event path is Executor-only and must use Luna
  `gpt-5.6-luna`/max/workspace-write. It must never invoke a second model or
  agent. The Issue #55 dual-Codex runner remains historical regression
  evidence only; it is not an active dispatch path.
- Before Luna, the host must require exactly one open `orchestra:execute`
  Issue, recover the latest canonical Architect READY/FIX_REQUIRED ref and
  current main/PR HEAD, derive the event key, and check both the GitHub marker
  and local lock. After one validated result, the host owns stage/commit/push,
  one PR create/update, sanitized Issue reporting, and
  `orchestra:execute -> orchestra:review`; it never routes directly to Human.
- The only active protocol labels are exactly `orchestra:execute`,
  `orchestra:review`, `orchestra:human`, and `orchestra:done`.
- Keep Executor edits inside the active scoped workspace. Never touch `.git`
  from a model process.
- If the historical Issue #55 runner is explicitly exercised, retain its
  recorded read-only Architect and Luna role configuration exactly; do not
  silently alter or reuse it for the active event path.
- No production, A23, payment, webhook, runtime-config, secret, or unrelated
  product action is in scope.
- Never print, persist, or commit credentials or raw model transcripts.
- Classify `runner.py`, the Architect schema, the Issue #55 fixture, and their
  tests as ARCHIVE; reuse only their safety concepts in the active path.

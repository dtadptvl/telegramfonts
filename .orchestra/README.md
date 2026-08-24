# Codex-native bootstrap

`.orchestra/runner.py` is a finite transport boundary for one active
Architect contract. It invokes the two independent Codex roles, validates
their structured final JSON, checks scoped workspace effects, routes only
the allowed transitions, and stops on gates, duplicates, stale references,
invalid output, timeout, or budget exhaustion.

It does not edit contracts or product code, decide PASS, invent evidence,
authorize, merge, deploy, or retry. The existing Desktop trigger and
`ORCH|v1` footer remain canonical and usable.

The validated host contract is the sole executable contract authority. Architect
JSON contains only state/ref/head and a bounded review decision/delta; it cannot
echo or replace the contract. The runner passes the unchanged host contract to
Executor and passes only the Architect review delta for a correction.

The bounded local smoke shape is:

```text
Architect READY (Sol / high / read-only)
→ Executor report (Luna / max / workspace-write)
→ Architect review
→ MERGE_READY or an honest terminal stop
```

Run focused protocol tests with:

```text
python .orchestra/tests/test_runner.py
```

The real smoke is host-controlled and uses an isolated reversible workspace;
raw model transcripts are not retained by the repository.

# Executor event handoff

The active Issue #57 path is a bounded Executor-only transport:

```text
GitHub issues:labeled event (orchestra:execute)
    or the equivalent Desktop/local event
→ host validates repository, issue, label, canonical ref, and live heads
→ GitHub marker check + local atomic claim
→ one Luna Executor call (max / workspace-write)
→ validate the structured result and workspace boundary
→ record one terminal outcome and stop
```

Run the launcher with a native GitHub event:

```text
python .orchestra/executor_launcher.py \
  --github-event "$GITHUB_EVENT_PATH" \
  --workspace .
```

For a Desktop/local trigger, pass a compact `orchestra/event/v1` JSON file with
`--local-event`. The event is a pointer only; the host recovers the canonical
Issue contract and the Executor receives only the validated pointer. The
ledger is outside the checkout by default so event bookkeeping does not become
a model change. The historical Desktop dual-role runner is not a usable active
dispatch path.

Before Luna, the host queries GitHub and requires exactly one open
`orchestra:execute` Issue. It recovers the newest canonical Architect
`READY`/`FIX_REQUIRED` ref plus the current `main` and PR heads, derives the
event key from those values, checks the GitHub event marker, and then claims the
same key in the local SQLite ledger. Ambiguity or stale refs/heads stop before
Luna.

After one validated Luna result, the host stages only reported workspace
changes, commits and pushes them, creates or updates one PR when needed, posts
one sanitized report/marker, and changes `orchestra:execute` to
`orchestra:review`. It never routes directly to Human; the ChatGPT Architect
owns that decision. Duplicate events are terminal `NO_CHANGE` results, an
in-flight event is a stop, and a correction event must name a completed parent.
At most one correction is admitted per event chain; there is no automatic
retry or polling loop. `BLOCKED`, `READY_HUMAN_AUTH`, and `SECURITY_BLOCKED`
remain stop gates reported to Architect review.

Run focused tests with:

```text
python .orchestra/tests/test_executor_handoff.py
python .orchestra/tests/test_runner.py
```

The workflow is intentionally restricted to one global lane on
`[self-hosted, windows, x64, orchestra]` with the least write permissions
needed for contents, issues, and pull requests. `orchestra.cmd execute` may be
run without an event argument for manual discovery of the sole canonical
Issue; `--github-event` is the Actions payload option. The root launcher picks
a verified Python 3.10+ runtime or stops with `python_unavailable`.

`runner.py`, `schema/architect.schema.json`, and `fixtures/issue55-smoke.json`
plus their Issue #55 tests are `ARCHIVE` only. They preserve historical safety
concepts and regression evidence; they are not canonical or usable dispatch
paths. The active path is the host-owned GitHub handoff in
`executor_handoff.py`/`executor_launcher.py`.

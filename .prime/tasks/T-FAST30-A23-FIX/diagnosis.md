# T-FAST30-A23-FIX — RB diagnosis verdict (2026-08-30T~11:05Z, gen 9)

Source: RB diagnosis worker (read-only); contract rev 1; journal E-00020.

## Root cause (three compounding, proven gaps)
- RC-1 PROVEN: deployed device lineage b8c6994 (PR #94 merge) PREDATES FAST_30 commit b385ab1; it contains zero wall enforcement (BALANCED_MAX + one-shot FULL_MAX escalation, unbounded). Attempt 6 (leased 2026-08-29T19:46Z) runs under that lineage.
- RC-2 PROVEN: FAST_30 wall in origin/main is gate-scoped per-font and cooperative (deadline born at Stage9DReleaseGate entry; checkpoints only). Acquisition, browser supplemental measurement, upload/complete, queue dwell are outside any deadline; no job-level claim->complete wall in runner.process_message.
- RC-3 PROVEN: nothing terminates a heartbeating job: agent heartbeat daemon thread extends lease for the whole run (Issue #90 design; runner.py:223-267); edge heartbeat extends unconditionally with no age cap (job-service.ts:1435-1473); cron finalizer needs lease_expires_at<=now AND attempts exhausted (job-service.ts:345) - starved while heartbeats land; supervisor restarts only on process exit (no hang watchdog).
- Zombie verdict: process continuously alive since 2026-08-29T19:46Z PROVEN (lease token e3fd2050 + attempt 6/6 continuity across P0/P6/RA reads); compute stalled INFERRED (no healthy path explains 15h; worst-case browser measurement ~5,300 sequential CDP calls x 10s cap ~= 14.7h is the only matching scale, outside any wall).
- Device surface: Tailscale node pings; Termux sshd 8022 refuses (dead, no listener); adb absent. sshd death consistent with LMK under memory pressure (orphan Chromium suspect). Reboot excluded by lease continuity.

## Timing evidence (reusable)
- A23 cold full style (Be Vietnam Pro, 481 glyphs, serialized): 1629.34s = 27.2 min total; mean 3387.4 ms/glyph (ops/a23_proof.log, ops/max_physical_a23_proof_report.json; CAPACITY_MODEL measured_full_style_cold_seconds 1629.34).
- Dominant cost: per-glyph SDF-driven reconstruction/optimization (solver+fit); optimizer stages dominate perf reports.
- Healthy cold baseline consumes ~90% of the 30-min wall BEFORE acquisition/measurement/upload -> <=25-min target must attack per-glyph cost and/or parallelism (A23 8 cores; proof run serialized; profile_workers default 4).
- Timing delta: promised <=30 min vs observed >=15h (>=30x).

## Fix plan (all configuration-driven; no A23 hardcoding; gates/invariants unchanged)
- F1 runner.py process_message: job-level monotonic deadline claim->ACK (JOB_WALL_SECONDS config, default 1800); independent of heartbeat-moved expiry_holder; wraps acquisition block; replaces lease-margin guards (764/847/859); clamps gate wall to remaining budget; FAST30_FAILED terminal on breach; stop heartbeat before failing.
- F2 release_gate.py execute_sync/execute_with_model: preemptive boundary - future.result(timeout=remaining_budget) -> FAST30_FAILED WALL_LIMIT_EXCEEDED.
- F3 acquisition/raster_ingest.py collect_browser_measurement: aggregate deadline parameter around per-codepoint/per-size CDP loops; breach -> terminal FAST30_FAILED mapping; per-call timeouts unchanged.
- F4 edge job-service.ts: config-driven age backstop - refuse heartbeat extensions once now - leased_at > MAX_JOB_AGE_MS (wrangler var; wall + margin, e.g. 35 min) returning EXPIRED_OR_FENCED; finalizer age clause optional complement. Deploys zombie-termination via control plane: refused heartbeat -> lease expires <=5min -> cron finalizer sets job FAILED (max_attempts_exhausted) + order FAILED; no D1 mutation needed.
- F5 scripts/debian_worker_supervisor.sh: hang watchdog - worker touches progress file on stage transitions/heartbeat beats; supervisor kills on stale progress beyond N x heartbeat interval (config); existing restart budget applies.
- F6 checkpoint persistence: locate GlyphCheckpointStore under stable identity (job_id + snapshot/coverage identity) in durable cache dir (not scratch/<job>_<lease_token>) so re-claimed attempts resume; identity-hash validation already fail-closed.

## Device-dependent remainder (blocked on surface)
- Zombie stop: graceful stop NOT authorized (attribution unavailable; sshd dead). Zombie dies via F4 edge deploy or natural death; system then self-finalizes (no D1 mutation).
- Final acceptance: one production-equivalent one-style run on device <=25 min (isolated scratch/cache namespace) - needs surface.
- Deployment + recovery per T-A23-PROD-02 (candidate SHA freeze -> origin/main containment -> governance sync -> A23 release -> conditional CAS recovery).

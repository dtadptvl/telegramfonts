# Legacy import archive (cold reference; migrated from v7 state.yaml 2026-08-30)

Decision-relevant legacy context from the Architect/Executor era. Source records remain authoritative:
GitHub #90, #6 (D18/D21 supersession comment 5466282425), #3, #7.

## Source refs
- GitHub #90: BALANCED_MAX runtime-to-bot contract; stale/incompatible at generation 2 (FAST_30_ONLY).
- GitHub #6: D18/D21 profile escalation semantics retired; D21 runtime truth preserved in ADR-0001 consequences.
- GitHub #3/#7: old AI-PLAN/AI-CHECKPOINT; retired governance memory.

## Preserved runtime truth (verified during #90 cycle)
- A23 archive mode no_local_archive_v1 explicit; external ext4 off A23; mini-PC is future archive target.
- chroot runtime: /proc + /dev/{urandom,random,zero} binds + tmpfs /dev/shm; boot-recovery script sha256 631d2920; Chromium CDP proven.
- worker heartbeat on dedicated daemon thread; terminal-FAILED recovery CAS incl. reaper-terminal residue.
- edge version 7c582378; device staged release b8c6994; CI lanes quick-tests/fullmax-final exist.

## Stable identities
- canonical line main@02dfe557 pre-cutover; merged PRs #85/#87/#89/#91/#92/#93/#94/#95.
- paid order ord_2a142f39c5d2443492d1d8410d18bf9f / payment tx 77506129 (unfulfilled; reconciliation unresolved).
- stale retired-profile job job_477b205f4aff41cb89c2dd005ecf8009 (attempt 6; terminal disposition unproven).
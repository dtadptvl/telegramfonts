# TelegramFonts A23 Worker Operator & Production Readiness Guide

This operational manual documents the production readiness surface, preflight diagnostics, deployment manifest integrity checks, deterministic soak harness execution, and execution authorization guards for the **TelegramFonts A23 Autonomous Compute Node**.

---

## 1. System Architecture & Tiered Execution Model

The A23 Worker executes font conversion and reconstruction tasks received via Cloudflare Queues with a strict fail-closed contract across four reuse tiers:

`
Claim Queue Job (Fenced Lease)
       |
  [Tier L1: FinalFontArchive] -------- (Hit) -> Fast-path Package & Deliver
       | (Miss)
  [Tier L2: CanonicalFontModelCache] -- (Hit) -> Build & Consumer Gate (Skip Reconstruction)
       | (Miss)
  [Tier L3: AuthorizedBinaryCache] --- (Hit) -> Verify Sfnt Continuity & Package
       | (Miss)
  [Tier L4: Dump-DOM / Browser Acquisition]
       |--> Dump-DOM Binary Hit ------> Verify Binary -> Store L3 -> Package
       |--> Monotype CDN / Raster ----> Ingest MAX Observations -> Stage 9D Solver -> Store L2 -> Package
       |
  [Vietnamese Extension]
       |--> Preserved (Zero AI) -------> Keep Original Glyphs
       |--> Missing (AI Extension) ----> Fixed 12B/27B/Arbiter Routing -> Strict Validation
       |
  [Four-Consumer Release Gate]
       FontTools + FreeType + HarfBuzz + Chromium Headless (Zero Phase/Metric Drift)
       |
  [Atomic Package & Delivery]
       Stream ZIP to R2 -> Complete Job in D1 -> ACK Queue Message
`

---

## 2. Preflight Diagnostics (scripts/a23_preflight.py)

The preflight utility verifies that all local environment, database schema, native dependency, and configuration requirements are satisfied before starting a worker process.

### Running Preflight Checks

`ash
# Standard interactive check
python scripts/a23_preflight.py

# Strict mode (exits with non-zero code on warnings or failures)
python scripts/a23_preflight.py --strict

# Output structured JSON report
python scripts/a23_preflight.py --json --output ops/preflight_report.json
`

### Preflight Check Categories
1. **Configuration Boundaries**:
   - PULL_BATCH_SIZE: Bounded in 1..10.
   - VISIBILITY_TIMEOUT_MS: Bounded in 10s..30m.
   - LEASE_DURATION_SECONDS: Bounded in 30s..600s.
   - Heartbeat Safety Margin: Worker heartbeat occurs at least 15s before lease expiration.
2. **Native & Python Dependencies**:
   - ontTools, reetype-py, uharfbuzz, PIL (Pillow), pydantic, httpx, sqlite3.
3. **Database Schemas & Idempotent Migrations**:
   - ObservationStore: source_collections, observations, pair_observations, eature_observations.
   - CanonicalFontModelCache: canonical_font_models.
   - AuthorizedBinaryCache: uthorized_binaries.
   - FinalFontArchive: inal_fonts.
4. **Browser Runtime**:
   - Locates local Chromium executable for direct headless measurement and lossless raster capture.
5. **Filesystem Boundaries**:
   - Ensures SCRATCH_DIR and FONT_ARCHIVE_ROOT are writable and on isolated mounts.

---

## 3. Reproducible Deployment Manifest (scripts/generate_deployment_manifest.py)

The deployment manifest binds the exact Git commit SHA, Python runtime version, dependency versions, SQLite schema hashes, ObservationConfig canonical hash, and normalized SHA-256 digests of all 48 core source files into a signed manifest.

### Generating the Manifest

`ash
python scripts/generate_deployment_manifest.py --output ops/a23_deployment_manifest.json
`

### Verifying Manifest Integrity & Detecting Code Drift

`ash
python scripts/generate_deployment_manifest.py --verify ops/a23_deployment_manifest.json
`

If any core file, schema definition, or configuration parameter is modified, verification fails closed and reports the exact drift reasons:
- SCHEMA_DRIFT_<TABLE>: Database schema altered without migration record.
- FILE_DRIFT_<PATH>: Core implementation modified.
- MANIFEST_SIGNATURE_TAMPERED: Manifest payload altered or forged.

---

## 4. Deterministic Soak Test Harness (scripts/a23_soak_runner.py)

The soak harness runs 100+ end-to-end simulated production jobs across seven distinct operational scenarios to verify determinism, concurrency safety, scratch pruning, and lease fencing.

### Running the Soak Harness

`ash
# Run 100 deterministic jobs with default seed (42)
python scripts/a23_soak_runner.py --jobs 100 --seed 42

# Export execution trace report
python scripts/a23_soak_runner.py --jobs 100 --seed 42 --output ops/soak_results.json
`

### Soak Invariants Verified
- **Zero Duplicate Completions**: Replay/duplicate delivery results in no duplicate D1 completion.
- **Zero Partial Publishes**: A failed or retried job never uploads an artifact or archives a font.
- **Zero Orphan Scratch Directories**: Unclaimed or aborted job staging directories are pruned automatically.
- **Deterministic Trace Hash**: Rerunning the soak harness with the same seed produces an identical SHA-256 trace hash.

---

## 5. Physical Proof Authorization Guard (scripts/run_physical_a23_proof.py)

Physical proof scripts that perform compute-intensive solver execution or produce authoritative baseline measurements require explicit Architect execution authorization.

### Invocation Contract

`ash
# Via CLI flag
python scripts/run_physical_a23_proof.py --browser-version chromium_130 --config-hash <hash> --auth-token ARCHITECT_EXECUTING_AUTHORIZED_<hex>

# Or via Environment Variable
export A23_PHYSICAL_PROOF_AUTH_TOKEN=ARCHITECT_EXECUTING_AUTHORIZED_<hex>
python scripts/run_physical_a23_proof.py --browser-version chromium_130 --config-hash <hash>
`

Without an authorized token matching the ARCHITECT_EXECUTING_AUTHORIZED_ prefix and at least 16 hex characters, the runner fails closed immediately with PermissionError: UNAUTHORIZED_EXECUTION_BLOCKED before opening databases or performing network requests.

---

## 6. Operational Failure Classifications

| Reason Code / Error | Classification | Remediation |
|---|---|---|
| NO_OBSERVABLE_BROWSER_FONT_FACES | Source acquisition miss | Verify target URL and network access. |
| STAGE9D_GATE_FAILED | Consumer release gate rejected candidate | Check fidelity tolerances or raster observations. |
| VI_GATE_FAILED | Vietnamese AI candidate failed contour validation | AI generated invalid geometry; fallback triggered. |
| ACQUISITION_BINARY_INTEGRITY_FAILED | Downloaded font container corrupt | Corrupt font container rejected; fail-closed. |
| LEASE_FENCED_OR_EXPIRED | Worker lost lease heartbeat | Worker stalled; job retried by another node safely. |
| MALFORMED_CLAIM_PAYLOAD | Invalid job envelope parameters | Edge worker emitted unexpected parameter schema. |

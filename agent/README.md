# Agent Service (Python A23 Compute)

Private compute worker for TelegramFonts that consumes fulfillment jobs via Cloudflare Queues HTTP pull and performs deterministic font reconstruction.

## Architecture Context
- **Outbound Network Only**: Acts as an external HTTP-pull consumer of Cloudflare Queues (`/pull`, `/ack`). Requires no inbound public ports, webhooks, tunnels, or open listeners.
- **Fenced Lease Protocol**: Communicates with Cloudflare Worker `/internal/jobs/{job_id}/claim`, `/heartbeat`, and `/fail` using `A23_NODE_SECRET` Bearer auth.
- **Deterministic Compute**: Uses FontTools to generate TrueType (`TTF`) and OpenType (`OTF`) fonts from observable source data / fixture inputs, validating all table structures and glyph sets.
- **Deterministic Staging & Streaming**: Packages generated font binaries into a reproducible ZIP bundle with normalized timestamps, writes `manifest.json`, and streams chunks directly to `PUT /internal/jobs/{job_id}/artifact`.
- **Durable Final-Font Archive (D21 safe mode)**: When `FONT_ARCHIVE_ROOT` is configured, validated TTF/OTF binaries are atomically retained on the external archive while the SQLite index remains under `SCRATCH_DIR`; verified repeat hits skip MAX reconstruction and validation. Archive mode is explicit, versioned, and fail-closed via `FONT_ARCHIVE_MODE`: `external_ext4_archive_v1` (canonical external ext4 archive, mini-PC production target) or `no_local_archive_v1` (external archive cannot be attached: delivery unchanged, local L1 reuse disabled, repeat orders recompute). Mode truth is observable in the startup log, readiness report, per-job reuse trace, and supervisor log.
- **Durable Completion & Final ACK**: Executes atomic D1 completion via `POST /internal/jobs/{job_id}/complete` and acknowledges the Cloudflare Queue message strictly after durable completion.
- **Graceful Lifecycle**: Handles `SIGINT` / `SIGTERM` signals for clean worker drainage without corrupting in-flight font jobs.

## Configuration
All configuration is loaded from environment variables (or `.env`):
- `CF_ACCOUNT_ID`: Cloudflare account ID
- `CF_QUEUE_ID`: Cloudflare Queue Resource UUID (retrieved via `wrangler queues list`; required by REST API path)
- `CF_QUEUES_TOKEN`: Scoped API token with `Queues:Read` and `Queues:Write` permissions
- `EDGE_BASE_URL`: Base URL of Cloudflare Worker Edge (e.g. `https://telefont.example.com`)
- `A23_NODE_SECRET`: Internal node bearer authentication secret
- `A23_WORKER_ID`: Unique worker identifier (e.g. `a23-node-primary-01`)
- `SCRATCH_DIR`: Non-canonical temporary scratch root (default `./scratch`)
- `FONT_ARCHIVE_ROOT`: External ext4 archive root for immutable validated TTF/OTF outputs (optional until production authorization)
- `FONT_ARCHIVE_MODE`: D21 safe archive mode (default `AUTO`). `AUTO` resolves to `external_ext4_archive_v1` when `FONT_ARCHIVE_ROOT` is configured, otherwise to `no_local_archive_v1`. `EXTERNAL_EXT4` requires `FONT_ARCHIVE_ROOT`; `NO_LOCAL_ARCHIVE` rejects it. Unknown modes and contradictions fail closed.
- `PULL_BATCH_SIZE`: Messages per pull request (default `1`, max `10`)
- `VISIBILITY_TIMEOUT_MS`: Visibility timeout in milliseconds (default `300000` = 5 min)
- `HEARTBEAT_INTERVAL_SECONDS`: Lease heartbeat cadence (default `60`s)
- `LEASE_DURATION_SECONDS`: Initial lease duration (default `300`s)

## Testing & Benchmarking

### Run Test Suite
```bash
pytest agent/tests
```

### Run Capacity Benchmark Harness
```bash
python agent/src/benchmark.py --samples 10 --json-out ops/benchmark_report.json
```

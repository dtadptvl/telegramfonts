# Agent Service (Python A23 Compute)

Private compute worker for TelegramFonts that consumes fulfillment jobs via Cloudflare Queues HTTP pull and performs deterministic font reconstruction.

## Architecture Context
- **Outbound Network Only**: Acts as an external HTTP-pull consumer of Cloudflare Queues (`/pull`, `/ack`). Requires no inbound public ports, webhooks, tunnels, or open listeners.
- **Fenced Lease Protocol**: Communicates with Cloudflare Worker `/internal/jobs/{job_id}/claim`, `/heartbeat`, and `/fail` using `A23_NODE_SECRET` Bearer auth.
- **Deterministic Compute**: Uses FontTools to generate TrueType (`TTF`), OpenType (`OTF`), and `WOFF2` fonts from public-preview / fixture inputs, validating all table structures and glyph sets.
- **Deterministic Staging**: Packages generated font binaries into a reproducible ZIP bundle with normalized timestamps and writes `manifest.json`.
- **Success Boundary**: Stops at `HOLD_FOR_COMPLETION` on successful local compute without acknowledging the Queue message before Phase 6 handles durable R2 storage and completion.

## Configuration
All configuration is loaded from environment variables (or `.env`):
- `CF_ACCOUNT_ID`: Cloudflare account ID
- `CF_QUEUE_ID`: Cloudflare Queue ID/name
- `CF_QUEUES_TOKEN`: Scoped API token with `Queues:Read` and `Queues:Write` permissions
- `EDGE_BASE_URL`: Base URL of Cloudflare Worker Edge (e.g. `http://localhost:8787`)
- `A23_NODE_SECRET`: Internal node bearer authentication secret
- `A23_WORKER_ID`: Unique worker identifier
- `SCRATCH_DIR`: Non-canonical temporary scratch root (default `./scratch`)
- `PULL_BATCH_SIZE`: Messages per pull request (default `1`, max `10`)
- `VISIBILITY_TIMEOUT_MS`: Visibility timeout in milliseconds (default `300000` = 5 min)
- `HEARTBEAT_INTERVAL_SECONDS`: Lease heartbeat cadence (default `60`s)
- `LEASE_DURATION_SECONDS`: Initial lease duration (default `300`s)

## Testing
Run unit and integration test suite:
```bash
pytest agent/tests
```

# telegramfonts

Hybrid cloud architecture for automated Vietnamese font processing and fulfillment via Telegram bot.

## Architecture & Boundary

```
[ Telegram Client / SePay ] ──(Inbound Webhook)──> [ Cloudflare Worker (edge/) ]
                                                           │
                                                           ├── D1 (Canonical State & Fencing)
                                                           ├── R2 (Private Artifact Storage)
                                                           └── Queue (Fulfillment Transport)
                                                                   │
                                                                   ▼ (Outbound HTTP-Pull / Fenced Lease)
                                                       [ Python A23 Agent (agent/) ]
                                                       (Private Compute - No Inbound Ports)
```

- **Control Plane (`edge/`)**: TypeScript Cloudflare Worker terminating Telegram and SePay webhooks, managing canonical durable state via Cloudflare D1, dispatching transactional outbox events to Cloudflare Queues, exposing fenced internal job control APIs, verifying versioned HMAC-SHA256 download links, and streaming verified ZIP bundles from private R2.
- **Compute Plane (`agent/`)**: Python A23 compute worker pulling jobs via Cloudflare Queue HTTP-pull consumer API, claiming fenced D1 leases, reconstructing TrueType/OpenType binaries with FontTools, packaging deterministic ZIPs, and streaming artifacts to private R2 without opening public inbound ports.
- **Boundary Note**: Production Cloudflare resources, deployment bindings, and live secrets are strictly operated by human operators per `ops/RUNBOOK.md`. Local tests run against simulated bindings (Miniflare/D1 local SQLite/R2).

## Repository Layout

```
.
├── .github/
│   └── workflows/
│       ├── ci.yml               # Continuous Integration (install, typecheck, preflight, test)
│       └── executor-issue-label.yml # Host-owned orchestra:execute handoff
├── orchestra.cmd                 # Safe Windows launcher fallback
├── agent/                       # Python A23 private compute worker
│   ├── src/                     # Runner, Queue client, Worker client, compute pipeline
│   │   ├── compute/             # FontBuilder, Source acquisition, Validator, Packager
│   │   ├── benchmark.py         # Capacity benchmark CLI & latency modeling harness
│   │   ├── config.py            # Environment settings
│   │   ├── logging_utils.py     # Sanitized structured logging
│   │   ├── main.py              # Entrypoint and lifecycle manager
│   │   ├── queue_client.py      # Cloudflare Queues HTTP pull client
│   │   ├── runner.py            # Runner state machine and lease heartbeat loop
│   │   ├── scratch.py           # Scratch directory manager & path traversal guards
│   │   └── worker_client.py     # Internal job control client
│   ├── tests/                   # Pytest test suite
│   ├── pyproject.toml
│   ├── requirements.txt
│   └── README.md
├── edge/                        # Cloudflare Worker control-plane
│   ├── migrations/              # D1 database migrations (0001–0005)
│   │   ├── 0001_initial_schema.sql
│   │   ├── 0002_telegram_sessions_and_catalog.sql
│   │   ├── 0003_payment_code_and_sepay.sql
│   │   ├── 0004_outbox_dispatch_and_job_lease.sql
│   │   └── 0005_r2_artifacts_and_delivery.sql
│   ├── src/                     # Worker TypeScript source
│   │   ├── handlers/            # Telegram, SePay, Internal Jobs, and Downloads handlers
│   │   ├── services/            # Telegram, catalog, session, order, payment, outbox, and job services
│   │   ├── utils/               # URL normalization, HTML escaping, VietQR, download signing, preflight
│   │   ├── env.ts               # Typed environment & bindings
│   │   └── index.ts             # Entrypoint, routing & scheduled dispatcher
│   ├── test/                    # Automated tests (Vitest + Cloudflare Workers Pool)
│   ├── package.json
│   ├── tsconfig.json
│   ├── vitest.config.ts
│   └── wrangler.jsonc           # Wrangler Worker configuration
├── ops/                         # Production Readiness, Runbooks, and Capacity Models
│   ├── CAPACITY_MODEL.md        # Mathematical dimensioning for 500 & 1000 jobs/day
│   ├── OBSERVABILITY.md         # Sanitized structured log schemas & telemetry signals
│   ├── PRODUCTION_READINESS.md  # REPO_PASS vs WAIT_HUMAN_RUNTIME checklist
│   ├── RUNBOOK.md               # Step-by-step operator launch, verification & rollback runbook
│   └── SECURITY.md              # Secret separation matrix & rotation procedures
├── scripts/
│   └── preflight.mjs            # Deterministic configuration validator CLI
├── package.json                 # Root monorepo workspace configuration
├── package-lock.json
└── README.md
```

## Cloudflare Queue HTTP-Pull Consumer & Lease Fencing

Per Decision #6 D05:
1. **No Queue Consumer Worker**: In Cloudflare Workers, no Queue push consumer Worker is deployed. A23 acts as an external HTTP-pull consumer.
2. **Operational Enablement**: HTTP pull is enabled operationally via Cloudflare Dashboard or CLI (`npx wrangler queues consumer http add <QUEUE-NAME>`), not via `wrangler.jsonc`. It requires a scoped Cloudflare API token with `Queues:Read` and `Queues:Write` permissions.
3. **Visibility Timeout & Fencing**: When pulling messages from Cloudflare Queue, A23 explicitly requests a visibility timeout (e.g. 5 minutes) matching or exceeding lease duration (`A23_JOB_LEASE_SECONDS`).
4. **D1 Lease Fencing**: Cloudflare Queues provide at-least-once delivery. D1 lease fencing with cryptographically random `lease_token`s and atomic batch validation is authoritative: duplicate redeliveries or competing workers cannot corrupt state or double-process jobs.

## Getting Started

### Prerequisites
- Node.js 20+
- npm 10+
- Python 3.11+

### Installation
From repository root:
```bash
npm ci
pip install -r agent/requirements-lock.txt
```

### Local Development & Verification Commands

1. **Release Preflight Check**:
   ```bash
   npm run preflight
   ```

2. **Typecheck**:
   ```bash
   npm run typecheck
   ```

3. **Run Automated Test Suites**:
   ```bash
   npm test
   pytest agent/tests
   ```

4. **Run Capacity Benchmark**:
   ```bash
   python agent/src/benchmark.py --samples 10
   ```

5. **Apply D1 Migrations Locally**:
   ```bash
   npx --workspace=edge wrangler d1 migrations apply DB --local
   ```

6. **Start Local Development Worker**:
   ```bash
   npm run --workspace=edge dev
   ```

### Worker Endpoints

- `GET /health` -> Liveness check returning `200 {"status": "ok"}`.
- `GET /ready` -> Readiness check querying D1 database schema returning `200 {"status": "ready", "database": "connected"}` or `503` if unavailable.
- `POST /webhooks/telegram` -> Telegram Bot webhook endpoint (requires `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and `X-Telegram-Bot-Api-Secret-Token` header).
- `POST /webhooks/sepay` -> SePay payment webhook endpoint (requires `SEPAY_WEBHOOK_SECRET`, HMAC-SHA256 signature verification, and timestamp within 300s window).
- `POST /internal/jobs/:job_id/claim` -> Internal A23 node job claiming endpoint with D1 lease fencing (requires `A23_NODE_SECRET` Bearer token).
- `POST /internal/jobs/:job_id/heartbeat` -> Internal A23 node lease extension endpoint (requires `A23_NODE_SECRET` Bearer token).
- `POST /internal/jobs/:job_id/fail` -> Internal A23 node failure / retry transition endpoint (requires `A23_NODE_SECRET` Bearer token).
- `PUT /internal/jobs/:job_id/artifact` -> Streams verified font ZIP bundle directly into private R2 bucket (requires `A23_NODE_SECRET`, `X-Worker-Id`, `X-Lease-Token`, and `X-Artifact-SHA256`).
- `POST /internal/jobs/:job_id/complete` -> Atomic D1 completion guard creating receipt, setting COMPLETED status, and enqueueing exactly one `DELIVERY_READY` outbox event.
- `GET /downloads/:order_id` -> Verifies versioned HMAC-SHA256 URL and streams private ZIP artifact directly from R2.
- All other routes return `404 Not Found`.

## Canonical States

- **Orders**: `AWAITING_PAYMENT`, `PAID`, `PROCESSING`, `COMPLETED`, `FAILED`, `CANCELLED`
- **Fulfillment Jobs**: `PENDING`, `PROCESSING`, `RETRY`, `COMPLETED`, `FAILED`
- **Outbox Events**: `PENDING`, `SENT`, `FAILED`
- **Telegram Sessions**: `IDLE`, `AWAITING_CATALOG`, `SELECTING_STYLES`, `SELECTING_FORMATS`, `CONFIRMING`, `ORDER_CREATED`

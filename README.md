# telegramfonts

Hybrid cloud architecture for automated Vietnamese font processing and fulfillment via Telegram bot.

## Architecture & Boundary

```
[ Telegram Client / SePay ] ──(Inbound Webhook)──> [ Cloudflare Worker (edge/) ]
                                                           │
                                                           ├── D1 (Canonical State)
                                                           ├── R2 (Artifact Storage)
                                                           └── Queue (Fulfillment Transport)
                                                                   │
                                                                   ▼ (Outbound HTTP-Pull / Job Lease)
                                                       [ Python A23 Agent (agent/) ]
                                                       (Private Compute - No Inbound Ports)
```

- **Control Plane (`edge/`)**: TypeScript Cloudflare Worker terminating Telegram and SePay webhooks, managing canonical durable state via Cloudflare D1, dispatching transactional outbox events to Cloudflare Queues, exposing fenced internal job control APIs, and storing assets in R2.
- **Compute Plane (`agent/`)**: Python A23 compute worker pulling jobs via Cloudflare Queue HTTP-pull consumer API, claiming fenced D1 leases, and processing fonts without opening public inbound ports.
- **Boundary Note**: Production Cloudflare resources, deployment bindings, and live secrets are **NOT** provisioned in this phase. All development and testing run against local simulated bindings (Miniflare/D1 local SQLite).

## Repository Layout

```
.
├── .github/
│   └── workflows/
│       └── ci.yml               # Continuous Integration (install, typecheck, test)
├── agent/                       # Python A23 private compute worker
│   ├── src/                     # Runner, Queue client, Worker client, compute pipeline
│   │   ├── compute/             # FontBuilder, Source acquisition, Validator, Packager
│   │   ├── config.py            # Environment settings
│   │   ├── logging_utils.py     # Sanitized structured logging
│   │   ├── queue_client.py      # Cloudflare Queues HTTP pull client
│   │   ├── runner.py            # Runner state machine and lease heartbeat loop
│   │   ├── scratch.py           # Scratch directory manager & path traversal guards
│   │   └── worker_client.py     # Internal job control client
│   ├── tests/                   # Pytest test suite
│   ├── pyproject.toml
│   ├── requirements.txt
│   └── README.md
├── edge/                        # Cloudflare Worker control-plane
│   ├── migrations/              # D1 database migrations
│   │   ├── 0001_initial_schema.sql
│   │   ├── 0002_telegram_sessions_and_catalog.sql
│   │   ├── 0003_payment_code_and_sepay.sql
│   │   └── 0004_outbox_dispatch_and_job_lease.sql
│   ├── src/                     # Worker TypeScript source
│   │   ├── handlers/            # Telegram, SePay, and Internal Job route handlers
│   │   ├── services/            # Telegram, catalog, session, order, payment, outbox, and job services
│   │   ├── types/               # Domain types & interfaces
│   │   ├── utils/               # URL normalization, HTML escaping, and VietQR generation
│   │   ├── env.ts               # Typed environment & bindings
│   │   └── index.ts             # Entrypoint, routing & scheduled dispatcher
│   ├── test/                    # Automated tests (Vitest + Cloudflare Workers Pool)
│   │   ├── apply-migrations.ts
│   │   ├── env.d.ts
│   │   ├── html.spec.ts
│   │   ├── index.spec.ts
│   │   ├── jobs.spec.ts
│   │   ├── myfonts.spec.ts
│   │   ├── outbox.spec.ts
│   │   ├── schema.spec.ts
│   │   ├── sepay.spec.ts
│   │   └── telegram.spec.ts
│   ├── package.json
│   ├── tsconfig.json
│   ├── vitest.config.ts
│   └── wrangler.jsonc           # Wrangler Worker configuration
├── package.json                 # Root monorepo workspace configuration
├── package-lock.json
└── README.md
```

## Cloudflare Queue HTTP-Pull Consumer & Lease Fencing

Per Decision #6 D05:
1. **No Queue Consumer Worker**: In Cloudflare Workers, no Queue push consumer Worker is deployed. A23 acts as an external HTTP-pull consumer.
2. **Operational Enablement**: HTTP pull is enabled operationally via Cloudflare Dashboard or CLI (`npx wrangler queues consumer http add <QUEUE-NAME>`), not via `wrangler.jsonc`. It requires a scoped Cloudflare API token with `Queues:Read` and `Queues:Write` permissions.
3. **Visibility Timeout & Fencing**: When pulling messages from Cloudflare Queue, A23 should explicitly request a visibility timeout suitable for long-running font reconstruction (e.g. 5–10 minutes) rather than relying on default short timeouts.
4. **D1 Lease Fencing**: Cloudflare Queues provide at-least-once delivery. D1 lease fencing with cryptographically random `lease_token`s is authoritative: duplicate redeliveries or competing workers cannot corrupt state or double-process jobs.

## Getting Started

### Prerequisites
- Node.js 20+
- npm 10+

### Installation
From repository root:
```bash
npm ci
```

### Local Development & Wrangler Commands

1. **Typecheck**:
   ```bash
   npm run typecheck
   ```

2. **Run Automated Tests**:
   ```bash
   npm test
   ```

3. **Apply D1 Migrations Locally**:
   ```bash
   npx --workspace=edge wrangler d1 migrations apply DB --local
   ```

4. **Start Local Development Worker**:
   ```bash
   npm run --workspace=edge dev
   ```

### Worker Endpoints

- `GET /health` -> Liveness check returning `200 {"status": "ok"}`.
- `GET /ready` -> Readiness check querying D1 database returning `200 {"status": "ready", "database": "connected"}` or `503` if unavailable.
- `POST /webhooks/telegram` -> Telegram Bot webhook endpoint (requires `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and `X-Telegram-Bot-Api-Secret-Token` header).
- `POST /webhooks/sepay` -> SePay payment webhook endpoint (requires `SEPAY_WEBHOOK_SECRET`, HMAC-SHA256 signature verification, and timestamp within 300s window).
- `POST /internal/jobs/:job_id/claim` -> Internal A23 node job claiming endpoint with D1 lease fencing (requires `A23_NODE_SECRET` Bearer token).
- `POST /internal/jobs/:job_id/heartbeat` -> Internal A23 node lease extension endpoint (requires `A23_NODE_SECRET` Bearer token).
- `POST /internal/jobs/:job_id/fail` -> Internal A23 node failure / retry transition endpoint (requires `A23_NODE_SECRET` Bearer token).
- All other routes return `404 Not Found`.

## Canonical States

- **Orders**: `AWAITING_PAYMENT`, `PAID`, `PROCESSING`, `COMPLETED`, `FAILED`, `CANCELLED`
- **Fulfillment Jobs**: `PENDING`, `PROCESSING`, `RETRY`, `COMPLETED`, `FAILED`
- **Outbox Events**: `PENDING`, `SENT`, `FAILED`
- **Telegram Sessions**: `IDLE`, `AWAITING_CATALOG`, `SELECTING_STYLES`, `SELECTING_FORMATS`, `CONFIRMING`, `ORDER_CREATED`

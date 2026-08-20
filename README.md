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
                                                                   ▼ (Outbound Pull / Job Lease)
                                                       [ Python A23 Agent (agent/) ]
                                                       (Private Compute - No Inbound Ports)
```

- **Control Plane (`edge/`)**: TypeScript Cloudflare Worker terminating Telegram and SePay webhooks, managing canonical durable state via Cloudflare D1, dispatching jobs via Queues, and storing assets in R2.
- **Compute Plane (`agent/`)**: Reserved directory for the future Python A23 compute worker that will process font generation requests without opening public inbound ports.
- **Boundary Note**: Production Cloudflare resources, deployment bindings, and live secrets are **NOT** provisioned in this initial phase. All development and testing run against local simulated bindings (Miniflare/D1 local SQLite).

## Repository Layout

```
.
├── .github/
│   └── workflows/
│       └── ci.yml               # Continuous Integration (install, typecheck, test)
├── agent/                       # Reserved for Python A23 compute worker
│   └── README.md
├── edge/                        # Cloudflare Worker control-plane
│   ├── migrations/              # D1 database migrations
│   │   └── 0001_initial_schema.sql
│   ├── src/                     # Worker TypeScript source
│   │   ├── env.ts               # Typed environment & bindings
│   │   └── index.ts             # Entrypoint & minimal health/ready routes
│   ├── test/                    # Automated tests (Vitest + Cloudflare Workers Pool)
│   │   ├── apply-migrations.ts
│   │   ├── env.d.ts
│   │   ├── index.spec.ts
│   │   └── schema.spec.ts
│   ├── package.json
│   ├── tsconfig.json
│   ├── vitest.config.ts
│   └── wrangler.jsonc           # Wrangler Worker configuration
├── package.json                 # Root monorepo workspace configuration
├── package-lock.json
└── README.md
```

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

### Minimal Worker Endpoints

- `GET /health` -> Liveness check returning `200 {"status": "ok"}`.
- `GET /ready` -> Readiness check querying D1 database returning `200 {"status": "ready", "database": "connected"}` or `503` if unavailable.
- All other routes return `404 Not Found`.

## Canonical States

- **Orders**: `AWAITING_PAYMENT`, `PAID`, `PROCESSING`, `COMPLETED`, `FAILED`, `CANCELLED`
- **Fulfillment Jobs**: `PENDING`, `PROCESSING`, `RETRY`, `COMPLETED`, `FAILED`

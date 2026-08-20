# TelegramFonts Operator Launch & Production Runbook

> **OPERATOR POLICY**: This document defines the exact, ordered deployment, verification, operation, and rollback procedures for TelegramFonts. Every mutation of live infrastructure or production secrets is strictly marked **`[WAIT HUMAN]`** and must be performed only by an authorized human operator.

---

## 1. Architecture Summary & Boundaries

```
[ Telegram Webhook ] ──┐
                       ├──> [ Cloudflare Worker (edge/) ]
[ SePay Webhook ]   ───┘         │  (TypeScript, D1, R2, Queues)
                                 ├── D1: Canonical state & fenced leases
                                 ├── R2: Private artifact storage
                                 └── Queue: Outbound fulfillment producer
                                         │
                                         ▼ (HTTP-Pull Consumer via Cloudflare API)
                             [ Python A23 Compute Agent (agent/) ]
                             (Outbound-Only Private Device, No Inbound Ports)
```

- **Edge Control Plane**: Stateless Cloudflare Worker executing Telegram UX, VietQR payment negotiation, SePay settlement, D1 atomic transactions, R2 streaming, signed HMAC download verification, and scheduled outbox dispatching.
- **A23 Compute Plane**: Private ARM64 / Termux / Linux Python worker pulling jobs via Cloudflare Queue HTTP-pull, claiming fenced D1 leases, reconstructing TrueType/OpenType/WOFF2 binaries, and streaming ZIP bundles into private R2.

---

## 2. Cloudflare Constraints & Runtime Parameters

| Parameter | Cloudflare Platform Limit | TelegramFonts Application Value | Rationale |
| :--- | :--- | :--- | :--- |
| **Queue Pull Batch Size** | 100 msgs / pull | `1` to `10` (default `1`) | Prevents head-of-line blocking on long font builds |
| **Visibility Timeout** | Max 12 hours (43,200s) | `300,000 ms` (5 min) | Must exceed `A23_JOB_LEASE_SECONDS` (300s) |
| **Queue Throughput** | 5,000 msgs / sec | ~10-50 msgs / day (goal 1000/day) | Far below platform ceilings |
| **Worker Request Body** | 100 MB | 50 MiB (`MAX_ARTIFACT_BYTES`) | Enforces safe memory bounds for ZIP uploads |
| **D1 Concurrency** | 1 write transaction / DB | Gated 5-statement atomic batches | Sub-10ms write latency with covering indexes |
| **R2 Storage** | Unlimited | Private bucket (no public URL) | Access strictly via HMAC-SHA256 signed URLs |

---

## 3. Ordered Launch Sequence

### Step 1: Preflight Verification `[REPO_PASS]`
From repository root, execute the deterministic preflight check:
```bash
npm run preflight
```
*Expected Result*: 12/12 checks pass (verifies binding definitions, architecture rules, and parameter boundaries).

---

### Step 2: D1 Database Provisioning `[WAIT HUMAN]`
1. Create the production D1 database:
   ```bash
   npx wrangler d1 create telegramfonts-d1
   ```
2. Copy the resulting `database_id` into `edge/wrangler.jsonc`:
   ```jsonc
   "d1_databases": [
     {
       "binding": "DB",
       "database_name": "telegramfonts-d1",
       "database_id": "<REAL-D1-UUID-FROM-STEP-1>",
       "migrations_dir": "migrations"
     }
   ]
   ```
3. Apply all additive migrations (0001 through 0005) to remote D1:
   ```bash
   npx wrangler d1 migrations apply DB --remote
   ```

---

### Step 3: Cloudflare Queue & HTTP-Pull Consumer `[WAIT HUMAN]`
1. Create the fulfillment queue:
   ```bash
   npx wrangler queues create telegramfonts-fulfillment
   ```
2. Enable HTTP-pull consumer on the queue:
   ```bash
   npx wrangler queues consumer http add telegramfonts-fulfillment
   ```
3. Create a scoped Cloudflare API token:
   - **Permissions**: `Account.Queues:Read`, `Account.Queues:Write`
   - **Account Resources**: Include target Cloudflare Account ID
   - Note down the token value as `CF_QUEUES_TOKEN`.

---

### Step 4: Private R2 Bucket Creation `[WAIT HUMAN]`
1. Create the private artifacts bucket:
   ```bash
   npx wrangler r2 bucket create telegramfonts-artifacts
   ```
2. Ensure public access is **disabled** (R2 default). Do **NOT** attach a public custom domain.

---

### Step 5: Worker Secrets Configuration `[WAIT HUMAN]`
Set all required production secrets in Cloudflare Workers:
```bash
# 1. Telegram Bot Token (from @BotFather)
npx wrangler secret put TELEGRAM_BOT_TOKEN

# 2. Telegram Webhook Secret (cryptographically random 32+ hex string)
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET

# 3. SePay Webhook Secret (from SePay merchant dashboard)
npx wrangler secret put SEPAY_WEBHOOK_SECRET

# 4. Internal A23 Node Secret (cryptographically random 32+ hex string)
npx wrangler secret put A23_NODE_SECRET

# 5. Download HMAC Signing Secret (cryptographically random 32+ hex string)
npx wrangler secret put DOWNLOAD_SIGNING_SECRET
```

---

### Step 6: Worker Deployment & Webhook Registration `[WAIT HUMAN]`
1. Deploy Cloudflare Worker:
   ```bash
   npx wrangler deploy
   ```
2. Verify Worker liveness & readiness:
   ```bash
   curl -i https://<worker-domain>/health
   # Expected: HTTP 200 {"status":"ok"}

   curl -i https://<worker-domain>/ready
   # Expected: HTTP 200 {"status":"ready","database":"connected"}
   ```
3. Register Telegram Bot Webhook:
   ```bash
   curl -F "url=https://<worker-domain>/webhooks/telegram" \
        -F "secret_token=<TELEGRAM_WEBHOOK_SECRET>" \
        https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook
   ```

---

### Step 7: A23 Compute Agent Deployment `[WAIT HUMAN]`
1. On the physical A23 Android/ARM64 (or dedicated Linux server), prepare `/etc/telefont/agent.env`:
   ```env
   CF_ACCOUNT_ID=<YOUR_CLOUDFLARE_ACCOUNT_ID>
   CF_QUEUE_ID=telegramfonts-fulfillment
   CF_QUEUES_TOKEN=<SCOPED_QUEUES_API_TOKEN>
   EDGE_BASE_URL=https://<worker-domain>
   A23_NODE_SECRET=<A23_NODE_SECRET>
   A23_WORKER_ID=a23-node-primary-01
   SCRATCH_DIR=/var/tmp/telefont/scratch
   PULL_BATCH_SIZE=1
   VISIBILITY_TIMEOUT_MS=300000
   HEARTBEAT_INTERVAL_SECONDS=60
   LEASE_DURATION_SECONDS=300
   ```
2. Install dependencies:
   ```bash
   pip install -r agent/requirements-lock.txt
   ```
3. Start the A23 compute worker daemon:
   ```bash
   python agent/src/main.py
   ```
   *(Or enable systemd service `systemctl start telefont-agent`)*

---

## 4. Live Verification & Observability

### Tail Worker Logs
```bash
npx wrangler tail --format=json
```

### Tail A23 Agent Logs
```bash
journalctl -u telefont-agent -f
```

### Key Health Signals
- **Liveness**: `GET /health` -> 200
- **Readiness**: `GET /ready` -> 200 (checks D1 schema migration status and database connectivity)
- **Queue Backlog**: Check Cloudflare Dashboard -> Queues -> `telegramfonts-fulfillment` backlog graph (should remain near 0).

---

## 5. Rollback Procedures `[WAIT HUMAN]`

### 1. Worker Version Rollback
If a defect is detected in a new Worker deployment:
```bash
# List recent deployment versions
npx wrangler deployments list

# Rollback to specific version
npx wrangler rollback <DEPLOYMENT-ID>
```

### 2. D1 Schema Emergency Rollback
All migrations (0001–0005) are strictly additive (new tables and nullable columns with defaults). In the event of application rollback, older application code continues to execute safely against the schema without dropping columns or tables.

### 3. A23 Worker Drain / Stop
To safely stop the A23 compute agent without interrupting in-flight font jobs:
```bash
# Send SIGINT / SIGTERM for graceful shutdown
kill -SIGINT $(pgrep -f "python.*agent/src/main.py")
```
The runner will finish current message processing, upload artifact, complete the D1 job, ACK the queue, and exit cleanly.

### 4. Queue Purge (Emergency Drill)
If malformed poisoned messages cause a continuous loop before dead-lettering:
```bash
npx wrangler queues purge telegramfonts-fulfillment
```
*(Only use in catastrophic queue corruption scenarios)*.

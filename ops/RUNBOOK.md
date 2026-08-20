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
                                         ▼ (HTTP-Pull Consumer via Cloudflare REST API)
                             [ Python A23 Compute Agent (agent/) ]
                             (Outbound-Only Private Device, No Inbound Ports)
```

- **Edge Control Plane**: Stateless Cloudflare Worker executing Telegram UX, VietQR payment negotiation, SePay settlement, D1 atomic transactions, R2 streaming, signed HMAC download verification, and scheduled outbox dispatching.
- **A23 Compute Plane**: Private ARM64 / Termux / Linux Python worker pulling jobs via Cloudflare Queue HTTP-pull REST API, claiming fenced D1 leases, reconstructing TrueType/OpenType/WOFF2 binaries, and streaming ZIP bundles into private R2.

---

## 2. Cloudflare Constraints & Runtime Parameters

| Parameter | Cloudflare Platform Limit | TelegramFonts Application Value | Rationale |
| :--- | :--- | :--- | :--- |
| **Queue Pull Batch Size** | 100 msgs / pull | `1` to `10` (default `1`) | Prevents head-of-line blocking on long font builds |
| **Visibility Timeout** | Max 12 hours (43,200s) | `300,000 ms` (5 min) | Must exceed `A23_JOB_LEASE_SECONDS` (300s) |
| **Queue Throughput** | 5,000 msgs / sec | ~10-50 msgs / day (goal 1000/day) | Far below platform ceilings |
| **Worker Request Body** | 100 MB | 50 MiB (`MAX_ARTIFACT_BYTES`) | Enforces safe memory bounds for ZIP uploads |
| **D1 Concurrency** | 1 write transaction / DB | Gated 5-statement atomic batches | Minimized write lock duration with covering indexes |
| **R2 Storage** | Unlimited | Private bucket (no public URL) | Access strictly via HMAC-SHA256 signed URLs |

---

## 3. Ordered Launch Sequence

### Step 1: Preflight Verification `[REPO_PASS]`
From repository root, execute the deterministic preflight check:
```bash
npm run preflight
```
*Expected Result*: All checks pass in test fixture mode.

---

### Step 2: D1 Database Provisioning `[WAIT HUMAN]`
1. Create the production D1 database:
   ```bash
   npx wrangler d1 create telegramfonts-d1 --config edge/wrangler.jsonc
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
   npx wrangler d1 migrations apply DB --remote --config edge/wrangler.jsonc
   ```

---

### Step 3: Cloudflare Queue & HTTP-Pull Consumer `[WAIT HUMAN]`
1. Create the fulfillment queue:
   ```bash
   npx wrangler queues create telegramfonts-fulfillment --config edge/wrangler.jsonc
   ```
2. Enable HTTP-pull consumer on the queue:
   ```bash
   npx wrangler queues consumer http add telegramfonts-fulfillment --config edge/wrangler.jsonc
   ```
3. Retrieve the Queue Resource ID:
   ```bash
   npx wrangler queues list --config edge/wrangler.jsonc
   ```
   *Note*: The Cloudflare Queue REST API endpoint requires the Queue Resource UUID (e.g. `018f...`). Set this UUID as `CF_QUEUE_ID` in the agent configuration.
4. Create a scoped Cloudflare API token:
   - **Permissions**: `Account.Queues:Read`, `Account.Queues:Write`
   - **Account Resources**: Include target Cloudflare Account ID
   - Set as `CF_QUEUES_TOKEN`.

---

### Step 4: Private R2 Bucket Creation `[WAIT HUMAN]`
1. Create the private artifacts bucket:
   ```bash
   npx wrangler r2 bucket create telegramfonts-artifacts --config edge/wrangler.jsonc
   ```
2. Ensure public access is **disabled** (R2 default). Do **NOT** attach a public custom domain.

---

### Step 5: Worker Secrets & Payment Configuration `[WAIT HUMAN]`
Set all required production secrets in Cloudflare Workers:
```bash
# 1. Telegram Bot Token (from @BotFather)
npx wrangler secret put TELEGRAM_BOT_TOKEN --config edge/wrangler.jsonc

# 2. Telegram Webhook Secret (cryptographically random 32+ hex string)
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET --config edge/wrangler.jsonc

# 3. SePay Webhook Secret (from SePay merchant dashboard)
npx wrangler secret put SEPAY_WEBHOOK_SECRET --config edge/wrangler.jsonc

# 4. Internal A23 Node Secret (cryptographically random 32+ hex string)
npx wrangler secret put A23_NODE_SECRET --config edge/wrangler.jsonc

# 5. Download HMAC Signing Secret (cryptographically random 32+ hex string)
npx wrangler secret put DOWNLOAD_SIGNING_SECRET --config edge/wrangler.jsonc
```

Set runtime variables in `edge/wrangler.jsonc` `[vars]` or deploy secrets:
- `BASE_URL`: Production HTTPS origin (e.g. `https://telefont.yourdomain.com`).
- `BANK_ID`: Napas Bank BIN code (e.g. `970422` for MB Bank).
- `BANK_ACCOUNT_NUMBER`: Merchant receiving account number.
- `BANK_ACCOUNT_NAME`: Merchant receiving account name.

---

### Step 6: Worker Deployment `[WAIT HUMAN]`
1. Deploy Cloudflare Worker:
   ```bash
   npx wrangler deploy --config edge/wrangler.jsonc
   ```
2. Verify Worker liveness & readiness:
   ```bash
   curl -i https://<worker-domain>/health
   # Expected: HTTP 200 {"status":"ok"}

   curl -i https://<worker-domain>/ready
   # Expected: HTTP 200 {"status":"ready","database":"connected"}
   ```

---

### Step 7: A23 Compute Agent Setup (Termux / Linux) `[WAIT HUMAN]`

#### On Physical Samsung Galaxy A23 (Termux)
1. Install system prerequisites in Termux:
   ```bash
   pkg update && pkg install -y python git clang libxml2 libxslt
   termux-wake-lock
   ```
2. Clone repository & install pinned dependencies:
   ```bash
   git clone https://github.com/dtadptvl/telegramfonts.git telefont
   cd telefont
   pip install -r agent/requirements-lock.txt
   ```
3. Prepare environment file `~/.telefont.env`:
   ```env
   CF_ACCOUNT_ID=<YOUR_CLOUDFLARE_ACCOUNT_ID>
   CF_QUEUE_ID=<QUEUE_RESOURCE_UUID_FROM_STEP_3>
   CF_QUEUES_TOKEN=<SCOPED_QUEUES_API_TOKEN>
   EDGE_BASE_URL=https://<worker-domain>
   A23_NODE_SECRET=<A23_NODE_SECRET>
   A23_WORKER_ID=a23-termux-primary-01
   SCRATCH_DIR=/data/data/com.termux/files/usr/tmp/telefont/scratch
   PULL_BATCH_SIZE=1
   VISIBILITY_TIMEOUT_MS=300000
   HEARTBEAT_INTERVAL_SECONDS=60
   LEASE_DURATION_SECONDS=300
   ```
4. Load environment & Execute Authoritative Physical A23 Capacity Benchmark:
   ```bash
   set -a && source ~/.telefont.env && set +a
   python agent/src/benchmark.py --samples 20 --json-out ops/a23_device_benchmark.json
   ```
   *Verify*: Benchmark passes with 0 failures and records on-device ARM64 hardware identity.

5. Start the A23 Worker Daemon:
   ```bash
   set -a && source ~/.telefont.env && set +a
   python agent/src/main.py
   ```

---

### Step 8: Strict Preflight & Recovery Gating `[WAIT HUMAN]`
Before enabling public ingress traffic, run strict preflight against the live deployment:
```bash
npm run preflight -- --strict
```
*Gating requirement*: All 12 production checks must pass.

---

### Step 9: Webhook Ingress Activation `[WAIT HUMAN]`
1. Register Telegram Bot Webhook:
   ```bash
   curl -F "url=https://<worker-domain>/webhooks/telegram" \
        -F "secret_token=<TELEGRAM_WEBHOOK_SECRET>" \
        https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook
   ```
2. Configure SePay Webhook in SePay merchant portal:
   - **Webhook URL**: `https://<worker-domain>/webhooks/sepay`
   - **Authentication**: HMAC-SHA256 signature with `SEPAY_WEBHOOK_SECRET`
   - **Events**: Bank transfer received.

---

## 4. Live Observability & Log Signals

### Tail Edge Worker Logs
```bash
npx wrangler tail --config edge/wrangler.jsonc --format=json
```

### Tail A23 Agent Logs
- In Termux / Linux daemon:
  ```bash
  tail -f /data/data/com.termux/files/usr/tmp/telefont.log
  ```

---

## 5. Rollback & Disaster Recovery Procedures `[WAIT HUMAN]`

### 1. Worker Version Rollback
If a defect is detected in a new Worker deployment:
```bash
npx wrangler deployments list --config edge/wrangler.jsonc
npx wrangler rollback <DEPLOYMENT-ID> --config edge/wrangler.jsonc
```

### 2. D1 Schema Compatibility
All migrations (0001–0005) are strictly additive. Older Worker versions can execute safely without schema rollbacks.

### 3. A23 Compute Agent Drain / Stop
To safely stop the A23 compute worker without interrupting in-flight font jobs:
```bash
kill -SIGINT $(pgrep -f "python.*agent/src/main.py")
```
The runner will finish current compute, upload the artifact to R2, complete in D1, ACK the queue, and exit cleanly.

### 4. Queue Reconciliation & Re-drive (Replacing Queue Purge)
If queue messages become stuck or unacknowledged:
> [!CAUTION]
> Do NOT purge Cloudflare Queues blindly. D1 is the canonical source of truth.
1. Inspect stuck jobs in D1:
   ```sql
   SELECT id, order_id, status, attempt_count, last_error FROM fulfillment_jobs WHERE status IN ('PENDING', 'RETRY');
   ```
2. To re-drive unfulfilled jobs into the Queue safely without corrupting completed jobs, dispatch an outbox event from D1.

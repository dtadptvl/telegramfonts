# TelegramFonts Security Architecture & Runtime Separation

This document specifies secret separation boundaries, permission scoping, network isolation, and secret rotation procedures.

---

## 1. Secret Separation Matrix

| Secret Name | Consumer | Scope & Permissions | Exposure Boundary |
| :--- | :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | Edge Worker | Telegram Bot API client communication | Worker environment only; never exposed to A23 or webhooks |
| `TELEGRAM_WEBHOOK_SECRET` | Edge Worker | Validates `X-Telegram-Bot-Api-Secret-Token` on `/webhooks/telegram` | Worker environment only; shared only with Telegram setWebhook |
| `SEPAY_WEBHOOK_SECRET` | Edge Worker | Validates HMAC-SHA256 signatures on `/webhooks/sepay` | Worker environment only; shared only with SePay dashboard |
| `A23_NODE_SECRET` | Edge Worker + A23 Agent | Mutual Bearer authentication on `/internal/jobs/*` | Worker secret + A23 local environment only |
| `DOWNLOAD_SIGNING_SECRET` | Edge Worker | Signs and verifies versioned HMAC URLs on `/downloads/*` | Worker environment only; never exposed to A23 or users |
| `CF_QUEUES_TOKEN` | A23 Agent | Scoped Cloudflare API Token (`Queues:Read`, `Queues:Write`) | A23 local environment only; no access to D1, R2, or Worker scripts |

---

## 2. Network & Storage Isolation

1. **A23 Outbound-Only Principle**:
   - The A23 compute worker establishes **outbound HTTPS connections only** (to Cloudflare Queue API, Edge `/internal/jobs`, and public preview URLs).
   - No open inbound TCP ports, no reverse tunnels (e.g. ngrok/cloudflared tunnel are forbidden), no listening servers.
2. **Private R2 Bucket**:
   - The `ARTIFACTS_BUCKET` is private. No public custom domain or public bucket URLs exist.
   - User downloads are routed exclusively through the Cloudflare Worker `/downloads/:order_id` endpoint after verifying HMAC-SHA256 signatures and expiration timestamps.
3. **No Direct R2 Access for A23**:
   - A23 does not possess R2 AWS/S3 access keys.
   - A23 streams artifacts exclusively through the authenticated Worker control plane via `PUT /internal/jobs/:job_id/artifact`.

---

## 3. Secret Rotation Procedures `[WAIT HUMAN]`

### 1. Rotating `A23_NODE_SECRET`
1. Generate fresh cryptographically random 32-byte hex token: `NEW_SECRET=$(openssl rand -hex 32)`.
2. Stop A23 compute agent: `systemctl stop telefont-agent`.
3. Update Worker secret: `npx wrangler secret put A23_NODE_SECRET` (enter `$NEW_SECRET`).
4. Update `/etc/telefont/agent.env` with `A23_NODE_SECRET=$NEW_SECRET`.
5. Restart A23 compute agent: `systemctl start telefont-agent`.
6. Verify `/health` and test job execution.

### 2. Rotating `DOWNLOAD_SIGNING_SECRET`
1. Generate new secret: `NEW_SECRET=$(openssl rand -hex 32)`.
2. Update Worker secret: `npx wrangler secret put DOWNLOAD_SIGNING_SECRET`.
3. *Note*: In-flight download links generated before rotation will be rejected (signature mismatch); users can click "Check Status" in Telegram to receive fresh signed download links immediately.

### 3. Rotating `CF_QUEUES_TOKEN`
1. Create new Scoped API Token in Cloudflare Dashboard with `Queues:Read` and `Queues:Write`.
2. Update `/etc/telefont/agent.env` with `CF_QUEUES_TOKEN=<NEW_TOKEN>`.
3. Restart A23 compute agent: `systemctl restart telefont-agent`.
4. Delete old API token in Cloudflare Dashboard.

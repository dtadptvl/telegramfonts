# TelegramFonts Observability & Telemetry Reference

This document defines the structured log events, diagnostic signals, redaction standards, and metric monitoring procedures across the Edge control plane and A23 compute plane.

---

## 1. Sanitized Structured Event Schemas

All log output adheres to strict data minimization contracts (`edge/src/utils/logger.ts`): secret tokens, authorization headers, raw webhook signatures, bank account numbers, and user personally identifiable information (PII) are systematically stripped or redacted.

### Control Plane (`edge/`) Structured Events

| Event Name | Key Fields | Redacted / Excluded Fields | Emitting Module |
| :--- | :--- | :--- | :--- |
| `payment_accepted` | `order_id`, `user_id`, `amount`, `currency`, `payment_code`, `provider: "SEPAY"` | Raw webhook signature, merchant bank account numbers, raw payload JSON | `sepay-webhook.ts` |
| `outbox_dispatched` | `event_id`, `event_type`, `aggregate_id`, `attempt`, `status: "SENT"` | Recipient chat tokens, internal bearer tokens | `outbox-service.ts` |
| `job_claimed` | `job_id`, `worker_id`, `lease_duration_sec`, `attempt_count` | `lease_token` (redacted in external logs), Bearer auth headers | `internal-jobs.ts` |
| `job_heartbeat` | `job_id`, `worker_id`, `lease_expires_at` | `lease_token` (redacted in external logs) | `internal-jobs.ts` |
| `job_completed` | `job_id`, `order_id`, `artifact_key`, `size_bytes` | Auth headers | `internal-jobs.ts` |
| `telegram_delivered` | `order_id`, `chat_id`, `event_id` | Bot token, webhook secret | `outbox-service.ts` |
| `download_served` | `order_id`, `size_bytes` | HMAC signing secret, signature query param | `downloads.ts` |

### Compute Plane (`agent/`) Structured Events

| Event Name | Key Fields | Redacted / Excluded Fields | Emitting Module |
| :--- | :--- | :--- | :--- |
| `queue_pull` | `worker_id`, `batch_size`, `messages_received_count` | `CF_QUEUES_TOKEN`, authorization bearer | `queue_client.py` |
| `compute_started` | `job_id`, `styles_count`, `formats_count` | `lease_token`, `A23_NODE_SECRET` | `runner.py` |
| `compute_finished` | `job_id`, `font_files_count`, `duration_ms`, `peak_rss_mb` | File system internal absolute paths outside scratch | `runner.py` |
| `artifact_uploaded` | `job_id`, `artifact_key`, `sha256`, `size_bytes`, `duration_ms` | `A23_NODE_SECRET` | `worker_client.py` |
| `queue_acked` | `worker_id`, `message_id`, `job_id`, `status: "ACKED"` | `CF_QUEUES_TOKEN` | `runner.py` |

---

## 2. Cloudflare Dashboard & CLI Monitoring

### 1. Workers Live Tail Logs
```bash
npx wrangler tail --config edge/wrangler.jsonc --format=json
```
Filter for errors:
```bash
npx wrangler tail --config edge/wrangler.jsonc --status=error
```

### 2. Cloudflare Queues Metrics
- **Backlog Count**: Should remain $\approx 0$. A steady climb indicates A23 compute worker disconnection or lease contention.
- **Messages In / Messages Out**: Tracks real-time traffic balance between edge orders and worker processing.
- **Concurrent Consumers**: Tracks active HTTP-pull polling sessions.

### 3. D1 Query & Database Metrics
- **Query Performance**: Monitored via Cloudflare Dashboard -> D1 Analytics.
- **Row Read / Write Counts**: Verify indexed queries on `fulfillment_jobs(id)`, `orders(id)`, `outbox_events(status, next_dispatch_at)`.

### 4. R2 Bucket Metrics
- **Put Requests / Get Requests**: 1 PUT per completed order, 1-3 GETs per user download.
- **Error Rates**: 4xx/5xx should remain $0\%$.

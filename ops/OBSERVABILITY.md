# TelegramFonts Observability & Telemetry Reference

This document defines the structured log events, diagnostic signals, redaction standards, and metric monitoring procedures across the Edge control plane and A23 compute plane.

---

## 1. Sanitized Structured Event Schemas

All log output adheres to data minimization contracts: secret tokens, authorization headers, raw webhook signatures, bank account details, and user personally identifiable information (PII) are strictly stripped or redacted.

### Control Plane (`edge/`) Structured Events

| Event Name | Key Fields | Redacted / Excluded Fields |
| :--- | :--- | :--- |
| `payment_accepted` | `order_id`, `user_id`, `amount`, `currency`, `payment_code`, `provider: "SEPAY"` | Raw webhook signature, merchant bank account numbers, raw payload JSON |
| `outbox_dispatched` | `event_id`, `event_type`, `aggregate_type`, `aggregate_id`, `dispatch_attempt`, `status` | Recipient chat tokens, internal bearer tokens |
| `job_claimed` | `job_id`, `order_id`, `worker_id`, `lease_expires_at`, `attempt_count` | `lease_token` (redacted in external logs), Bearer auth headers |
| `job_heartbeat` | `job_id`, `worker_id`, `lease_expires_at`, `status: "EXTENDED"` | `lease_token` (redacted in external logs) |
| `job_completed` | `job_id`, `order_id`, `artifact_key`, `artifact_sha256`, `size_bytes`, `status: "COMPLETED"` | Auth headers |
| `telegram_delivered` | `order_id`, `chat_id`, `event_id`, `status: "SENT"` | Bot token, webhook secret |
| `download_served` | `order_id`, `artifact_key`, `size_bytes`, `status: 200` | HMAC signing secret, signature query param |

### Compute Plane (`agent/`) Structured Events

| Event Name | Key Fields | Redacted / Excluded Fields |
| :--- | :--- | :--- |
| `queue_pull` | `worker_id`, `batch_size`, `messages_received_count` | `CF_QUEUES_TOKEN`, authorization bearer |
| `compute_started` | `job_id`, `styles_count`, `formats_count` | `lease_token`, `A23_NODE_SECRET` |
| `compute_finished` | `job_id`, `font_files_count`, `duration_ms`, `peak_rss_mb` | File system internal absolute paths outside scratch |
| `artifact_uploaded` | `job_id`, `artifact_key`, `sha256`, `size_bytes`, `duration_ms` | `A23_NODE_SECRET` |
| `queue_acked` | `worker_id`, `message_id`, `job_id`, `status: "ACKED"` | `CF_QUEUES_TOKEN` |

---

## 2. Cloudflare Dashboard & CLI Monitoring

### 1. Workers Live Tail Logs
```bash
npx wrangler tail --format=json
```
Filter for errors:
```bash
npx wrangler tail --status=error
```

### 2. Cloudflare Queues Metrics
- **Backlog Count**: Should remain $\approx 0$. A steady climb indicates A23 compute worker disconnection or crash.
- **Messages In / Messages Out**: Proves production queue traffic balance.
- **Concurrent Consumers**: Tracks active HTTP-pull polling sessions.

### 3. D1 Query & Database Metrics
- **Query Latency**: Write transactions should execute in $< 15\text{ ms}$.
- **Row Read / Write Counts**: Verify indexed queries on `fulfillment_jobs(id)`, `orders(id)`, `outbox_events(status, next_dispatch_at)`.

### 4. R2 Bucket Metrics
- **Put Requests / Get Requests**: 1 PUT per completed order, 1-3 GETs per user download.
- **Error Rates**: 4xx/5xx should remain $0\%$.

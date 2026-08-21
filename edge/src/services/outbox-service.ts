import type { Env } from '../env';
import { TelegramClient } from './telegram-client';
import { escapeHtml } from '../utils/html';
import { emitStructuredLog } from '../utils/logger';
import type { ArtifactPartMeta } from './job-service';

export interface OutboxEventRecord {
  id: string;
  event_type: string;
  aggregate_type: string;
  aggregate_id: string;
  payload: string;
  status: string;
  dispatch_lease_token: string | null;
  dispatch_leased_at: number | null;
  dispatch_lease_expires_at: number | null;
  dispatch_attempts: number;
  next_dispatch_at: number | null;
  last_dispatch_error: string | null;
  dispatched_at: number | null;
  created_at: number;
}

export interface DispatchOptions {
  batchSize?: number;
  leaseDurationSeconds?: number;
}

export interface DispatchResult {
  dispatchedCount: number;
  failureCount: number;
}

export class OutboxService {
  constructor(
    private readonly db: D1Database,
    private readonly queue?: Queue<unknown>,
    private readonly env?: Env
  ) {}

  async dispatchPendingEvents(options: DispatchOptions = {}): Promise<DispatchResult> {
    const batchSize = Math.max(1, Math.min(options.batchSize || 10, 50));
    const leaseDurationMs = Math.max(5, options.leaseDurationSeconds || 30) * 1000;
    const now = Date.now();

    // 1. Select candidate pending outbox rows due for dispatch (JOB_READY & DELIVERY_READY)
    const candidates = await this.db
      .prepare(
        `SELECT * FROM outbox_events
         WHERE status = 'PENDING'
           AND event_type IN ('JOB_READY', 'DELIVERY_READY')
           AND (next_dispatch_at IS NULL OR next_dispatch_at <= ?)
           AND (dispatch_lease_expires_at IS NULL OR dispatch_lease_expires_at <= ?)
         ORDER BY created_at ASC
         LIMIT ?`
      )
      .bind(now, now, batchSize)
      .all<OutboxEventRecord>();

    let dispatchedCount = 0;
    let failureCount = 0;

    for (const event of candidates.results) {
      const leaseToken = crypto.randomUUID();
      const leaseExpiresAt = now + leaseDurationMs;

      // 2. CAS Acquire Lease before calling external services
      const acquireResult = await this.db
        .prepare(
          `UPDATE outbox_events
           SET dispatch_lease_token = ?,
               dispatch_leased_at = ?,
               dispatch_lease_expires_at = ?,
               dispatch_attempts = dispatch_attempts + 1
           WHERE id = ?
             AND status = 'PENDING'
             AND (next_dispatch_at IS NULL OR next_dispatch_at <= ?)
             AND (dispatch_lease_expires_at IS NULL OR dispatch_lease_expires_at <= ?)`
        )
        .bind(leaseToken, now, leaseExpiresAt, event.id, now, now)
        .run();

      if (!acquireResult.meta.changes || acquireResult.meta.changes === 0) {
        continue;
      }

      // Parse payload
      let parsed: Record<string, unknown> | null = null;
      try {
        parsed = JSON.parse(event.payload) as Record<string, unknown>;
      } catch {
        parsed = null;
      }

      // Route 1: JOB_READY -> Cloudflare Fulfillment Queue
      if (event.event_type === 'JOB_READY') {
        if (
          !parsed ||
          typeof parsed !== 'object' ||
          Array.isArray(parsed) ||
          typeof parsed.job_id !== 'string' ||
          !/^[a-zA-Z0-9_-]{1,64}$/.test(parsed.job_id.trim())
        ) {
          failureCount++;
          const attempts = event.dispatch_attempts + 1;
          const backoffSeconds = Math.min(300, 5 * Math.pow(2, attempts - 1));
          const nextDispatchAt = Date.now() + backoffSeconds * 1000;

          await this.db
            .prepare(
              `UPDATE outbox_events
               SET dispatch_lease_token = NULL,
                   dispatch_leased_at = NULL,
                   dispatch_lease_expires_at = NULL,
                   next_dispatch_at = ?,
                   last_dispatch_error = 'INVALID_JOB_ID_PAYLOAD'
               WHERE id = ? AND status = 'PENDING' AND dispatch_lease_token = ?`
            )
            .bind(nextDispatchAt, event.id, leaseToken)
            .run();
          continue;
        }

        if (!this.queue) {
          failureCount++;
          continue;
        }

        const jobId = parsed.job_id.trim();
        const queueMessage = { job_id: jobId };

        try {
          await this.queue.send(queueMessage);

          const markSentResult = await this.db
            .prepare(
              `UPDATE outbox_events
               SET status = 'SENT',
                   dispatched_at = ?,
                   dispatch_lease_token = NULL,
                   dispatch_leased_at = NULL,
                   dispatch_lease_expires_at = NULL,
                   last_dispatch_error = NULL
               WHERE id = ? AND status = 'PENDING' AND dispatch_lease_token = ?`
            )
            .bind(Date.now(), event.id, leaseToken)
            .run();

          if (markSentResult.meta.changes && markSentResult.meta.changes > 0) {
            dispatchedCount++;
            emitStructuredLog({
              event: 'outbox_dispatched',
              event_id: event.id,
              event_type: event.event_type,
              aggregate_id: event.aggregate_id,
              attempt: event.dispatch_attempts + 1,
              status: 'SENT',
            });
          }
        } catch {
          failureCount++;
          const attempts = event.dispatch_attempts + 1;
          const backoffSeconds = Math.min(300, 5 * Math.pow(2, attempts - 1));
          const nextDispatchAt = Date.now() + backoffSeconds * 1000;

          await this.db
            .prepare(
              `UPDATE outbox_events
               SET dispatch_lease_token = NULL,
                   dispatch_leased_at = NULL,
                   dispatch_lease_expires_at = NULL,
                   next_dispatch_at = ?,
                   last_dispatch_error = 'QUEUE_SEND_FAILED'
               WHERE id = ? AND status = 'PENDING' AND dispatch_lease_token = ?`
            )
            .bind(nextDispatchAt, event.id, leaseToken)
            .run();
        }
        continue;
      }

      // Route 2: DELIVERY_READY -> Telegram Notification with signed download URL (BLOCK 8)
      if (event.event_type === 'DELIVERY_READY') {
        if (
          !parsed ||
          typeof parsed !== 'object' ||
          Array.isArray(parsed) ||
          typeof parsed.order_id !== 'string' ||
          !/^[a-zA-Z0-9_-]{1,64}$/.test(parsed.order_id.trim())
        ) {
          failureCount++;
          const attempts = event.dispatch_attempts + 1;
          const backoffSeconds = Math.min(300, 5 * Math.pow(2, attempts - 1));
          const nextDispatchAt = Date.now() + backoffSeconds * 1000;

          await this.db
            .prepare(
              `UPDATE outbox_events
               SET dispatch_lease_token = NULL,
                   dispatch_leased_at = NULL,
                   dispatch_lease_expires_at = NULL,
                   next_dispatch_at = ?,
                   last_dispatch_error = 'INVALID_ORDER_ID_PAYLOAD'
               WHERE id = ? AND status = 'PENDING' AND dispatch_lease_token = ?`
            )
            .bind(nextDispatchAt, event.id, leaseToken)
            .run();
          continue;
        }

        const orderId = parsed.order_id.trim();

        // Query canonical order and user chat ID
        const order = await this.db
          .prepare('SELECT id, user_id, status, metadata FROM orders WHERE id = ?')
          .bind(orderId)
          .first<{ id: string; user_id: string; status: string; metadata?: string | null }>();

        if (!order || order.status !== 'COMPLETED') {
          failureCount++;
          const attempts = event.dispatch_attempts + 1;
          const backoffSeconds = Math.min(300, 5 * Math.pow(2, attempts - 1));
          const nextDispatchAt = Date.now() + backoffSeconds * 1000;

          await this.db
            .prepare(
              `UPDATE outbox_events
               SET dispatch_lease_token = NULL,
                   dispatch_leased_at = NULL,
                   dispatch_lease_expires_at = NULL,
                   next_dispatch_at = ?,
                   last_dispatch_error = 'ORDER_NOT_COMPLETED'
               WHERE id = ? AND status = 'PENDING' AND dispatch_lease_token = ?`
            )
            .bind(nextDispatchAt, event.id, leaseToken)
            .run();
          continue;
        }

        // Find user chat ID
        const userSession = await this.db
          .prepare('SELECT chat_id FROM telegram_sessions WHERE user_id = ? ORDER BY updated_at DESC LIMIT 1')
          .bind(order.user_id)
          .first<{ chat_id: number }>();

        const userRecord = userSession || (await this.db
          .prepare('SELECT id as chat_id FROM telegram_users WHERE id = ?')
          .bind(order.user_id)
          .first<{ chat_id: number }>());

        if (!userRecord || !userRecord.chat_id) {
          failureCount++;
          const attempts = event.dispatch_attempts + 1;
          const backoffSeconds = Math.min(300, 5 * Math.pow(2, attempts - 1));
          const nextDispatchAt = Date.now() + backoffSeconds * 1000;

          await this.db
            .prepare(
              `UPDATE outbox_events
               SET dispatch_lease_token = NULL,
                   dispatch_leased_at = NULL,
                   dispatch_lease_expires_at = NULL,
                   next_dispatch_at = ?,
                   last_dispatch_error = 'TELEGRAM_USER_NOT_FOUND'
               WHERE id = ? AND status = 'PENDING' AND dispatch_lease_token = ?`
            )
            .bind(nextDispatchAt, event.id, leaseToken)
            .run();
          continue;
        }

        const botToken = this.env?.TELEGRAM_BOT_TOKEN;
        const bucket = this.env?.ARTIFACTS_BUCKET;

        if (!botToken || !bucket) {
          failureCount++;
          const attempts = event.dispatch_attempts + 1;
          const backoffSeconds = Math.min(300, 5 * Math.pow(2, attempts - 1));
          const nextDispatchAt = Date.now() + backoffSeconds * 1000;

          await this.db
            .prepare(
              `UPDATE outbox_events
               SET dispatch_lease_token = NULL,
                   dispatch_leased_at = NULL,
                   dispatch_lease_expires_at = NULL,
                   next_dispatch_at = ?,
                   last_dispatch_error = 'MISSING_DELIVERY_RESOURCES'
               WHERE id = ? AND status = 'PENDING' AND dispatch_lease_token = ?`
            )
            .bind(nextDispatchAt, event.id, leaseToken)
            .run();
          continue;
        }

        const receipt = await this.db
          .prepare(
            'SELECT artifact_key, artifact_sha256, artifact_size_bytes, artifact_parts FROM fulfillment_receipts WHERE order_id = ?'
          )
          .bind(order.id)
          .first<{
            artifact_key: string;
            artifact_sha256: string;
            artifact_size_bytes: number;
            artifact_parts?: string | null;
          }>();

        if (!receipt) {
          failureCount++;
          const attempts = event.dispatch_attempts + 1;
          const backoffSeconds = Math.min(300, 5 * Math.pow(2, attempts - 1));
          const nextDispatchAt = Date.now() + backoffSeconds * 1000;

          await this.db
            .prepare(
              `UPDATE outbox_events
               SET dispatch_lease_token = NULL,
                   dispatch_leased_at = NULL,
                   dispatch_lease_expires_at = NULL,
                   next_dispatch_at = ?,
                   last_dispatch_error = 'RECEIPT_NOT_FOUND'
               WHERE id = ? AND status = 'PENDING' AND dispatch_lease_token = ?`
            )
            .bind(nextDispatchAt, event.id, leaseToken)
            .run();
          continue;
        }

        try {
          let familyName = 'Fonts';
          if (order.metadata) {
            try {
              const meta = JSON.parse(order.metadata) as { family_name?: string };
              if (meta.family_name) familyName = meta.family_name;
            } catch {
              // fallback to default
            }
          }

          // 1. Determine parts list
          let parts: ArtifactPartMeta[];
          if (receipt.artifact_parts) {
            try {
              parts = JSON.parse(receipt.artifact_parts) as ArtifactPartMeta[];
            } catch {
              parts = [];
            }
          } else {
            const safeFamily = (familyName || 'fonts').replace(/[^a-zA-Z0-9_-]/g, '_').toLowerCase();
            parts = [
              {
                part_index: 1,
                total_parts: 1,
                filename: `${safeFamily}_${order.id}.zip`,
                artifact_key: receipt.artifact_key,
                artifact_size_bytes: receipt.artifact_size_bytes,
                artifact_sha256: receipt.artifact_sha256,
              },
            ];
          }

          if (!parts || parts.length === 0) {
            throw new Error('NO_PARTS_IN_RECEIPT');
          }

          // Sort deterministically by part_index ascending
          parts.sort((a, b) => a.part_index - b.part_index);

          // 2. Preflight check: verify EVERY canonical R2 part exists and matches recorded size/hash before upload without retaining bodies
          for (const part of parts) {
            const r2Obj = await bucket.get(part.artifact_key);
            if (!r2Obj) {
              throw new Error(`R2_PART_NOT_FOUND: ${part.artifact_key}`);
            }
            if (r2Obj.size !== part.artifact_size_bytes) {
              throw new Error(
                `R2_PART_SIZE_MISMATCH: expected ${part.artifact_size_bytes}, got ${r2Obj.size}`
              );
            }
            const arrayBuf = await r2Obj.arrayBuffer();
            if (arrayBuf.byteLength !== part.artifact_size_bytes) {
              throw new Error(
                `R2_PART_SIZE_MISMATCH: expected ${part.artifact_size_bytes}, got ${arrayBuf.byteLength}`
              );
            }
            const shaBuf = await crypto.subtle.digest('SHA-256', arrayBuf);
            const shaHex = Array.from(new Uint8Array(shaBuf))
              .map((b) => b.toString(16).padStart(2, '0'))
              .join('');
            if (shaHex !== part.artifact_sha256.toLowerCase()) {
              throw new Error(
                `R2_PART_CHECKSUM_MISMATCH: expected ${part.artifact_sha256}, got ${shaHex}`
              );
            }
            // Note: arrayBuf is not stored in any collection; eligible for immediate GC before next part
          }

          // 3. Progressive delivery with per-part confirmed progress tracking: load and send at most one part body at a time
          let payloadObj: { order_id: string; confirmed_parts?: number[] } = {
            order_id: order.id,
            confirmed_parts: [],
          };
          try {
            if (event.payload) {
              payloadObj = JSON.parse(event.payload);
            }
          } catch {
            // fallback to default
          }
          const confirmedParts: number[] = Array.isArray(payloadObj.confirmed_parts)
            ? [...payloadObj.confirmed_parts]
            : [];

          const tg = new TelegramClient(botToken);

          for (const part of parts) {
            if (confirmedParts.includes(part.part_index)) {
              // Already confirmed by Telegram in earlier attempt
              continue;
            }

            const r2Obj = await bucket.get(part.artifact_key);
            if (!r2Obj) {
              throw new Error(`R2_PART_NOT_FOUND_DURING_DELIVERY: ${part.artifact_key}`);
            }
            const partBuffer = await r2Obj.arrayBuffer();

            const caption =
              parts.length > 1
                ? `📦 <b>${escapeHtml(familyName)}</b> (Part ${part.part_index}/${part.total_parts})`
                : `📦 <b>${escapeHtml(familyName)}</b>`;

            await tg.sendDocument({
              chat_id: userRecord.chat_id,
              document: new Blob([partBuffer], { type: 'application/zip' }),
              filename: part.filename,
              caption,
            });

            confirmedParts.push(part.part_index);

            // Persist per-part progress so that partial failure won't re-send confirmed parts on retry
            await this.db
              .prepare('UPDATE outbox_events SET payload = ? WHERE id = ?')
              .bind(JSON.stringify({ order_id: order.id, confirmed_parts: confirmedParts }), event.id)
              .run();
          }

          // 4. Mark outbox event SENT only after every part is confirmed
          const markSentResult = await this.db
            .prepare(
              `UPDATE outbox_events
               SET status = 'SENT',
                   dispatched_at = ?,
                   dispatch_lease_token = NULL,
                   dispatch_leased_at = NULL,
                   dispatch_lease_expires_at = NULL,
                   last_dispatch_error = NULL
               WHERE id = ? AND status = 'PENDING' AND dispatch_lease_token = ?`
            )
            .bind(Date.now(), event.id, leaseToken)
            .run();

          if (markSentResult.meta.changes && markSentResult.meta.changes > 0) {
            dispatchedCount++;
            emitStructuredLog({
              event: 'telegram_delivered',
              order_id: order.id,
              chat_id: userRecord.chat_id,
              event_id: event.id,
              total_parts: parts.length,
            });
            emitStructuredLog({
              event: 'outbox_dispatched',
              event_id: event.id,
              event_type: event.event_type,
              aggregate_id: event.aggregate_id,
              attempt: event.dispatch_attempts + 1,
              status: 'SENT',
            });
          }
        } catch (err: unknown) {
          failureCount++;
          const errorMessage = err instanceof Error ? err.message : 'TELEGRAM_DELIVERY_FAILED';
          const attempts = event.dispatch_attempts + 1;
          const backoffSeconds = Math.min(300, 5 * Math.pow(2, attempts - 1));
          const nextDispatchAt = Date.now() + backoffSeconds * 1000;

          await this.db
            .prepare(
              `UPDATE outbox_events
               SET dispatch_lease_token = NULL,
                   dispatch_leased_at = NULL,
                   dispatch_lease_expires_at = NULL,
                   next_dispatch_at = ?,
                   last_dispatch_error = ?
               WHERE id = ? AND status = 'PENDING' AND dispatch_lease_token = ?`
            )
            .bind(nextDispatchAt, errorMessage, event.id, leaseToken)
            .run();
        }
      }
    }

    return { dispatchedCount, failureCount };
  }
}

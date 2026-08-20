import type { Env } from '../env';
import { generateSignedDownloadUrl, getDownloadTtlSeconds } from '../utils/download-signer';
import { TelegramClient } from './telegram-client';
import { escapeHtml } from '../utils/html';

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
          .prepare('SELECT id, user_id, status FROM orders WHERE id = ?')
          .bind(orderId)
          .first<{ id: string; user_id: string; status: string }>();

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
        const signingSecret = this.env?.DOWNLOAD_SIGNING_SECRET;

        if (!botToken || !signingSecret) {
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
                   last_dispatch_error = 'MISSING_DELIVERY_SECRETS'
               WHERE id = ? AND status = 'PENDING' AND dispatch_lease_token = ?`
            )
            .bind(nextDispatchAt, event.id, leaseToken)
            .run();
          continue;
        }

        try {
          const ttlSeconds = getDownloadTtlSeconds(this.env?.DOWNLOAD_URL_TTL_SECONDS);
          const signed = await generateSignedDownloadUrl(order.id, signingSecret, {
            baseUrl: this.env?.BASE_URL,
            ttlSeconds,
          });

          const tg = new TelegramClient(botToken);
          const messageText = `📦 <b>Your fonts are ready!</b>\n\n• <b>Order ID:</b> <code>${escapeHtml(
            order.id
          )}</code>\n\nClick the button below to download your complete ZIP bundle. Link is active for 24 hours.`;

          await tg.sendMessage({
            chat_id: userRecord.chat_id,
            text: messageText,
            reply_markup: {
              inline_keyboard: [
                [
                  {
                    text: '⬇️ Download Fonts (.ZIP)',
                    url: signed.url,
                  },
                ],
              ],
            },
          });

          // Mark outbox event SENT only after Telegram success
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
                   last_dispatch_error = 'TELEGRAM_DELIVERY_FAILED'
               WHERE id = ? AND status = 'PENDING' AND dispatch_lease_token = ?`
            )
            .bind(nextDispatchAt, event.id, leaseToken)
            .run();
        }
      }
    }

    return { dispatchedCount, failureCount };
  }
}

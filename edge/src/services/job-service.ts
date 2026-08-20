export interface FulfillmentJobRecord {
  id: string;
  order_id: string;
  status: 'PENDING' | 'PROCESSING' | 'RETRY' | 'COMPLETED' | 'FAILED';
  leased_at: number | null;
  lease_expires_at: number | null;
  lease_owner: string | null;
  lease_token: string | null;
  attempt_count: number;
  max_attempts: number;
  next_retry_at: number | null;
  last_error: string | null;
  created_at: number;
  updated_at: number;
}

export interface ClaimComputePayload {
  job_id: string;
  order_id: string;
  lease_token: string;
  lease_expires_at: number;
  source_url: string;
  family_name?: string;
  foundry?: string;
  styles: Array<{ id: string; display_name: string }>;
  formats: string[];
}

export interface ClaimJobResult {
  status: 'CLAIMED' | 'NOT_FOUND' | 'TERMINAL' | 'LEASED' | 'RETRY_NOT_DUE' | 'CONFLICT' | 'MALFORMED_METADATA';
  queue_action?: 'ack' | 'retry';
  reason?: string;
  payload?: ClaimComputePayload;
}

export interface HeartbeatResult {
  status: 'EXTENDED' | 'EXPIRED_OR_FENCED' | 'NOT_FOUND';
  lease_expires_at?: number;
  queue_action?: 'ack' | 'retry';
}

export interface FailJobResult {
  status: 'RETRY' | 'FAILED' | 'EXPIRED_OR_FENCED' | 'NOT_FOUND';
  queue_action: 'ack' | 'retry';
  delay_seconds?: number;
  next_retry_at?: number;
  reason?: string;
}

const ALLOWED_FORMATS = new Set(['TTF', 'OTF', 'WOFF', 'WOFF2']);

export class JobService {
  constructor(private readonly db: D1Database) {}

  async getJobById(jobId: string): Promise<FulfillmentJobRecord | null> {
    return this.db
      .prepare('SELECT * FROM fulfillment_jobs WHERE id = ?')
      .bind(jobId)
      .first<FulfillmentJobRecord>();
  }

  async claimJob(
    jobId: string,
    workerId: string,
    leaseDurationSeconds = 300
  ): Promise<ClaimJobResult> {
    // 1. Enforce bounded safe identifier for worker_id (BLOCK 6)
    const cleanWorkerId = workerId.trim();
    if (!/^[a-zA-Z0-9_-]{1,64}$/.test(cleanWorkerId)) {
      return { status: 'CONFLICT', queue_action: 'retry', reason: 'invalid_worker_id' };
    }

    const boundedLeaseSeconds = Math.max(10, Math.min(leaseDurationSeconds, 1800));

    const job = await this.getJobById(jobId);
    if (!job) {
      return { status: 'NOT_FOUND', queue_action: 'ack', reason: 'job_not_found' };
    }

    const now = Date.now();

    // Check if terminal
    if (job.status === 'COMPLETED' || job.status === 'FAILED') {
      return {
        status: 'TERMINAL',
        queue_action: 'ack',
        reason: `job_${job.status.toLowerCase()}`,
      };
    }

    // Check if attempt count exhausted (BLOCK 5)
    if (job.attempt_count >= job.max_attempts) {
      // If previous worker crashed on final attempt, terminalize atomically before returning ACK
      if (
        job.status === 'PROCESSING' &&
        (job.lease_expires_at === null || job.lease_expires_at <= now)
      ) {
        await this.db.batch([
          this.db
            .prepare(
              `UPDATE fulfillment_jobs
               SET status = 'FAILED',
                   lease_owner = NULL,
                   lease_token = NULL,
                   leased_at = NULL,
                   lease_expires_at = NULL,
                   last_error = 'max_attempts_exhausted',
                   updated_at = ?
               WHERE id = ?
                 AND status = 'PROCESSING'
                 AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                 AND attempt_count >= max_attempts`
            )
            .bind(now, jobId, now),
          this.db
            .prepare(
              `UPDATE orders
               SET status = 'FAILED', updated_at = ?
               WHERE id = (
                 SELECT order_id FROM fulfillment_jobs
                 WHERE id = ? AND status = 'FAILED' AND last_error = 'max_attempts_exhausted' AND updated_at = ?
               )
               AND status = 'PROCESSING'`
            )
            .bind(now, jobId, now),
        ]);
      }

      return {
        status: 'TERMINAL',
        queue_action: 'ack',
        reason: 'max_attempts_exhausted',
      };
    }

    // Check if actively leased by another worker
    if (job.status === 'PROCESSING' && job.lease_expires_at && job.lease_expires_at > now) {
      return {
        status: 'LEASED',
        queue_action: 'retry',
        reason: 'job_currently_leased',
      };
    }

    // Check if in retry delay
    if (job.status === 'RETRY' && job.next_retry_at && job.next_retry_at > now) {
      return {
        status: 'RETRY_NOT_DUE',
        queue_action: 'retry',
        reason: 'retry_backoff_active',
      };
    }

    // CAS transition to PROCESSING with fresh lease token & atomic order binding (BLOCK 3)
    const newLeaseToken = crypto.randomUUID();
    const leaseExpiresAt = now + boundedLeaseSeconds * 1000;

    const statements: D1PreparedStatement[] = [
      // 1. Update job with optimistic fencing AND order precondition check
      this.db
        .prepare(
          `UPDATE fulfillment_jobs
           SET status = 'PROCESSING',
               lease_owner = ?,
               lease_token = ?,
               leased_at = ?,
               lease_expires_at = ?,
               attempt_count = attempt_count + 1,
               next_retry_at = NULL,
               last_error = NULL,
               updated_at = ?
           WHERE id = ?
             AND (
               (status = 'PENDING' AND EXISTS (SELECT 1 FROM orders WHERE id = fulfillment_jobs.order_id AND status = 'PAID')) OR
               (status = 'RETRY' AND (next_retry_at IS NULL OR next_retry_at <= ?) AND EXISTS (SELECT 1 FROM orders WHERE id = fulfillment_jobs.order_id AND status = 'PROCESSING')) OR
               (status = 'PROCESSING' AND (lease_expires_at IS NOT NULL AND lease_expires_at <= ?) AND EXISTS (SELECT 1 FROM orders WHERE id = fulfillment_jobs.order_id AND status = 'PROCESSING'))
             )
             AND attempt_count < max_attempts`
        )
        .bind(cleanWorkerId, newLeaseToken, now, leaseExpiresAt, now, jobId, now, now),

      // 2. Transition order PAID -> PROCESSING atomically bound to successful job lease
      this.db
        .prepare(
          `UPDATE orders
           SET status = 'PROCESSING', updated_at = ?
           WHERE id = (
             SELECT order_id FROM fulfillment_jobs
             WHERE id = ?
               AND status = 'PROCESSING'
               AND lease_token = ?
               AND lease_owner = ?
           )
           AND status = 'PAID'`
        )
        .bind(now, jobId, newLeaseToken, cleanWorkerId),
    ];

    try {
      const results = await this.db.batch(statements);
      const jobUpdateResult = results[0];

      if (!jobUpdateResult.meta.changes || jobUpdateResult.meta.changes === 0) {
        return {
          status: 'CONFLICT',
          queue_action: 'retry',
          reason: 'claim_cas_lost',
        };
      }
    } catch {
      return {
        status: 'CONFLICT',
        queue_action: 'retry',
        reason: 'claim_transaction_error',
      };
    }

    // Fetch and defensively validate compute payload (BLOCK 6)
    const order = await this.db
      .prepare('SELECT id, metadata FROM orders WHERE id = ?')
      .bind(job.order_id)
      .first<{ id: string; metadata: string | null }>();

    if (!order) {
      return { status: 'MALFORMED_METADATA', queue_action: 'retry', reason: 'order_not_found' };
    }

    let sourceUrl = '';
    let familyName: string | undefined;
    let foundry: string | undefined;
    let formats: string[] = [];

    try {
      const parsedMeta = JSON.parse(order.metadata || '{}') as Record<string, unknown>;
      if (!parsedMeta || typeof parsedMeta !== 'object' || Array.isArray(parsedMeta)) {
        throw new Error('invalid_metadata_type');
      }

      // Must be canonical MyFonts HTTPS URL (BLOCK B)
      if (
        typeof parsedMeta.source_url !== 'string' ||
        !/^https:\/\/(www\.)?myfonts\.com\/[a-zA-Z0-9_\-\/]+$/.test(parsedMeta.source_url.trim())
      ) {
        throw new Error('invalid_source_url');
      }
      sourceUrl = parsedMeta.source_url.trim();

      if (typeof parsedMeta.family_name === 'string' && parsedMeta.family_name.trim()) {
        familyName = parsedMeta.family_name.trim().slice(0, 128);
      }
      if (typeof parsedMeta.foundry === 'string' && parsedMeta.foundry.trim()) {
        foundry = parsedMeta.foundry.trim().slice(0, 128);
      }

      // Formats must be non-empty array with only supported values (no fallback default!) (BLOCK B)
      if (!Array.isArray(parsedMeta.selected_formats) || parsedMeta.selected_formats.length === 0) {
        throw new Error('missing_or_empty_formats');
      }

      const validatedFormats: string[] = [];
      for (const f of parsedMeta.selected_formats) {
        if (typeof f !== 'string') {
          throw new Error('non_string_format');
        }
        const upper = f.trim().toUpperCase();
        if (!ALLOWED_FORMATS.has(upper)) {
          throw new Error(`unsupported_format_${upper}`);
        }
        if (!validatedFormats.includes(upper)) {
          validatedFormats.push(upper);
        }
      }

      if (validatedFormats.length === 0) {
        throw new Error('no_valid_formats');
      }
      formats = validatedFormats;
    } catch {
      return {
        status: 'MALFORMED_METADATA',
        queue_action: 'retry',
        reason: 'invalid_order_metadata_json',
      };
    }

    const items = await this.db
      .prepare('SELECT font_id, font_name FROM order_items WHERE order_id = ? ORDER BY created_at ASC')
      .bind(job.order_id)
      .all<{ font_id: string; font_name: string | null }>();

    if (!items.results || items.results.length === 0) {
      return {
        status: 'MALFORMED_METADATA',
        queue_action: 'retry',
        reason: 'no_order_items',
      };
    }

    const styles: Array<{ id: string; display_name: string }> = [];
    for (const i of items.results) {
      if (typeof i.font_id !== 'string' || !i.font_id.trim()) {
        return {
          status: 'MALFORMED_METADATA',
          queue_action: 'retry',
          reason: 'invalid_font_id_in_order_items',
        };
      }
      styles.push({
        id: i.font_id.trim(),
        display_name: (i.font_name && i.font_name.trim()) || i.font_id.trim(),
      });
    }

    return {
      status: 'CLAIMED',
      payload: {
        job_id: jobId,
        order_id: job.order_id,
        lease_token: newLeaseToken,
        lease_expires_at: leaseExpiresAt,
        source_url: sourceUrl,
        family_name: familyName,
        foundry,
        styles,
        formats,
      },
    };
  }

  async heartbeat(
    jobId: string,
    workerId: string,
    leaseToken: string,
    extendSeconds = 300
  ): Promise<HeartbeatResult> {
    const cleanWorkerId = workerId.trim();
    const cleanToken = leaseToken.trim();

    if (
      !/^[a-zA-Z0-9_-]{1,64}$/.test(cleanWorkerId) ||
      !/^[0-9a-fA-F-]{36}$/.test(cleanToken)
    ) {
      return { status: 'EXPIRED_OR_FENCED', queue_action: 'ack' };
    }

    const boundedExtend = Math.max(10, Math.min(extendSeconds, 1800));
    const now = Date.now();
    const newExpiresAt = now + boundedExtend * 1000;

    const result = await this.db
      .prepare(
        `UPDATE fulfillment_jobs
         SET lease_expires_at = ?, updated_at = ?
         WHERE id = ?
           AND status = 'PROCESSING'
           AND lease_owner = ?
           AND lease_token = ?
           AND lease_expires_at > ?`
      )
      .bind(newExpiresAt, now, jobId, cleanWorkerId, cleanToken, now)
      .run();

    if (result.meta.changes && result.meta.changes > 0) {
      return { status: 'EXTENDED', lease_expires_at: newExpiresAt };
    }

    return { status: 'EXPIRED_OR_FENCED', queue_action: 'ack' };
  }

  async failJob(params: {
    jobId: string;
    workerId: string;
    leaseToken: string;
    retryable: boolean;
    reasonCode?: string;
  }): Promise<FailJobResult> {
    const { jobId, workerId, leaseToken, retryable, reasonCode } = params;
    const cleanWorkerId = workerId.trim();
    const cleanToken = leaseToken.trim();

    if (
      !/^[a-zA-Z0-9_-]{1,64}$/.test(cleanWorkerId) ||
      !/^[0-9a-fA-F-]{36}$/.test(cleanToken)
    ) {
      return { status: 'EXPIRED_OR_FENCED', queue_action: 'ack', reason: 'invalid_credentials' };
    }

    const cleanReason = (reasonCode && /^[A-Z0-9_]{1,64}$/.test(reasonCode.trim()))
      ? reasonCode.trim()
      : 'UNSPECIFIED_FAILURE';

    const now = Date.now();

    // 1. Authoritative D1 check: must be active non-expired lease (BLOCK 4)
    const job = await this.db
      .prepare(
        `SELECT * FROM fulfillment_jobs
         WHERE id = ?
           AND status = 'PROCESSING'
           AND lease_owner = ?
           AND lease_token = ?
           AND lease_expires_at > ?`
      )
      .bind(jobId, cleanWorkerId, cleanToken, now)
      .first<FulfillmentJobRecord>();

    if (!job) {
      return { status: 'EXPIRED_OR_FENCED', queue_action: 'ack', reason: 'lease_superseded_or_expired' };
    }

    // 2. Retryable failure with attempts remaining
    if (retryable && job.attempt_count < job.max_attempts) {
      const backoffSeconds = Math.min(300, 10 * Math.pow(2, job.attempt_count - 1));
      const nextRetryAt = now + backoffSeconds * 1000;

      const result = await this.db
        .prepare(
          `UPDATE fulfillment_jobs
           SET status = 'RETRY',
               lease_owner = NULL,
               lease_token = NULL,
               leased_at = NULL,
               lease_expires_at = NULL,
               next_retry_at = ?,
               last_error = ?,
               updated_at = ?
           WHERE id = ?
             AND status = 'PROCESSING'
             AND lease_owner = ?
             AND lease_token = ?
             AND lease_expires_at > ?`
        )
        .bind(nextRetryAt, cleanReason, now, jobId, cleanWorkerId, cleanToken, now)
        .run();

      if (result.meta.changes && result.meta.changes > 0) {
        return {
          status: 'RETRY',
          queue_action: 'retry',
          delay_seconds: backoffSeconds,
          next_retry_at: nextRetryAt,
          reason: cleanReason,
        };
      }

      return { status: 'EXPIRED_OR_FENCED', queue_action: 'ack' };
    }

    // 3. Terminal failure or exhausted attempts: atomic job + order FAILED transition (BLOCK 4)
    const terminalReason =
      job.attempt_count >= job.max_attempts ? 'max_attempts_exhausted' : cleanReason;

    const statements: D1PreparedStatement[] = [
      // Update job conditional on active lease
      this.db
        .prepare(
          `UPDATE fulfillment_jobs
           SET status = 'FAILED',
               lease_owner = NULL,
               lease_token = NULL,
               leased_at = NULL,
               lease_expires_at = NULL,
               last_error = ?,
               updated_at = ?
           WHERE id = ?
             AND status = 'PROCESSING'
             AND lease_owner = ?
             AND lease_token = ?
             AND lease_expires_at > ?`
        )
        .bind(terminalReason, now, jobId, cleanWorkerId, cleanToken, now),

      // Update order conditional on job FAILED transition above
      this.db
        .prepare(
          `UPDATE orders
           SET status = 'FAILED', updated_at = ?
           WHERE id = (
             SELECT order_id FROM fulfillment_jobs
             WHERE id = ?
               AND status = 'FAILED'
               AND last_error = ?
               AND updated_at = ?
           )
           AND status = 'PROCESSING'`
        )
        .bind(now, jobId, terminalReason, now),
    ];

    const results = await this.db.batch(statements);
    if (results[0].meta.changes && results[0].meta.changes > 0) {
      return {
        status: 'FAILED',
        queue_action: 'ack',
        reason: terminalReason,
      };
    }

    return { status: 'EXPIRED_OR_FENCED', queue_action: 'ack' };
  }
}

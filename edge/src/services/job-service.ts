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
    const cleanWorkerId = workerId.trim();
    if (!cleanWorkerId || cleanWorkerId.length > 128) {
      return { status: 'CONFLICT', queue_action: 'retry', reason: 'invalid_worker_id' };
    }

    const job = await this.getJobById(jobId);
    if (!job) {
      return { status: 'NOT_FOUND', queue_action: 'ack', reason: 'job_not_found' };
    }

    const now = Date.now();

    // Check if terminal or max attempts exhausted
    if (
      job.status === 'COMPLETED' ||
      job.status === 'FAILED' ||
      job.attempt_count >= job.max_attempts
    ) {
      return {
        status: 'TERMINAL',
        queue_action: 'ack',
        reason:
          job.attempt_count >= job.max_attempts ? 'max_attempts_exhausted' : `job_${job.status.toLowerCase()}`,
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

    // CAS transition to PROCESSING with fresh lease token
    const newLeaseToken = crypto.randomUUID();
    const leaseExpiresAt = now + leaseDurationSeconds * 1000;

    const statements: D1PreparedStatement[] = [
      // 1. Update job with optimistic fencing
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
               (status = 'PENDING') OR
               (status = 'RETRY' AND (next_retry_at IS NULL OR next_retry_at <= ?)) OR
               (status = 'PROCESSING' AND (lease_expires_at IS NOT NULL AND lease_expires_at <= ?))
             )
             AND attempt_count < max_attempts`
        )
        .bind(cleanWorkerId, newLeaseToken, now, leaseExpiresAt, now, jobId, now, now),

      // 2. Transition order PAID -> PROCESSING atomically on first claim
      this.db
        .prepare(
          `UPDATE orders
           SET status = 'PROCESSING', updated_at = ?
           WHERE id = ? AND status = 'PAID'`
        )
        .bind(now, job.order_id),
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

    // Fetch compute payload from order and order_items
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
    let formats: string[] = ['TTF'];

    try {
      const parsedMeta = JSON.parse(order.metadata || '{}');
      sourceUrl = parsedMeta.source_url || '';
      familyName = parsedMeta.family_name;
      foundry = parsedMeta.foundry;
      formats = Array.isArray(parsedMeta.selected_formats)
        ? parsedMeta.selected_formats
        : ['TTF'];
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

    const styles = items.results.map((i) => ({
      id: i.font_id,
      display_name: i.font_name || i.font_id,
    }));

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
    const now = Date.now();
    const newExpiresAt = now + extendSeconds * 1000;

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
    const cleanReason = (reasonCode || 'unspecified_failure').slice(0, 64);
    const now = Date.now();

    const job = await this.getJobById(jobId);
    if (!job) {
      return { status: 'NOT_FOUND', queue_action: 'ack', reason: 'job_not_found' };
    }

    // Verify active lease ownership (fencing check)
    if (
      job.status !== 'PROCESSING' ||
      job.lease_owner !== cleanWorkerId ||
      job.lease_token !== cleanToken
    ) {
      return { status: 'EXPIRED_OR_FENCED', queue_action: 'ack', reason: 'lease_superseded_or_expired' };
    }

    // Retryable failure with attempts remaining
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
           WHERE id = ? AND lease_token = ?`
        )
        .bind(nextRetryAt, cleanReason, now, jobId, cleanToken)
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

    // Terminal failure or exhausted attempts
    const terminalReason =
      job.attempt_count >= job.max_attempts ? 'max_attempts_exhausted' : cleanReason;

    const statements: D1PreparedStatement[] = [
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
           WHERE id = ? AND lease_token = ?`
        )
        .bind(terminalReason, now, jobId, cleanToken),

      this.db
        .prepare(
          `UPDATE orders
           SET status = 'FAILED', updated_at = ?
           WHERE id = ? AND status = 'PROCESSING'`
        )
        .bind(now, job.order_id),
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

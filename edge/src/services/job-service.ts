export interface ArtifactPartMeta {
  part_index: number;
  total_parts: number;
  filename: string;
  artifact_key: string;
  artifact_size_bytes: number;
  artifact_sha256: string;
}

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
  artifact_key?: string | null;
  artifact_sha256?: string | null;
  artifact_size_bytes?: number | null;
  artifact_parts?: string | null;
  completed_at?: number | null;
  created_at: number;
  updated_at: number;
}

export interface FulfillmentReceiptRecord {
  job_id: string;
  order_id: string;
  artifact_key: string;
  artifact_sha256: string;
  artifact_size_bytes: number;
  artifact_parts?: string | null;
  completed_at: number;
  created_at: number;
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

export interface CompleteJobResult {
  status: 'COMPLETED' | 'ALREADY_COMPLETED' | 'CONFLICT_DIFFERENT_ARTIFACT' | 'EXPIRED_OR_FENCED' | 'NOT_FOUND' | 'ERROR';
  queue_action: 'ack' | 'retry';
  completed_at?: number;
  artifact_key?: string;
  reason?: string;
}

export type QueueRearmStatus = 'REARMED' | 'ALREADY_REARMED' | 'CONFLICT';

export interface QueueRearmResult {
  status: QueueRearmStatus;
}

export type ExactJobRescueStatus = 'RESCUED' | 'CONFLICT';

export interface ExactJobRescueResult {
  status: ExactJobRescueStatus;
}

export interface ExactJobRescueParams {
  jobId: string;
  orderId: string;
  outboxId: string;
  paymentId: string;
  paymentTransactionId: string;
  paymentCode: string;
  leaseOwner: string;
  leaseExpiresAt: number;
  attemptCount: number;
  maxAttempts: number;
  lastError: string;
  dispatchAttempts: number;
}

export type TerminalJobRecoveryStatus = 'RECOVERED' | 'ALREADY_RECOVERED' | 'CONFLICT';

export interface TerminalJobRecoveryResult {
  status: TerminalJobRecoveryStatus;
}

export interface TerminalJobRecoveryParams {
  jobId: string;
  orderId: string;
  outboxId: string;
  paymentId: string;
  paymentTransactionId: string;
  paymentCode: string;
  attemptCount: number;
  maxAttempts: number;
  lastError: string;
  dispatchAttempts: number;
}

export interface FinalizeExpiredJobsResult {
  finalizedJobs: number;
  transitionedOrders: number;
}

export interface QueueRearmParams {
  jobId: string;
  orderId: string;
  outboxId: string;
  leaseOwner: string;
  leaseExpiresAt: number;
  attemptCount: number;
  dispatchAttempts: number;
}

export const QUEUE_REARM_AUDIT_REASON = 'QUEUE_RETENTION_EXPIRED_RECOVERY';
export const MAX_ATTEMPTS_EXHAUSTED_REASON = 'max_attempts_exhausted';
export const MAX_ATTEMPTS_EXHAUSTED_RESCUE_REASON = 'MAX_ATTEMPTS_EXHAUSTED_RESCUE';
export const TERMINAL_FAILED_RECOVERY_REASON = 'TERMINAL_FAILED_RECOVERY';

const SAFE_INTERNAL_ID = /^[a-zA-Z0-9_-]{1,64}$/;
const SAFE_LINEAGE_ID = /^[a-zA-Z0-9_.:-]{1,128}$/;
const MAX_REARM_ATTEMPT_COUNT = 1000;
const MAX_REARM_DISPATCH_ATTEMPTS = 1_000_000;

function validQueueRearmParams(params: QueueRearmParams): boolean {
  return (
    SAFE_INTERNAL_ID.test(params.jobId) &&
    SAFE_INTERNAL_ID.test(params.orderId) &&
    SAFE_INTERNAL_ID.test(params.outboxId) &&
    SAFE_INTERNAL_ID.test(params.leaseOwner) &&
    Number.isSafeInteger(params.leaseExpiresAt) &&
    params.leaseExpiresAt >= 0 &&
    Number.isInteger(params.attemptCount) &&
    params.attemptCount >= 0 &&
    params.attemptCount <= MAX_REARM_ATTEMPT_COUNT &&
    Number.isInteger(params.dispatchAttempts) &&
    params.dispatchAttempts >= 0 &&
    params.dispatchAttempts <= MAX_REARM_DISPATCH_ATTEMPTS
  );
}

function validExactJobRescueParams(params: ExactJobRescueParams): boolean {
  return (
    SAFE_INTERNAL_ID.test(params.jobId) &&
    SAFE_INTERNAL_ID.test(params.orderId) &&
    SAFE_INTERNAL_ID.test(params.outboxId) &&
    SAFE_INTERNAL_ID.test(params.paymentId) &&
    SAFE_LINEAGE_ID.test(params.paymentTransactionId) &&
    SAFE_INTERNAL_ID.test(params.paymentCode) &&
    SAFE_INTERNAL_ID.test(params.leaseOwner) &&
    Number.isSafeInteger(params.leaseExpiresAt) &&
    params.leaseExpiresAt >= 0 &&
    Number.isInteger(params.attemptCount) &&
    params.attemptCount >= 0 &&
    params.attemptCount <= MAX_REARM_ATTEMPT_COUNT &&
    Number.isInteger(params.maxAttempts) &&
    params.maxAttempts > 0 &&
    params.maxAttempts < MAX_REARM_ATTEMPT_COUNT &&
    params.attemptCount === params.maxAttempts &&
    params.lastError === MAX_ATTEMPTS_EXHAUSTED_REASON &&
    Number.isInteger(params.dispatchAttempts) &&
    params.dispatchAttempts >= 0 &&
    params.dispatchAttempts <= MAX_REARM_DISPATCH_ATTEMPTS
  );
}

function validTerminalJobRecoveryParams(params: TerminalJobRecoveryParams): boolean {
  return (
    SAFE_INTERNAL_ID.test(params.jobId) &&
    SAFE_INTERNAL_ID.test(params.orderId) &&
    SAFE_INTERNAL_ID.test(params.outboxId) &&
    SAFE_INTERNAL_ID.test(params.paymentId) &&
    SAFE_LINEAGE_ID.test(params.paymentTransactionId) &&
    SAFE_INTERNAL_ID.test(params.paymentCode) &&
    Number.isInteger(params.attemptCount) &&
    params.attemptCount >= 0 &&
    params.attemptCount <= MAX_REARM_ATTEMPT_COUNT &&
    Number.isInteger(params.maxAttempts) &&
    params.maxAttempts > 0 &&
    params.maxAttempts < MAX_REARM_ATTEMPT_COUNT &&
    params.attemptCount === params.maxAttempts &&
    params.lastError === MAX_ATTEMPTS_EXHAUSTED_REASON &&
    Number.isInteger(params.dispatchAttempts) &&
    params.dispatchAttempts >= 0 &&
    params.dispatchAttempts <= MAX_REARM_DISPATCH_ATTEMPTS
  );
}

function queueRearmGuard(status: 'SENT' | 'PENDING'): string {
  return `
    e.id = ?
    AND e.event_type = 'JOB_READY'
    AND e.aggregate_type = 'ORDER'
    AND e.aggregate_id = ?
    AND e.status = '${status}'
    AND e.dispatch_attempts = ?
    AND CASE
      WHEN json_valid(e.payload) = 1 THEN json_extract(e.payload, '$.job_id')
      ELSE NULL
    END = ?
    AND (
      SELECT COUNT(*)
      FROM outbox_events AS sole_event
      WHERE sole_event.event_type = 'JOB_READY'
        AND sole_event.aggregate_type = 'ORDER'
        AND sole_event.aggregate_id = ?
    ) = 1
    AND EXISTS (
      SELECT 1
      FROM fulfillment_jobs AS j
      WHERE j.id = ?
        AND j.order_id = ?
        AND j.status = 'PROCESSING'
        AND j.lease_owner = ?
        AND j.lease_expires_at IS NOT NULL
        AND j.lease_expires_at = ?
        AND j.lease_expires_at < ?
        AND j.attempt_count = ?
        AND j.attempt_count < j.max_attempts
        AND j.artifact_key IS NULL
        AND j.artifact_sha256 IS NULL
        AND j.artifact_size_bytes IS NULL
        AND j.artifact_parts IS NULL
    )
    AND EXISTS (
      SELECT 1
      FROM orders AS o
      WHERE o.id = ?
        AND o.status = 'PROCESSING'
    )
    AND NOT EXISTS (
      SELECT 1
      FROM fulfillment_receipts AS r
      WHERE r.job_id = ? OR r.order_id = ?
    )
    AND NOT EXISTS (
      SELECT 1
      FROM artifacts AS a
      WHERE a.order_id = ? OR a.job_id = ?
    )
  `;
}

function queueRearmGuardBindings(params: QueueRearmParams, now: number): unknown[] {
  return [
    params.outboxId,
    params.orderId,
    params.dispatchAttempts,
    params.jobId,
    params.orderId,
    params.jobId,
    params.orderId,
    params.leaseOwner,
    params.leaseExpiresAt,
    now,
    params.attemptCount,
    params.orderId,
    params.jobId,
    params.orderId,
    params.orderId,
    params.jobId,
  ];
}

export function buildArtifactStorageKey(orderId: string, jobId: string, sha256Hex: string): string {
  const cleanOrder = orderId.trim().replace(/[^a-zA-Z0-9_-]/g, '');
  const cleanJob = jobId.trim().replace(/[^a-zA-Z0-9_-]/g, '');
  const cleanSha = sha256Hex.trim().toLowerCase().replace(/[^0-9a-f]/g, '');
  return `artifacts/${cleanOrder}/${cleanJob}/${cleanSha}.zip`;
}

const ALLOWED_FORMATS = new Set(['TTF', 'OTF']);

export class JobService {
  constructor(private readonly db: D1Database) {}

  async getJobById(jobId: string): Promise<FulfillmentJobRecord | null> {
    return this.db
      .prepare('SELECT * FROM fulfillment_jobs WHERE id = ?')
      .bind(jobId)
      .first<FulfillmentJobRecord>();
  }

  async getReceiptByJobId(jobId: string): Promise<FulfillmentReceiptRecord | null> {
    return this.db
      .prepare('SELECT * FROM fulfillment_receipts WHERE job_id = ?')
      .bind(jobId)
      .first<FulfillmentReceiptRecord>();
  }

  async getReceiptByOrderId(orderId: string): Promise<FulfillmentReceiptRecord | null> {
    return this.db
      .prepare('SELECT * FROM fulfillment_receipts WHERE order_id = ?')
      .bind(orderId)
      .first<FulfillmentReceiptRecord>();
  }

  async finalizeExpiredExhaustedJobs(
    now = Date.now(),
    batchSize = 25
  ): Promise<FinalizeExpiredJobsResult> {
    const boundedBatchSize = Math.max(1, Math.min(batchSize, 25));
    const candidates = await this.db
      .prepare(
        `SELECT id, order_id, lease_expires_at, attempt_count, max_attempts
         FROM fulfillment_jobs
         WHERE status = 'PROCESSING'
           AND lease_expires_at IS NOT NULL
           AND lease_expires_at <= ?
           AND attempt_count >= max_attempts
         ORDER BY lease_expires_at ASC
         LIMIT ?`
      )
      .bind(now, boundedBatchSize)
      .all<{
        id: string;
        order_id: string;
        lease_expires_at: number;
        attempt_count: number;
        max_attempts: number;
      }>();

    if (candidates.results.length === 0) {
      return { finalizedJobs: 0, transitionedOrders: 0 };
    }

    const statements: D1PreparedStatement[] = [];
    for (const candidate of candidates.results) {
      statements.push(
        this.db
          .prepare(
            `UPDATE fulfillment_jobs
             SET status = 'FAILED',
                 lease_token = NULL,
                 next_retry_at = NULL,
                 last_error = ?,
                 updated_at = ?
             WHERE id = ?
               AND order_id = ?
               AND status = 'PROCESSING'
               AND lease_expires_at = ?
               AND lease_expires_at <= ?
               AND attempt_count = ?
               AND max_attempts = ?
               AND attempt_count >= max_attempts`
          )
          .bind(
            MAX_ATTEMPTS_EXHAUSTED_REASON,
            now,
            candidate.id,
            candidate.order_id,
            candidate.lease_expires_at,
            now,
            candidate.attempt_count,
            candidate.max_attempts
          )
      );
      statements.push(
        this.db
          .prepare(
            `UPDATE orders
             SET status = 'FAILED', updated_at = ?
             WHERE id = ?
               AND status = 'PROCESSING'
               AND EXISTS (
                 SELECT 1
                 FROM fulfillment_jobs
                 WHERE id = ?
                   AND order_id = ?
                   AND status = 'FAILED'
                   AND last_error = ?
                   AND updated_at = ?
                   AND attempt_count = ?
                   AND max_attempts = ?
                   AND lease_expires_at = ?
               )`
          )
          .bind(
            now,
            candidate.order_id,
            candidate.id,
            candidate.order_id,
            MAX_ATTEMPTS_EXHAUSTED_REASON,
            now,
            candidate.attempt_count,
            candidate.max_attempts,
            candidate.lease_expires_at
          )
      );
    }

    const results = await this.db.batch(statements);
    let finalizedJobs = 0;
    let transitionedOrders = 0;
    for (let i = 0; i < candidates.results.length; i++) {
      if (results[i * 2].meta.changes === 1) finalizedJobs++;
      if (results[i * 2 + 1].meta.changes === 1) transitionedOrders++;
    }
    return { finalizedJobs, transitionedOrders };
  }

  async rearmExpiredQueueEvent(params: QueueRearmParams): Promise<QueueRearmResult> {
    const cleanParams: QueueRearmParams = {
      jobId: params.jobId.trim(),
      orderId: params.orderId.trim(),
      outboxId: params.outboxId.trim(),
      leaseOwner: params.leaseOwner.trim(),
      leaseExpiresAt: params.leaseExpiresAt,
      attemptCount: params.attemptCount,
      dispatchAttempts: params.dispatchAttempts,
    };

    if (!validQueueRearmParams(cleanParams)) {
      return { status: 'CONFLICT' };
    }

    // Server time is captured once and is the only expiry authority for this CAS.
    const now = Date.now();
    const guard = queueRearmGuard('SENT');
    const updateResult = await this.db
      .prepare(
        `UPDATE outbox_events AS e
         SET status = 'PENDING',
             dispatched_at = NULL,
             dispatch_lease_token = NULL,
             dispatch_leased_at = NULL,
             dispatch_lease_expires_at = NULL,
             next_dispatch_at = NULL,
             last_dispatch_error = ?
         WHERE ${guard}`
      )
      .bind(QUEUE_REARM_AUDIT_REASON, ...queueRearmGuardBindings(cleanParams, now))
      .run();

    if (updateResult.meta.changes && updateResult.meta.changes === 1) {
      return { status: 'REARMED' };
    }

    // A replay is accepted only when the exact guarded row is PENDING and carries
    // the bounded recovery marker; broad status inference is intentionally avoided.
    const alreadyRearmed = await this.db
      .prepare(
        `SELECT 1 AS matched
         FROM outbox_events AS e
         WHERE ${queueRearmGuard('PENDING')}
           AND e.dispatched_at IS NULL
           AND e.dispatch_lease_token IS NULL
           AND e.dispatch_leased_at IS NULL
           AND e.dispatch_lease_expires_at IS NULL
           AND e.next_dispatch_at IS NULL
           AND e.last_dispatch_error = ?`
      )
      .bind(...queueRearmGuardBindings(cleanParams, now), QUEUE_REARM_AUDIT_REASON)
      .first<{ matched: number }>();

    return alreadyRearmed ? { status: 'ALREADY_REARMED' } : { status: 'CONFLICT' };
  }

  async rescueExactExhaustedJob(params: ExactJobRescueParams): Promise<ExactJobRescueResult> {
    const cleanParams: ExactJobRescueParams = {
      jobId: params.jobId.trim(),
      orderId: params.orderId.trim(),
      outboxId: params.outboxId.trim(),
      paymentId: params.paymentId.trim(),
      paymentTransactionId: params.paymentTransactionId.trim(),
      paymentCode: params.paymentCode.trim(),
      leaseOwner: params.leaseOwner.trim(),
      leaseExpiresAt: params.leaseExpiresAt,
      attemptCount: params.attemptCount,
      maxAttempts: params.maxAttempts,
      lastError: params.lastError.trim(),
      dispatchAttempts: params.dispatchAttempts,
    };

    if (!validExactJobRescueParams(cleanParams)) {
      return { status: 'CONFLICT' };
    }

    const now = Date.now();
    const nextMaxAttempts = cleanParams.maxAttempts + 1;
    const statements: D1PreparedStatement[] = [
      this.db
        .prepare(
          `UPDATE fulfillment_jobs AS j
           SET status = 'RETRY',
               max_attempts = max_attempts + 1,
               next_retry_at = NULL,
               last_error = ?,
               lease_owner = NULL,
               lease_token = NULL,
               leased_at = NULL,
               lease_expires_at = NULL,
               updated_at = ?
           WHERE j.id = ?
             AND j.order_id = ?
             AND j.status = 'FAILED'
             AND j.attempt_count = ?
             AND j.max_attempts = ?
             AND j.lease_owner = ?
             AND j.lease_expires_at = ?
             AND j.lease_expires_at <= ?
             AND j.last_error = ?
             AND j.artifact_key IS NULL
             AND j.artifact_sha256 IS NULL
             AND j.artifact_size_bytes IS NULL
             AND j.artifact_parts IS NULL
             AND j.completed_at IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM fulfillment_receipts AS r
               WHERE r.job_id = j.id OR r.order_id = j.order_id
             )
             AND NOT EXISTS (
               SELECT 1 FROM artifacts AS a
               WHERE a.job_id = j.id OR a.order_id = j.order_id
             )
             AND EXISTS (
               SELECT 1 FROM orders AS o
               WHERE o.id = ?
                 AND o.status = 'FAILED'
                 AND o.payment_code = ?
             )
              AND EXISTS (
                SELECT 1
                FROM payments AS p
                JOIN orders AS payment_order ON payment_order.id = p.order_id
                WHERE p.id = ?
                  AND p.order_id = ?
                  AND payment_order.id = ?
                  AND p.provider = 'SEPAY'
                  AND p.status = 'VERIFIED'
                  AND p.transaction_id = ?
                  AND p.amount = payment_order.total_amount
                  AND p.currency = payment_order.currency
              )
             AND (
               SELECT COUNT(*) FROM payments
               WHERE order_id = ? AND provider = 'SEPAY' AND status = 'VERIFIED'
             ) = 1
             AND EXISTS (
               SELECT 1 FROM outbox_events AS e
               WHERE e.id = ?
                 AND e.event_type = 'JOB_READY'
                 AND e.aggregate_type = 'ORDER'
                 AND e.aggregate_id = ?
                 AND e.status = 'SENT'
                 AND e.dispatch_attempts = ?
                 AND e.dispatched_at IS NOT NULL
                 AND e.dispatch_lease_token IS NULL
                 AND e.dispatch_leased_at IS NULL
                 AND e.dispatch_lease_expires_at IS NULL
                 AND e.next_dispatch_at IS NULL
                 AND json_valid(e.payload) = 1
                 AND json_extract(e.payload, '$.job_id') = ?
             )
             AND (
               SELECT COUNT(*) FROM outbox_events
               WHERE event_type = 'JOB_READY'
                 AND aggregate_id = ?
             ) = 1`
        )
        .bind(
          MAX_ATTEMPTS_EXHAUSTED_RESCUE_REASON,
          now,
          cleanParams.jobId,
          cleanParams.orderId,
          cleanParams.attemptCount,
          cleanParams.maxAttempts,
          cleanParams.leaseOwner,
          cleanParams.leaseExpiresAt,
          now,
          cleanParams.lastError,
          cleanParams.orderId,
           cleanParams.paymentCode,
           cleanParams.paymentId,
           cleanParams.orderId,
           cleanParams.orderId,
           cleanParams.paymentTransactionId,
          cleanParams.orderId,
          cleanParams.outboxId,
          cleanParams.orderId,
          cleanParams.dispatchAttempts,
          cleanParams.jobId,
          cleanParams.orderId
        ),
      this.db
        .prepare(
          `UPDATE orders AS o
           SET status = 'PROCESSING', updated_at = ?
           WHERE o.id = ?
             AND o.status = 'FAILED'
             AND o.payment_code = ?
             AND EXISTS (
               SELECT 1 FROM fulfillment_jobs AS j
               WHERE j.id = ?
                 AND j.order_id = ?
                 AND j.status = 'RETRY'
                 AND j.attempt_count = ?
                 AND j.max_attempts = ?
                 AND j.last_error = ?
                 AND j.updated_at = ?
             )
           AND EXISTS (
              SELECT 1 FROM payments AS p
              WHERE p.id = ?
                AND p.order_id = o.id
                AND p.provider = 'SEPAY'
                AND p.status = 'VERIFIED'
                AND p.transaction_id = ?
                AND p.amount = o.total_amount
                AND p.currency = o.currency
              )
             AND (
               SELECT COUNT(*) FROM payments
               WHERE order_id = o.id AND provider = 'SEPAY' AND status = 'VERIFIED'
             ) = 1`
        )
        .bind(
          now,
          cleanParams.orderId,
          cleanParams.paymentCode,
          cleanParams.jobId,
          cleanParams.orderId,
          cleanParams.attemptCount,
          nextMaxAttempts,
          MAX_ATTEMPTS_EXHAUSTED_RESCUE_REASON,
          now,
          cleanParams.paymentId,
          cleanParams.paymentTransactionId
        ),
      this.db
        .prepare(
          `UPDATE outbox_events AS e
           SET status = 'PENDING',
               dispatched_at = NULL,
               dispatch_lease_token = NULL,
               dispatch_leased_at = NULL,
               dispatch_lease_expires_at = NULL,
               next_dispatch_at = NULL,
               last_dispatch_error = ?
           WHERE e.id = ?
             AND e.event_type = 'JOB_READY'
             AND e.aggregate_type = 'ORDER'
             AND e.aggregate_id = ?
             AND e.status = 'SENT'
             AND e.dispatch_attempts = ?
             AND e.dispatched_at IS NOT NULL
             AND e.dispatch_lease_token IS NULL
             AND e.dispatch_leased_at IS NULL
             AND e.dispatch_lease_expires_at IS NULL
             AND e.next_dispatch_at IS NULL
             AND json_valid(e.payload) = 1
             AND json_extract(e.payload, '$.job_id') = ?
             AND EXISTS (
               SELECT 1 FROM fulfillment_jobs AS j
               WHERE j.id = ?
                 AND j.order_id = ?
                 AND j.status = 'RETRY'
                 AND j.attempt_count = ?
                 AND j.max_attempts = ?
                 AND j.last_error = ?
                 AND j.updated_at = ?
             )
             AND EXISTS (
               SELECT 1 FROM orders AS o
               WHERE o.id = ? AND o.status = 'PROCESSING'
             )`
        )
        .bind(
          MAX_ATTEMPTS_EXHAUSTED_RESCUE_REASON,
          cleanParams.outboxId,
          cleanParams.orderId,
          cleanParams.dispatchAttempts,
          cleanParams.jobId,
          cleanParams.jobId,
          cleanParams.orderId,
          cleanParams.attemptCount,
          nextMaxAttempts,
          MAX_ATTEMPTS_EXHAUSTED_RESCUE_REASON,
          now,
          cleanParams.orderId
        ),
    ];

    const results = await this.db.batch(statements);
    return results.every((result) => result.meta.changes === 1)
      ? { status: 'RESCUED' }
      : { status: 'CONFLICT' };
  }

  async recoverTerminalFailedJob(params: TerminalJobRecoveryParams): Promise<TerminalJobRecoveryResult> {
    const cleanParams: TerminalJobRecoveryParams = {
      jobId: params.jobId.trim(),
      orderId: params.orderId.trim(),
      outboxId: params.outboxId.trim(),
      paymentId: params.paymentId.trim(),
      paymentTransactionId: params.paymentTransactionId.trim(),
      paymentCode: params.paymentCode.trim(),
      attemptCount: params.attemptCount,
      maxAttempts: params.maxAttempts,
      lastError: params.lastError.trim(),
      dispatchAttempts: params.dispatchAttempts,
    };

    if (!validTerminalJobRecoveryParams(cleanParams)) {
      return { status: 'CONFLICT' };
    }

    const now = Date.now();
    const nextMaxAttempts = cleanParams.maxAttempts + 1;
    const statements: D1PreparedStatement[] = [
      this.db
        .prepare(
          `UPDATE fulfillment_jobs AS j
           SET status = 'RETRY',
               max_attempts = max_attempts + 1,
               next_retry_at = NULL,
               last_error = ?,
               lease_owner = NULL,
               lease_token = NULL,
               leased_at = NULL,
               lease_expires_at = NULL,
               updated_at = ?
           WHERE j.id = ?
             AND j.order_id = ?
             AND j.status = 'FAILED'
             AND j.attempt_count = ?
             AND j.max_attempts = ?
             AND j.lease_owner IS NULL
             AND j.lease_token IS NULL
             AND j.leased_at IS NULL
             AND j.lease_expires_at IS NULL
             AND j.last_error = ?
             AND j.artifact_key IS NULL
             AND j.artifact_sha256 IS NULL
             AND j.artifact_size_bytes IS NULL
             AND j.artifact_parts IS NULL
             AND j.completed_at IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM fulfillment_receipts AS r
               WHERE r.job_id = j.id OR r.order_id = j.order_id
             )
             AND NOT EXISTS (
               SELECT 1 FROM artifacts AS a
               WHERE a.job_id = j.id OR a.order_id = j.order_id
             )
             AND EXISTS (
               SELECT 1 FROM orders AS o
               WHERE o.id = ?
                 AND o.status = 'FAILED'
                 AND o.payment_code = ?
             )
             AND EXISTS (
               SELECT 1
               FROM payments AS p
               JOIN orders AS payment_order ON payment_order.id = p.order_id
               WHERE p.id = ?
                 AND p.order_id = ?
                 AND payment_order.id = ?
                 AND p.provider = 'SEPAY'
                 AND p.status = 'VERIFIED'
                 AND p.transaction_id = ?
                 AND p.amount = payment_order.total_amount
                 AND p.currency = payment_order.currency
             )
             AND (
               SELECT COUNT(*) FROM payments
               WHERE order_id = ? AND provider = 'SEPAY' AND status = 'VERIFIED'
             ) = 1
             AND EXISTS (
               SELECT 1 FROM outbox_events AS e
               WHERE e.id = ?
                 AND e.event_type = 'JOB_READY'
                 AND e.aggregate_type = 'ORDER'
                 AND e.aggregate_id = ?
                 AND e.status = 'SENT'
                 AND e.dispatch_attempts = ?
                 AND e.dispatched_at IS NOT NULL
                 AND e.dispatch_lease_token IS NULL
                 AND e.dispatch_leased_at IS NULL
                 AND e.dispatch_lease_expires_at IS NULL
                 AND e.next_dispatch_at IS NULL
                 AND json_valid(e.payload) = 1
                 AND json_extract(e.payload, '$.job_id') = ?
             )
             AND (
               SELECT COUNT(*) FROM outbox_events
               WHERE event_type = 'JOB_READY'
                 AND aggregate_id = ?
             ) = 1`
        )
        .bind(
          TERMINAL_FAILED_RECOVERY_REASON,
          now,
          cleanParams.jobId,
          cleanParams.orderId,
          cleanParams.attemptCount,
          cleanParams.maxAttempts,
          cleanParams.lastError,
          cleanParams.orderId,
          cleanParams.paymentCode,
          cleanParams.paymentId,
          cleanParams.orderId,
          cleanParams.orderId,
          cleanParams.paymentTransactionId,
          cleanParams.orderId,
          cleanParams.outboxId,
          cleanParams.orderId,
          cleanParams.dispatchAttempts,
          cleanParams.jobId,
          cleanParams.orderId
        ),
      this.db
        .prepare(
          `UPDATE orders AS o
           SET status = 'PROCESSING', updated_at = ?
           WHERE o.id = ?
             AND o.status = 'FAILED'
             AND o.payment_code = ?
             AND EXISTS (
               SELECT 1 FROM fulfillment_jobs AS j
               WHERE j.id = ?
                 AND j.order_id = ?
                 AND j.status = 'RETRY'
                 AND j.attempt_count = ?
                 AND j.max_attempts = ?
                 AND j.last_error = ?
                 AND j.updated_at = ?
             )
             AND EXISTS (
               SELECT 1 FROM payments AS p
               WHERE p.id = ?
                 AND p.order_id = o.id
                 AND p.provider = 'SEPAY'
                 AND p.status = 'VERIFIED'
                 AND p.transaction_id = ?
                 AND p.amount = o.total_amount
                 AND p.currency = o.currency
             )
             AND (
               SELECT COUNT(*) FROM payments
               WHERE order_id = o.id AND provider = 'SEPAY' AND status = 'VERIFIED'
             ) = 1`
        )
        .bind(
          now,
          cleanParams.orderId,
          cleanParams.paymentCode,
          cleanParams.jobId,
          cleanParams.orderId,
          cleanParams.attemptCount,
          nextMaxAttempts,
          TERMINAL_FAILED_RECOVERY_REASON,
          now,
          cleanParams.paymentId,
          cleanParams.paymentTransactionId
        ),
      this.db
        .prepare(
          `UPDATE outbox_events AS e
           SET status = 'PENDING',
               dispatched_at = NULL,
               dispatch_lease_token = NULL,
               dispatch_leased_at = NULL,
               dispatch_lease_expires_at = NULL,
               next_dispatch_at = NULL,
               last_dispatch_error = ?
           WHERE e.id = ?
             AND e.event_type = 'JOB_READY'
             AND e.aggregate_type = 'ORDER'
             AND e.aggregate_id = ?
             AND e.status = 'SENT'
             AND e.dispatch_attempts = ?
             AND e.dispatched_at IS NOT NULL
             AND e.dispatch_lease_token IS NULL
             AND e.dispatch_leased_at IS NULL
             AND e.dispatch_lease_expires_at IS NULL
             AND e.next_dispatch_at IS NULL
             AND json_valid(e.payload) = 1
             AND json_extract(e.payload, '$.job_id') = ?
             AND EXISTS (
               SELECT 1 FROM fulfillment_jobs AS j
               WHERE j.id = ?
                 AND j.order_id = ?
                 AND j.status = 'RETRY'
                 AND j.attempt_count = ?
                 AND j.max_attempts = ?
                 AND j.last_error = ?
                 AND j.updated_at = ?
             )
             AND EXISTS (
               SELECT 1 FROM orders AS o
               WHERE o.id = ? AND o.status = 'PROCESSING'
             )`
        )
        .bind(
          TERMINAL_FAILED_RECOVERY_REASON,
          cleanParams.outboxId,
          cleanParams.orderId,
          cleanParams.dispatchAttempts,
          cleanParams.jobId,
          cleanParams.jobId,
          cleanParams.orderId,
          cleanParams.attemptCount,
          nextMaxAttempts,
          TERMINAL_FAILED_RECOVERY_REASON,
          now,
          cleanParams.orderId
        ),
    ];

    const results = await this.db.batch(statements);
    if (results.every((result) => result.meta.changes === 1)) {
      return { status: 'RECOVERED' };
    }

    // An idempotent replay is accepted only when the exact recovered lineage
    // already carries the bounded recovery marker; broad status inference is
    // intentionally avoided (same discipline as rearm ALREADY_REARMED).
    const alreadyRecovered = await this.db
      .prepare(
        `SELECT 1 AS matched
         FROM fulfillment_jobs AS j
         WHERE j.id = ?
           AND j.order_id = ?
           AND j.status = 'RETRY'
           AND j.attempt_count = ?
           AND j.max_attempts = ?
           AND j.last_error = ?
           AND j.lease_owner IS NULL
           AND j.lease_token IS NULL
           AND j.leased_at IS NULL
           AND j.lease_expires_at IS NULL
           AND j.next_retry_at IS NULL
           AND j.artifact_key IS NULL
           AND j.artifact_sha256 IS NULL
           AND j.artifact_size_bytes IS NULL
           AND j.artifact_parts IS NULL
           AND j.completed_at IS NULL
           AND EXISTS (
             SELECT 1 FROM orders AS o
             WHERE o.id = ? AND o.status = 'PROCESSING'
           )
           AND EXISTS (
             SELECT 1 FROM outbox_events AS e
             WHERE e.id = ?
               AND e.event_type = 'JOB_READY'
               AND e.aggregate_type = 'ORDER'
               AND e.aggregate_id = ?
               AND e.status = 'PENDING'
               AND e.dispatch_attempts = ?
               AND e.dispatched_at IS NULL
               AND e.dispatch_lease_token IS NULL
               AND e.dispatch_leased_at IS NULL
               AND e.dispatch_lease_expires_at IS NULL
               AND e.next_dispatch_at IS NULL
               AND e.last_dispatch_error = ?
               AND json_valid(e.payload) = 1
               AND json_extract(e.payload, '$.job_id') = ?
           )`
      )
      .bind(
        cleanParams.jobId,
        cleanParams.orderId,
        cleanParams.attemptCount,
        nextMaxAttempts,
        TERMINAL_FAILED_RECOVERY_REASON,
        cleanParams.orderId,
        cleanParams.outboxId,
        cleanParams.orderId,
        cleanParams.dispatchAttempts,
        TERMINAL_FAILED_RECOVERY_REASON,
        cleanParams.jobId
      )
      .first<{ matched: number }>();

    return alreadyRecovered ? { status: 'ALREADY_RECOVERED' } : { status: 'CONFLICT' };
  }

  async validateLeaseForArtifactUpload(
    jobId: string,
    workerId: string,
    leaseToken: string
  ): Promise<{ valid: boolean; orderId?: string; reason?: string }> {
    const cleanWorkerId = workerId.trim();
    const cleanToken = leaseToken.trim();

    if (!/^[a-zA-Z0-9_-]{1,64}$/.test(cleanWorkerId) || !/^[0-9a-fA-F-]{36}$/.test(cleanToken)) {
      return { valid: false, reason: 'invalid_credentials' };
    }

    const now = Date.now();
    const safetyMarginMs = 15000;

    const job = await this.db
      .prepare(
        `SELECT id, order_id, status, lease_owner, lease_token, lease_expires_at
         FROM fulfillment_jobs
         WHERE id = ?`
      )
      .bind(jobId)
      .first<{
        id: string;
        order_id: string;
        status: string;
        lease_owner: string | null;
        lease_token: string | null;
        lease_expires_at: number | null;
      }>();

    if (!job) {
      return { valid: false, reason: 'job_not_found' };
    }

    if (job.status !== 'PROCESSING') {
      return { valid: false, reason: `job_status_${job.status.toLowerCase()}` };
    }

    if (job.lease_owner !== cleanWorkerId || job.lease_token !== cleanToken) {
      return { valid: false, reason: 'lease_token_mismatch' };
    }

    if (!job.lease_expires_at || job.lease_expires_at <= now + safetyMarginMs) {
      return { valid: false, reason: 'lease_expired_or_near_margin' };
    }

    return { valid: true, orderId: job.order_id };
  }

  async claimJob(
    jobId: string,
    workerId: string,
    leaseDurationSeconds = 300
  ): Promise<ClaimJobResult> {
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
                   last_error = ?,
                   updated_at = ?
               WHERE id = ?
                 AND status = 'PROCESSING'
                 AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                 AND attempt_count >= max_attempts`
            )
             .bind(MAX_ATTEMPTS_EXHAUSTED_REASON, now, jobId, now),
          this.db
            .prepare(
              `UPDATE orders
               SET status = 'FAILED', updated_at = ?
               WHERE id = (
                 SELECT order_id FROM fulfillment_jobs
                 WHERE id = ? AND status = 'FAILED' AND last_error = ? AND updated_at = ?
               )
               AND status = 'PROCESSING'`
            )
             .bind(now, jobId, MAX_ATTEMPTS_EXHAUSTED_REASON, now),
        ]);
      }

      return {
        status: 'TERMINAL',
        queue_action: 'ack',
        reason: MAX_ATTEMPTS_EXHAUSTED_REASON,
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

  async completeJob(params: {
    jobId: string;
    workerId: string;
    leaseToken: string;
    artifactKey: string;
    artifactSha256: string;
    artifactSizeBytes: number;
    parts?: ArtifactPartMeta[];
  }): Promise<CompleteJobResult> {
    const { jobId, workerId, leaseToken, artifactKey, artifactSha256, artifactSizeBytes, parts } = params;
    const cleanWorkerId = workerId.trim();
    const cleanToken = leaseToken.trim();
    const cleanSha = artifactSha256.trim().toLowerCase();

    if (
      !/^[a-zA-Z0-9_-]{1,64}$/.test(cleanWorkerId) ||
      !/^[0-9a-fA-F-]{36}$/.test(cleanToken) ||
      !/^[0-9a-f]{64}$/.test(cleanSha) ||
      artifactSizeBytes <= 0
    ) {
      return { status: 'ERROR', queue_action: 'retry', reason: 'invalid_completion_params' };
    }

    // 1. Idempotency check: inspect existing fulfillment_receipts
    const existingReceipt = await this.getReceiptByJobId(jobId);
    if (existingReceipt) {
      if (
        existingReceipt.artifact_key === artifactKey &&
        existingReceipt.artifact_sha256 === cleanSha
      ) {
        return {
          status: 'ALREADY_COMPLETED',
          queue_action: 'ack',
          completed_at: existingReceipt.completed_at,
          artifact_key: existingReceipt.artifact_key,
        };
      }
      return {
        status: 'CONFLICT_DIFFERENT_ARTIFACT',
        queue_action: 'ack',
        reason: 'Job already completed with different artifact',
      };
    }

    const job = await this.getJobById(jobId);
    if (!job) {
      return { status: 'NOT_FOUND', queue_action: 'ack', reason: 'job_not_found' };
    }

    // Check if already completed on job record
    if (job.status === 'COMPLETED') {
      if (job.artifact_key === artifactKey && job.artifact_sha256 === cleanSha) {
        return {
          status: 'ALREADY_COMPLETED',
          queue_action: 'ack',
          completed_at: job.completed_at || job.updated_at,
          artifact_key: job.artifact_key || artifactKey,
        };
      }
      return {
        status: 'CONFLICT_DIFFERENT_ARTIFACT',
        queue_action: 'ack',
        reason: 'Job already completed with different artifact',
      };
    }

    const now = Date.now();

    // 2. Validate active unexpired lease fencing
    if (
      job.status !== 'PROCESSING' ||
      job.lease_owner !== cleanWorkerId ||
      job.lease_token !== cleanToken ||
      !job.lease_expires_at ||
      job.lease_expires_at <= now
    ) {
      return {
        status: 'EXPIRED_OR_FENCED',
        queue_action: 'retry',
        reason: 'lease_superseded_or_expired',
      };
    }

    const orderId = job.order_id;
    const outboxId = crypto.randomUUID();
    const artifactId = crypto.randomUUID();
    const outboxPayload = JSON.stringify({ order_id: orderId, confirmed_parts: [] });

    const partsJson =
      parts && parts.length > 0
        ? JSON.stringify(parts)
        : JSON.stringify([
            {
              part_index: 1,
              total_parts: 1,
              filename: `${jobId}.zip`,
              artifact_key: artifactKey,
              artifact_size_bytes: artifactSizeBytes,
              artifact_sha256: cleanSha,
            },
          ]);

    // 3. Atomic D1 transactional batch (BLOCK 2: Fully gated mutations)
    const statements: D1PreparedStatement[] = [
      // Statement 1: Insert completion receipt conditionally on current unexpired lease + processing order
      this.db
        .prepare(
          `INSERT INTO fulfillment_receipts (
             job_id, order_id, artifact_key, artifact_sha256, artifact_size_bytes, artifact_parts, completed_at, created_at
           )
           SELECT ?, ?, ?, ?, ?, ?, ?, ?
           WHERE EXISTS (
             SELECT 1 FROM fulfillment_jobs
             WHERE id = ?
               AND status = 'PROCESSING'
               AND lease_owner = ?
               AND lease_token = ?
               AND lease_expires_at > ?
           )
           AND EXISTS (
             SELECT 1 FROM orders
             WHERE id = ?
               AND status = 'PROCESSING'
           )`
        )
        .bind(
          jobId,
          orderId,
          artifactKey,
          cleanSha,
          artifactSizeBytes,
          partsJson,
          now,
          now,
          jobId,
          cleanWorkerId,
          cleanToken,
          now,
          orderId
        ),

      // Statement 2: Transition fulfillment_job to COMPLETED only if gated receipt exists
      this.db
        .prepare(
          `UPDATE fulfillment_jobs
           SET status = 'COMPLETED',
               artifact_key = ?,
               artifact_sha256 = ?,
               artifact_size_bytes = ?,
               artifact_parts = ?,
               completed_at = ?,
               lease_owner = NULL,
               lease_token = NULL,
               leased_at = NULL,
               lease_expires_at = NULL,
               updated_at = ?
           WHERE id = ?
             AND status = 'PROCESSING'
             AND lease_owner = ?
             AND lease_token = ?
             AND lease_expires_at > ?
             AND EXISTS (
               SELECT 1 FROM fulfillment_receipts
               WHERE job_id = ? AND artifact_key = ? AND artifact_sha256 = ?
             )`
        )
        .bind(
          artifactKey,
          cleanSha,
          artifactSizeBytes,
          partsJson,
          now,
          now,
          jobId,
          cleanWorkerId,
          cleanToken,
          now,
          jobId,
          artifactKey,
          cleanSha
        ),

      // Statement 3: Transition order to COMPLETED only if job is completed and receipt exists
      this.db
        .prepare(
          `UPDATE orders
           SET status = 'COMPLETED',
               completed_at = ?,
               updated_at = ?
           WHERE id = ?
             AND status = 'PROCESSING'
             AND EXISTS (
               SELECT 1 FROM fulfillment_receipts
               WHERE order_id = ? AND job_id = ?
             )`
        )
        .bind(now, now, orderId, orderId, jobId),

      // Statement 4: Insert DELIVERY_READY outbox event only if order successfully transitioned to COMPLETED
      this.db
        .prepare(
          `INSERT INTO outbox_events (
             id, event_type, aggregate_type, aggregate_id, payload, status, created_at
           )
           SELECT ?, 'DELIVERY_READY', 'order', ?, ?, 'PENDING', ?
           WHERE EXISTS (
             SELECT 1 FROM orders
             WHERE id = ? AND status = 'COMPLETED'
           )
           AND EXISTS (
             SELECT 1 FROM fulfillment_receipts
             WHERE order_id = ? AND job_id = ?
           )`
        )
        .bind(outboxId, orderId, outboxPayload, now, orderId, orderId, jobId),

      // Statement 5: Insert artifact record only if gated receipt exists
      this.db
        .prepare(
          `INSERT OR IGNORE INTO artifacts (
             id, order_id, job_id, storage_key, file_name, file_size, mime_type, created_at
           )
           SELECT ?, ?, ?, ?, ?, ?, 'application/zip', ?
           WHERE EXISTS (
             SELECT 1 FROM fulfillment_receipts
             WHERE job_id = ? AND order_id = ? AND artifact_key = ? AND artifact_sha256 = ?
           )`
        )
        .bind(artifactId, orderId, jobId, artifactKey, `${orderId}.zip`, artifactSizeBytes, now, jobId, orderId, artifactKey, cleanSha),
    ];

    try {
      const results = await this.db.batch(statements);
      const receiptChanges = results[0].meta.changes;
      const jobChanges = results[1].meta.changes;
      const orderChanges = results[2].meta.changes;

      if (!receiptChanges || receiptChanges === 0 || !jobChanges || jobChanges === 0 || !orderChanges || orderChanges === 0) {
        return {
          status: 'EXPIRED_OR_FENCED',
          queue_action: 'retry',
          reason: 'completion_gated_predicates_failed',
        };
      }

      return {
        status: 'COMPLETED',
        queue_action: 'ack',
        completed_at: now,
        artifact_key: artifactKey,
      };
    } catch (err: unknown) {
      // In case of unique constraint conflict due to concurrent race
      const raceReceipt = await this.getReceiptByJobId(jobId);
      if (raceReceipt) {
        if (raceReceipt.artifact_key === artifactKey && raceReceipt.artifact_sha256 === cleanSha) {
          return {
            status: 'ALREADY_COMPLETED',
            queue_action: 'ack',
            completed_at: raceReceipt.completed_at,
            artifact_key: raceReceipt.artifact_key,
          };
        }
        return {
          status: 'CONFLICT_DIFFERENT_ARTIFACT',
          queue_action: 'ack',
          reason: 'Job already completed with different artifact',
        };
      }

      return {
        status: 'ERROR',
        queue_action: 'retry',
        reason: err instanceof Error ? err.message : 'completion_batch_failed',
      };
    }
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

    const terminalReason =
      job.attempt_count >= job.max_attempts ? MAX_ATTEMPTS_EXHAUSTED_REASON : cleanReason;

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
           WHERE id = ?
             AND status = 'PROCESSING'
             AND lease_owner = ?
             AND lease_token = ?
             AND lease_expires_at > ?`
        )
        .bind(terminalReason, now, jobId, cleanWorkerId, cleanToken, now),

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

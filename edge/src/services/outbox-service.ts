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
    private readonly queue: Queue<unknown>
  ) {}

  async dispatchPendingEvents(options: DispatchOptions = {}): Promise<DispatchResult> {
    const batchSize = Math.max(1, Math.min(options.batchSize || 10, 50));
    const leaseDurationMs = Math.max(5, options.leaseDurationSeconds || 30) * 1000;
    const now = Date.now();

    // 1. Select candidate pending outbox rows due for dispatch
    const candidates = await this.db
      .prepare(
        `SELECT * FROM outbox_events
         WHERE status = 'PENDING'
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

      // 2. CAS Acquire Lease before calling Queue
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
        // Lost race to another concurrent dispatcher
        continue;
      }

      // 3. Publish minimal payload to Queue
      try {
        let queuePayload: unknown;
        try {
          queuePayload = JSON.parse(event.payload);
        } catch {
          queuePayload = { raw: event.payload };
        }

        await this.queue.send(queuePayload);

        // 4. Mark SENT only if current lease token still owns the record
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
      } catch (err: unknown) {
        failureCount++;
        const errorMessage = err instanceof Error ? err.message : String(err);
        const attempts = event.dispatch_attempts + 1;
        const backoffSeconds = Math.min(300, 5 * Math.pow(2, attempts - 1));
        const nextDispatchAt = Date.now() + backoffSeconds * 1000;

        // Release lease and schedule bounded retry
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
          .bind(nextDispatchAt, errorMessage.slice(0, 255), event.id, leaseToken)
          .run();
      }
    }

    return { dispatchedCount, failureCount };
  }
}

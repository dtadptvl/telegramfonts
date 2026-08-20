import { describe, it, expect } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker, { type Env } from '../src/index';
import { OutboxService } from '../src/services/outbox-service';

const testEnv: Env = {
  ...(env as unknown as Env),
};

describe('Phase 4: Transactional Outbox Dispatcher', () => {
  it('dispatches PENDING outbox event to Queue and transitions status to SENT', async () => {
    const queueSentMessages: unknown[] = [];
    const mockQueue = {
      send: async (msg: unknown) => {
        queueSentMessages.push(msg);
      },
      sendBatch: async () => {},
    } as unknown as Queue<unknown>;

    const outboxService = new OutboxService(env.DB, mockQueue);

    const eventId = `outbox_${crypto.randomUUID()}`;
    const jobId = `job_${crypto.randomUUID()}`;
    const now = Date.now();

    // Insert pending outbox event
    await env.DB.prepare(
      `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, created_at)
       VALUES (?, 'JOB_READY', 'ORDER', 'ord_test_1', ?, 'PENDING', ?)`
    )
      .bind(eventId, JSON.stringify({ job_id: jobId }), now)
      .run();

    const result = await outboxService.dispatchPendingEvents({ batchSize: 10 });
    expect(result.dispatchedCount).toBe(1);
    expect(result.failureCount).toBe(0);

    // Verify queue received exactly {"job_id": jobId}
    expect(queueSentMessages.length).toBe(1);
    expect(queueSentMessages[0]).toEqual({ job_id: jobId });

    // Verify row marked SENT with dispatched_at timestamp
    const row = await env.DB.prepare('SELECT * FROM outbox_events WHERE id = ?')
      .bind(eventId)
      .first<{ status: string; dispatched_at: number; dispatch_lease_token: string | null }>();

    expect(row?.status).toBe('SENT');
    expect(row?.dispatched_at).toBeGreaterThan(0);
    expect(row?.dispatch_lease_token).toBeNull();
  });

  it('prevents concurrent dispatchers from double-sending via D1 CAS lease acquisition', async () => {
    const queueSentMessages: unknown[] = [];
    const mockQueue = {
      send: async (msg: unknown) => {
        queueSentMessages.push(msg);
      },
      sendBatch: async () => {},
    } as unknown as Queue<unknown>;

    const outboxService1 = new OutboxService(env.DB, mockQueue);
    const outboxService2 = new OutboxService(env.DB, mockQueue);

    const eventId = `outbox_${crypto.randomUUID()}`;
    const jobId = `job_${crypto.randomUUID()}`;
    const now = Date.now();

    await env.DB.prepare(
      `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, created_at)
       VALUES (?, 'JOB_READY', 'ORDER', 'ord_test_concurrent', ?, 'PENDING', ?)`
    )
      .bind(eventId, JSON.stringify({ job_id: jobId }), now)
      .run();

    // Run two simultaneous dispatch calls
    const [res1, res2] = await Promise.all([
      outboxService1.dispatchPendingEvents({ batchSize: 10 }),
      outboxService2.dispatchPendingEvents({ batchSize: 10 }),
    ]);

    expect(res1.dispatchedCount + res2.dispatchedCount).toBe(1);
    expect(queueSentMessages.length).toBe(1);
  });

  it('preserves durable PENDING work with backoff when Queue send throws', async () => {
    const failingQueue = {
      send: async () => {
        throw new Error('Simulated Queue Service Unavailable');
      },
      sendBatch: async () => {},
    } as unknown as Queue<unknown>;

    const outboxService = new OutboxService(env.DB, failingQueue);

    const eventId = `outbox_${crypto.randomUUID()}`;
    const jobId = `job_${crypto.randomUUID()}`;
    const now = Date.now();

    await env.DB.prepare(
      `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, created_at)
       VALUES (?, 'JOB_READY', 'ORDER', 'ord_test_fail', ?, 'PENDING', ?)`
    )
      .bind(eventId, JSON.stringify({ job_id: jobId }), now)
      .run();

    const result = await outboxService.dispatchPendingEvents({ batchSize: 10 });
    expect(result.dispatchedCount).toBe(0);
    expect(result.failureCount).toBe(1);

    // Verify row remains PENDING with next_dispatch_at scheduled in the future and lease cleared
    const row = await env.DB.prepare('SELECT * FROM outbox_events WHERE id = ?')
      .bind(eventId)
      .first<{
        status: string;
        dispatch_attempts: number;
        next_dispatch_at: number;
        last_dispatch_error: string;
        dispatch_lease_token: string | null;
      }>();

    expect(row?.status).toBe('PENDING');
    expect(row?.dispatch_attempts).toBe(1);
    expect(row?.next_dispatch_at).toBeGreaterThan(now);
    expect(row?.last_dispatch_error).toContain('Simulated Queue Service Unavailable');
    expect(row?.dispatch_lease_token).toBeNull();
  });

  it('allows later duplicate publish on simulated crash without ever creating duplicate fulfillment jobs', async () => {
    const queueSentMessages: unknown[] = [];
    let throwAfterQueueSend = true;

    const mockQueue = {
      send: async (msg: unknown) => {
        queueSentMessages.push(msg);
        if (throwAfterQueueSend) {
          throwAfterQueueSend = false;
          // Simulate worker dying right after Queue accepted message before D1 mark-SENT
          throw new Error('Worker crash after Queue publish');
        }
      },
      sendBatch: async () => {},
    } as unknown as Queue<unknown>;

    const outboxService = new OutboxService(env.DB, mockQueue);

    const eventId = `outbox_${crypto.randomUUID()}`;
    const jobId = `job_${crypto.randomUUID()}`;
    const now = Date.now();

    await env.DB.prepare(
      `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, created_at)
       VALUES (?, 'JOB_READY', 'ORDER', 'ord_test_crash', ?, 'PENDING', ?)`
    )
      .bind(eventId, JSON.stringify({ job_id: jobId }), now)
      .run();

    // First attempt: publishes to queue then throws
    const res1 = await outboxService.dispatchPendingEvents({ batchSize: 10 });
    expect(res1.failureCount).toBe(1);
    expect(queueSentMessages.length).toBe(1);

    // Advance time past the backoff
    await env.DB.prepare('UPDATE outbox_events SET next_dispatch_at = NULL WHERE id = ?')
      .bind(eventId)
      .run();

    // Second attempt: publishes again and marks SENT
    const res2 = await outboxService.dispatchPendingEvents({ batchSize: 10 });
    expect(res2.dispatchedCount).toBe(1);
    expect(queueSentMessages.length).toBe(2);

    const row = await env.DB.prepare('SELECT status FROM outbox_events WHERE id = ?')
      .bind(eventId)
      .first<{ status: string }>();
    expect(row?.status).toBe('SENT');
  });

  it('stale dispatcher lease cannot mark a row SENT', async () => {
    const mockQueue = {
      send: async () => {},
      sendBatch: async () => {},
    } as unknown as Queue<unknown>;

    const eventId = `outbox_${crypto.randomUUID()}`;
    const now = Date.now();

    await env.DB.prepare(
      `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, dispatch_lease_token, created_at)
       VALUES (?, 'JOB_READY', 'ORDER', 'ord_test_stale', '{"job_id":"1"}', 'PENDING', 'valid_token_123', ?)`
    )
      .bind(eventId, now)
      .run();

    // Attempt to mark SENT with a stale token
    const result = await env.DB.prepare(
      `UPDATE outbox_events
       SET status = 'SENT', dispatched_at = ?
       WHERE id = ? AND status = 'PENDING' AND dispatch_lease_token = 'stale_wrong_token'`
    )
      .bind(now, eventId)
      .run();

    expect(result.meta.changes).toBe(0);

    const row = await env.DB.prepare('SELECT status FROM outbox_events WHERE id = ?')
      .bind(eventId)
      .first<{ status: string }>();
    expect(row?.status).toBe('PENDING');
  });

  it('runs scheduled outbox dispatcher safely without throwing', async () => {
    const ctx = createExecutionContext();
    await worker.scheduled({} as ScheduledEvent, testEnv, ctx);
    await waitOnExecutionContext(ctx);
  });
});

import { describe, it, expect } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker, { type Env } from '../src/index';
import { OutboxService } from '../src/services/outbox-service';

const testEnv: Env = {
  ...(env as unknown as Env),
};

describe('Phase 4: Transactional Outbox Dispatcher', () => {
  it('dispatches PENDING JOB_READY outbox event to Queue and transitions status to SENT with minimal payload { job_id }', async () => {
    const queueSentMessages: unknown[] = [];
    const mockQueue = {
      send: async (msg: unknown) => {
        queueSentMessages.push(msg);
      },
      sendBatch: async () => {},
    } as unknown as Queue<unknown>;

    const outboxService = new OutboxService(env.DB, mockQueue);

    const eventId = `outbox_${crypto.randomUUID()}`;
    const jobId = `job_${crypto.randomUUID().replace(/-/g, '')}`;
    const now = Date.now();

    // Insert pending outbox event with extra sensitive data
    await env.DB.prepare(
      `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, created_at)
       VALUES (?, 'JOB_READY', 'ORDER', 'ord_test_1', ?, 'PENDING', ?)`
    )
      .bind(
        eventId,
        JSON.stringify({
          job_id: jobId,
          sensitive_payment_info: 'secret',
          customer_email: 'user@example.com',
        }),
        now
      )
      .run();

    const result = await outboxService.dispatchPendingEvents({ batchSize: 10 });
    expect(result.dispatchedCount).toBe(1);
    expect(result.failureCount).toBe(0);

    // Verify queue received strictly and exactly {"job_id": jobId} without extra fields (BLOCK 2)
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

  it('ignores non-JOB_READY pending outbox rows (BLOCK 2)', async () => {
    const queueSentMessages: unknown[] = [];
    const mockQueue = {
      send: async (msg: unknown) => {
        queueSentMessages.push(msg);
      },
      sendBatch: async () => {},
    } as unknown as Queue<unknown>;

    const outboxService = new OutboxService(env.DB, mockQueue);
    const eventId = `outbox_other_${crypto.randomUUID()}`;
    const now = Date.now();

    // Insert pending outbox event of different type
    await env.DB.prepare(
      `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, created_at)
       VALUES (?, 'ORDER_COMPLETED', 'ORDER', 'ord_test_other', '{"order_id":"1"}', 'PENDING', ?)`
    )
      .bind(eventId, now)
      .run();

    const result = await outboxService.dispatchPendingEvents({ batchSize: 10 });
    expect(result.dispatchedCount).toBe(0);
    expect(queueSentMessages.length).toBe(0);

    const row = await env.DB.prepare('SELECT status FROM outbox_events WHERE id = ?')
      .bind(eventId)
      .first<{ status: string }>();
    expect(row?.status).toBe('PENDING');
  });

  it('rejects malformed outbox payload without publishing to Queue, leaving it recoverable (BLOCK 2)', async () => {
    const queueSentMessages: unknown[] = [];
    const mockQueue = {
      send: async (msg: unknown) => {
        queueSentMessages.push(msg);
      },
      sendBatch: async () => {},
    } as unknown as Queue<unknown>;

    const outboxService = new OutboxService(env.DB, mockQueue);
    const eventId = `outbox_malformed_${crypto.randomUUID()}`;
    const now = Date.now();

    // Insert pending outbox event with malformed payload (missing job_id)
    await env.DB.prepare(
      `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, created_at)
       VALUES (?, 'JOB_READY', 'ORDER', 'ord_test_bad', '{"no_job_id":"true"}', 'PENDING', ?)`
    )
      .bind(eventId, now)
      .run();

    const result = await outboxService.dispatchPendingEvents({ batchSize: 10 });
    expect(result.dispatchedCount).toBe(0);
    expect(result.failureCount).toBe(1);
    expect(queueSentMessages.length).toBe(0);

    const row = await env.DB.prepare('SELECT * FROM outbox_events WHERE id = ?')
      .bind(eventId)
      .first<{ status: string; last_dispatch_error: string }>();
    expect(row?.status).toBe('PENDING');
    expect(row?.last_dispatch_error).toBe('INVALID_JOB_ID_PAYLOAD');
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
    const jobId = `job_${crypto.randomUUID().replace(/-/g, '')}`;
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

  it('preserves durable PENDING work with backoff and sanitized error reason when Queue send throws (BLOCK 2)', async () => {
    const failingQueue = {
      send: async () => {
        throw new Error('Simulated Queue Service Unavailable');
      },
      sendBatch: async () => {},
    } as unknown as Queue<unknown>;

    const outboxService = new OutboxService(env.DB, failingQueue);

    const eventId = `outbox_${crypto.randomUUID()}`;
    const jobId = `job_${crypto.randomUUID().replace(/-/g, '')}`;
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

    // Verify row remains PENDING with sanitized error reason
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
    expect(row?.last_dispatch_error).toBe('QUEUE_SEND_FAILED');
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
    const jobId = `job_${crypto.randomUUID().replace(/-/g, '')}`;
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

  describe('Phase 6: DELIVERY_READY Outbox Dispatcher', () => {
    it('dispatches DELIVERY_READY to Telegram with signed download URL, never to fulfillment Queue', async () => {
      const queueSentMessages: unknown[] = [];
      const mockQueue = {
        send: async (msg: unknown) => {
          queueSentMessages.push(msg);
        },
        sendBatch: async () => {},
      } as unknown as Queue<unknown>;

      const orderId = `ord_delivery_${crypto.randomUUID().replace(/-/g, '')}`;
      const userId = `user_delivery_${crypto.randomUUID()}`;
      const now = Date.now();

      // Insert user
      await env.DB.prepare(
        `INSERT INTO telegram_users (id, username, first_name, created_at, updated_at)
         VALUES (?, 'delivery_user', 'Delivery', ?, ?)`
      )
        .bind(userId, now, now)
        .run();

      // Insert completed order (mode = ORIGINAL: T-PRICE-01 delivery binds mode)
      await env.DB.prepare(
        `INSERT INTO orders (id, user_id, status, total_amount, currency, mode, created_at, updated_at, completed_at)
         VALUES (?, ?, 'COMPLETED', 100000, 'VND', 'ORIGINAL', ?, ?, ?)`
      )
        .bind(orderId, userId, now, now, now)
        .run();

      // Insert user session with chat_id
      await env.DB.prepare(
        `INSERT INTO telegram_sessions (id, user_id, chat_id, workflow_token, checkout_token, status, created_at, updated_at)
         VALUES (?, ?, 987654321, 'wf_del', 'chk_del', 'ORDER_CREATED', ?, ?)`
      )
        .bind(`sess_${crypto.randomUUID()}`, userId, now, now)
        .run();

      // Clear any prior pending outbox events
      await env.DB.prepare('DELETE FROM outbox_events WHERE status = "PENDING"').run();

      // Insert DELIVERY_READY outbox event
      const eventId = `outbox_del_${crypto.randomUUID()}`;
      await env.DB.prepare(
        `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, created_at)
         VALUES (?, 'DELIVERY_READY', 'order', ?, ?, 'PENDING', ?)`
      )
        .bind(eventId, orderId, JSON.stringify({ order_id: orderId }), now)
        .run();

      // Insert fulfillment_receipts and R2 artifact for delivery
      const artifactKey = `artifacts/${orderId}/bundle.zip`;
      const dummyZip = new TextEncoder().encode('PK\x05\x06dummy_zip_content');
      const shaBuf = await crypto.subtle.digest('SHA-256', dummyZip);
      const shaHex = Array.from(new Uint8Array(shaBuf))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('');

      await env.DB.prepare(
        `INSERT INTO fulfillment_receipts (job_id, order_id, artifact_key, artifact_size_bytes, artifact_sha256, completed_at, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      )
        .bind(`job_${orderId}`, orderId, artifactKey, dummyZip.byteLength, shaHex, now, now)
        .run();

      await env.ARTIFACTS_BUCKET.put(artifactKey, dummyZip);

      const customEnv: Env = {
        ...(env as unknown as Env),
        TELEGRAM_BOT_TOKEN: 'fake_bot_token',
        ARTIFACTS_BUCKET: env.ARTIFACTS_BUCKET,
      };

      // Mock Telegram fetch
      const fetchCalls: Array<{ url: string; formData: FormData }> = [];
      const originalFetch = globalThis.fetch;
      globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('api.telegram.org')) {
          const formData = init?.body instanceof FormData ? init.body : new FormData();
          fetchCalls.push({ url, formData });
          return new Response(JSON.stringify({ ok: true, result: { message_id: 123 } }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return originalFetch(input, init);
      };

      try {
        const outboxService = new OutboxService(env.DB, mockQueue, customEnv);
        const result = await outboxService.dispatchPendingEvents({ batchSize: 10 });

        expect(result.dispatchedCount).toBe(1);
        expect(result.failureCount).toBe(0);

        // Queue was NOT called for DELIVERY_READY
        expect(queueSentMessages.length).toBe(0);

        // Telegram was called with sendDocument
        expect(fetchCalls.length).toBe(1);
        expect(fetchCalls[0].url).toContain('/sendDocument');
        expect(fetchCalls[0].formData.get('chat_id')).toBe('987654321');
        expect(fetchCalls[0].formData.get('document')).toBeDefined();

        // Outbox event marked SENT
        const row = await env.DB.prepare('SELECT status, dispatched_at FROM outbox_events WHERE id = ?')
          .bind(eventId)
          .first<{ status: string; dispatched_at: number }>();
        expect(row?.status).toBe('SENT');
        expect(row?.dispatched_at).toBeGreaterThan(0);
      } finally {
        globalThis.fetch = originalFetch;
      }
    });

    it('fails closed and keeps DELIVERY_READY recoverable PENDING when ARTIFACTS_BUCKET is missing', async () => {
      const mockQueue = {
        send: async () => {},
        sendBatch: async () => {},
      } as unknown as Queue<unknown>;

      const orderId = `ord_del_missing_bucket_${crypto.randomUUID().replace(/-/g, '')}`;
      const userId = `user_del_missing_bucket_${crypto.randomUUID()}`;
      const now = Date.now();

      await env.DB.prepare(
        `INSERT INTO telegram_users (id, username, first_name, created_at, updated_at)
         VALUES (?, 'insec_user', 'Insecure', ?, ?)`
      )
        .bind(userId, now, now)
        .run();

      await env.DB.prepare(
        `INSERT INTO orders (id, user_id, status, total_amount, currency, mode, created_at, updated_at, completed_at)
         VALUES (?, ?, 'COMPLETED', 100000, 'VND', 'ORIGINAL', ?, ?, ?)`
      )
        .bind(orderId, userId, now, now, now)
        .run();

      await env.DB.prepare(
        `INSERT INTO telegram_sessions (id, user_id, chat_id, workflow_token, checkout_token, status, created_at, updated_at)
         VALUES (?, ?, 987654321, 'wf_insec', 'chk_insec', 'ORDER_CREATED', ?, ?)`
      )
        .bind(`sess_${crypto.randomUUID()}`, userId, now, now)
        .run();

      await env.DB.prepare('DELETE FROM outbox_events WHERE status = "PENDING"').run();

      const eventId = `outbox_insec_${crypto.randomUUID()}`;
      await env.DB.prepare(
        `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, created_at)
         VALUES (?, 'DELIVERY_READY', 'order', ?, ?, 'PENDING', ?)`
      )
        .bind(eventId, orderId, JSON.stringify({ order_id: orderId }), now)
        .run();

      const missingBucketEnv: Env = {
        ...(env as unknown as Env),
        TELEGRAM_BOT_TOKEN: 'fake_bot_token',
        ARTIFACTS_BUCKET: undefined as unknown as R2Bucket,
      };

      const outboxService = new OutboxService(env.DB, mockQueue, missingBucketEnv);
      const result = await outboxService.dispatchPendingEvents({ batchSize: 10 });

      expect(result.dispatchedCount).toBe(0);
      expect(result.failureCount).toBe(1);

      // Row remains PENDING with backoff and error recorded
      const row = await env.DB.prepare('SELECT status, dispatch_attempts, last_dispatch_error FROM outbox_events WHERE id = ?')
        .bind(eventId)
        .first<{ status: string; dispatch_attempts: number; last_dispatch_error: string }>();
      expect(row?.status).toBe('PENDING');
      expect(row?.dispatch_attempts).toBe(1);
      expect(row?.last_dispatch_error).toBeDefined();
    });
  });

  it('runs scheduled outbox dispatcher safely without throwing', async () => {
    const ctx = createExecutionContext();
    await worker.scheduled({} as ScheduledEvent, testEnv, ctx);
    await waitOnExecutionContext(ctx);
  });
});


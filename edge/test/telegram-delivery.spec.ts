import { describe, it, expect, beforeAll } from 'vitest';
import { env } from 'cloudflare:test';
import { OutboxService } from '../src/services/outbox-service';
import { JobService } from '../src/services/job-service';
import type { Env } from '../src/env';

describe('Issue #33: In-Telegram Bot API sendDocument Delivery & Multipart Recovery', () => {
  beforeAll(async () => {
    await env.DB.exec('PRAGMA foreign_keys = ON;');
  });

  const setupOrderWithReceipt = async (params: {
    partsCount?: number;
    partSizes?: number[];
  }) => {
    const orderId = `ord_tgdel_${crypto.randomUUID().replace(/-/g, '')}`;
    const jobId = `job_tgdel_${crypto.randomUUID().replace(/-/g, '')}`;
    const userId = `user_tgdel_${crypto.randomUUID().replace(/-/g, '')}`;
    const now = Date.now();

    await env.DB.prepare(
      `INSERT INTO telegram_users (id, username, first_name, created_at, updated_at)
       VALUES (?, 'tguser', 'TelegramUser', ?, ?)`
    )
      .bind(userId, now, now)
      .run();

    const metadataJson = JSON.stringify({ family_name: 'Roboto Test' });
    await env.DB.prepare(
      `INSERT INTO orders (id, user_id, status, total_amount, currency, metadata, created_at, updated_at, completed_at)
       VALUES (?, ?, 'COMPLETED', 50000, 'VND', ?, ?, ?, ?)`
    )
      .bind(orderId, userId, metadataJson, now, now, now)
      .run();

    await env.DB.prepare(
      `INSERT INTO telegram_sessions (id, user_id, chat_id, workflow_token, checkout_token, status, created_at, updated_at)
       VALUES (?, ?, 889977, 'wf_tok', 'chk_tok', 'ORDER_CREATED', ?, ?)`
    )
      .bind(`sess_${crypto.randomUUID()}`, userId, now, now)
      .run();

    const partsCount = params.partsCount || 1;
    const partSizes = params.partSizes || [1024];
    const partsMeta = [];

    for (let i = 1; i <= partsCount; i++) {
      const size = partSizes[i - 1] || 1024;
      const dummyBytes = new Uint8Array(size);
      dummyBytes.fill(i);
      // ZIP header prefix
      dummyBytes[0] = 0x50;
      dummyBytes[1] = 0x4b;
      dummyBytes[2] = 0x03;
      dummyBytes[3] = 0x04;

      const shaBuf = await crypto.subtle.digest('SHA-256', dummyBytes);
      const shaHex = Array.from(new Uint8Array(shaBuf))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('');

      const partKey = `artifacts/${orderId}/${jobId}/${shaHex}.zip`;
      const partFilename = partsCount === 1
        ? `roboto_test_${orderId}.zip`
        : `roboto_test_${orderId}_part-${String(i).padStart(2, '0')}-of-${String(partsCount).padStart(2, '0')}.zip`;

      await env.ARTIFACTS_BUCKET.put(partKey, dummyBytes);

      partsMeta.push({
        part_index: i,
        total_parts: partsCount,
        filename: partFilename,
        artifact_key: partKey,
        artifact_size_bytes: size,
        artifact_sha256: shaHex,
      });
    }

    const firstPart = partsMeta[0];
    await env.DB.prepare(
      `INSERT INTO fulfillment_receipts (job_id, order_id, artifact_key, artifact_size_bytes, artifact_sha256, artifact_parts, completed_at, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
    )
      .bind(
        jobId,
        orderId,
        firstPart.artifact_key,
        firstPart.artifact_size_bytes,
        firstPart.artifact_sha256,
        JSON.stringify(partsMeta),
        now,
        now
      )
      .run();

    const eventId = `outbox_${crypto.randomUUID()}`;
    await env.DB.prepare(
      `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, created_at)
       VALUES (?, 'DELIVERY_READY', 'order', ?, ?, 'PENDING', ?)`
    )
      .bind(eventId, orderId, JSON.stringify({ order_id: orderId, confirmed_parts: [] }), now)
      .run();

    return { orderId, jobId, userId, eventId, partsMeta };
  };

  it('delivers a single-part font bundle directly via Telegram sendDocument and marks SENT', async () => {
    const { orderId, eventId, partsMeta } = await setupOrderWithReceipt({ partsCount: 1 });

    const fetchCalls: Array<{ url: string; formData: FormData }> = [];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('api.telegram.org')) {
        const formData = init?.body instanceof FormData ? init.body : new FormData();
        fetchCalls.push({ url, formData });
        return new Response(JSON.stringify({ ok: true, result: { message_id: 101 } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return originalFetch(input, init);
    };

    try {
      const mockQueue = { send: async () => {}, sendBatch: async () => {} } as unknown as Queue<unknown>;
      const deliveryEnv: Env = {
        ...(env as unknown as Env),
        TELEGRAM_BOT_TOKEN: 'test_bot_token',
        ARTIFACTS_BUCKET: env.ARTIFACTS_BUCKET,
      };

      const outboxService = new OutboxService(env.DB, mockQueue, deliveryEnv);
      const res = await outboxService.dispatchPendingEvents({ batchSize: 10 });

      expect(res.dispatchedCount).toBe(1);
      expect(res.failureCount).toBe(0);

      expect(fetchCalls.length).toBe(1);
      expect(fetchCalls[0].url).toContain('/sendDocument');
      expect(fetchCalls[0].formData.get('chat_id')).toBe('889977');
      expect(fetchCalls[0].formData.get('caption')).toBe('📦 <b>Roboto Test</b>');

      const outbox = await env.DB.prepare('SELECT status, dispatched_at, last_dispatch_error FROM outbox_events WHERE id = ?')
        .bind(eventId)
        .first<{ status: string; dispatched_at: number; last_dispatch_error: string | null }>();

      expect(outbox?.status).toBe('SENT');
      expect(outbox?.dispatched_at).toBeGreaterThan(0);
      expect(outbox?.last_dispatch_error).toBeNull();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('delivers multipart font bundles in ascending order with part captions', async () => {
    const { orderId, eventId, partsMeta } = await setupOrderWithReceipt({
      partsCount: 3,
      partSizes: [2048, 3072, 1024],
    });

    const fetchCalls: Array<{ url: string; formData: FormData }> = [];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('api.telegram.org')) {
        const formData = init?.body instanceof FormData ? init.body : new FormData();
        fetchCalls.push({ url, formData });
        return new Response(JSON.stringify({ ok: true, result: { message_id: 200 + fetchCalls.length } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return originalFetch(input, init);
    };

    try {
      const mockQueue = { send: async () => {}, sendBatch: async () => {} } as unknown as Queue<unknown>;
      const deliveryEnv: Env = {
        ...(env as unknown as Env),
        TELEGRAM_BOT_TOKEN: 'test_bot_token',
        ARTIFACTS_BUCKET: env.ARTIFACTS_BUCKET,
      };

      const outboxService = new OutboxService(env.DB, mockQueue, deliveryEnv);
      const res = await outboxService.dispatchPendingEvents({ batchSize: 10 });

      expect(res.dispatchedCount).toBe(1);
      expect(res.failureCount).toBe(0);

      // Verify all 3 parts sent in order
      expect(fetchCalls.length).toBe(3);
      expect(fetchCalls[0].formData.get('caption')).toBe('📦 <b>Roboto Test</b> (Part 1/3)');
      expect(fetchCalls[1].formData.get('caption')).toBe('📦 <b>Roboto Test</b> (Part 2/3)');
      expect(fetchCalls[2].formData.get('caption')).toBe('📦 <b>Roboto Test</b> (Part 3/3)');

      const outbox = await env.DB.prepare('SELECT status, payload FROM outbox_events WHERE id = ?')
        .bind(eventId)
        .first<{ status: string; payload: string }>();

      expect(outbox?.status).toBe('SENT');
      const payload = JSON.parse(outbox?.payload || '{}');
      expect(payload.confirmed_parts).toEqual([1, 2, 3]);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('resumes multipart delivery on retry without resending already confirmed parts', async () => {
    const { orderId, eventId, partsMeta } = await setupOrderWithReceipt({
      partsCount: 3,
      partSizes: [1000, 1000, 1000],
    });

    let sendAttempts = 0;
    const fetchCalls: Array<{ url: string; formData: FormData }> = [];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('api.telegram.org')) {
        sendAttempts++;
        const formData = init?.body instanceof FormData ? init.body : new FormData();
        fetchCalls.push({ url, formData });
        if (sendAttempts === 2) {
          // Telegram API failure on part 2
          return new Response(JSON.stringify({ ok: false, description: 'Telegram rate limit / network drop' }), {
            status: 429,
          });
        }
        return new Response(JSON.stringify({ ok: true, result: { message_id: 300 + sendAttempts } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return originalFetch(input, init);
    };

    try {
      const mockQueue = { send: async () => {}, sendBatch: async () => {} } as unknown as Queue<unknown>;
      const deliveryEnv: Env = {
        ...(env as unknown as Env),
        TELEGRAM_BOT_TOKEN: 'test_bot_token',
        ARTIFACTS_BUCKET: env.ARTIFACTS_BUCKET,
      };

      const outboxService = new OutboxService(env.DB, mockQueue, deliveryEnv);

      // Attempt 1: Part 1 succeeds, Part 2 fails -> outbox stays PENDING
      const res1 = await outboxService.dispatchPendingEvents({ batchSize: 10 });
      expect(res1.failureCount).toBe(1);
      expect(res1.dispatchedCount).toBe(0);

      const outboxAfterFail = await env.DB.prepare('SELECT status, payload, dispatch_attempts FROM outbox_events WHERE id = ?')
        .bind(eventId)
        .first<{ status: string; payload: string; dispatch_attempts: number }>();

      expect(outboxAfterFail?.status).toBe('PENDING');
      expect(outboxAfterFail?.dispatch_attempts).toBe(1);
      const payload1 = JSON.parse(outboxAfterFail?.payload || '{}');
      // Part 1 is durably confirmed in outbox payload
      expect(payload1.confirmed_parts).toEqual([1]);

      // Fast-forward next_dispatch_at for retry
      await env.DB.prepare('UPDATE outbox_events SET next_dispatch_at = ? WHERE id = ?')
        .bind(Date.now() - 1000, eventId)
        .run();

      // Attempt 2: Resume retry -> should ONLY send Part 2 and Part 3 (skipping Part 1)
      const res2 = await outboxService.dispatchPendingEvents({ batchSize: 10 });
      expect(res2.dispatchedCount).toBe(1);
      expect(res2.failureCount).toBe(0);

      // Total fetch calls = 1 (part 1) + 1 (part 2 fail) + 1 (part 2 retry) + 1 (part 3) = 4
      expect(fetchCalls.length).toBe(4);
      expect(fetchCalls[2].formData.get('caption')).toBe('📦 <b>Roboto Test</b> (Part 2/3)');
      expect(fetchCalls[3].formData.get('caption')).toBe('📦 <b>Roboto Test</b> (Part 3/3)');

      const outboxFinal = await env.DB.prepare('SELECT status, payload FROM outbox_events WHERE id = ?')
        .bind(eventId)
        .first<{ status: string; payload: string }>();

      expect(outboxFinal?.status).toBe('SENT');
      const finalPayload = JSON.parse(outboxFinal?.payload || '{}');
      expect(finalPayload.confirmed_parts).toEqual([1, 2, 3]);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('fails closed and keeps DELIVERY_READY PENDING when canonical R2 part is missing or corrupted', async () => {
    const { orderId, eventId, partsMeta } = await setupOrderWithReceipt({
      partsCount: 2,
      partSizes: [1000, 1000],
    });

    // Corrupt part 2 in R2 by overwriting with invalid checksum
    const corruptBytes = new TextEncoder().encode('corrupt_part_data');
    await env.ARTIFACTS_BUCKET.put(partsMeta[1].artifact_key, corruptBytes);

    let telegramSent = false;
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('api.telegram.org')) {
        telegramSent = true;
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      return originalFetch(input, init);
    };

    try {
      const mockQueue = { send: async () => {}, sendBatch: async () => {} } as unknown as Queue<unknown>;
      const deliveryEnv: Env = {
        ...(env as unknown as Env),
        TELEGRAM_BOT_TOKEN: 'test_bot_token',
        ARTIFACTS_BUCKET: env.ARTIFACTS_BUCKET,
      };

      const outboxService = new OutboxService(env.DB, mockQueue, deliveryEnv);
      const res = await outboxService.dispatchPendingEvents({ batchSize: 10 });

      expect(res.failureCount).toBe(1);
      expect(res.dispatchedCount).toBe(0);
      // Telegram was NOT called because preflight check rejected corrupted R2 part before sending
      expect(telegramSent).toBe(false);

      const outbox = await env.DB.prepare('SELECT status, last_dispatch_error FROM outbox_events WHERE id = ?')
        .bind(eventId)
        .first<{ status: string; last_dispatch_error: string }>();

      expect(outbox?.status).toBe('PENDING');
      expect(outbox?.last_dispatch_error).toBeDefined();

      // Order and job correctness remains unaffected
      const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?').bind(orderId).first<{ status: string }>();
      expect(order?.status).toBe('COMPLETED');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

import { describe, it, expect } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker, { type Env } from '../src/index';
import { JobService, buildArtifactStorageKey } from '../src/services/job-service';
import { OutboxService } from '../src/services/outbox-service';

const NODE_SECRET = 'test_a23_node_secret_phase7';
const SIGNING_SECRET = 'test_download_signing_secret_phase7';
const BASE_URL = 'https://telefont.example.com';

const testEnv: Env = {
  ...(env as unknown as Env),
  A23_NODE_SECRET: NODE_SECRET,
  DOWNLOAD_SIGNING_SECRET: SIGNING_SECRET,
  BASE_URL,
  DOWNLOAD_URL_TTL_SECONDS: '86400',
  TELEGRAM_BOT_TOKEN: 'fake_bot_token',
};

async function setupOrderAndJob(): Promise<{ orderId: string; jobId: string }> {
  const orderId = `ord_mc_${crypto.randomUUID().replace(/-/g, '')}`;
  const jobId = `job_mc_${crypto.randomUUID().replace(/-/g, '')}`;
  const userId = `user_mc_${crypto.randomUUID()}`;
  const now = Date.now();

  await env.DB.prepare(
    `INSERT INTO telegram_users (id, username, first_name, created_at, updated_at)
     VALUES (?, 'mc_user', 'MultiConsumer', ?, ?)`
  )
    .bind(userId, now, now)
    .run();

  await env.DB.prepare(
    `INSERT INTO orders (id, user_id, status, total_amount, currency, metadata, created_at, updated_at)
     VALUES (?, ?, 'PAID', 100000, 'VND', ?, ?, ?)`
  )
    .bind(
      orderId,
      userId,
      JSON.stringify({
        source_url: 'https://www.myfonts.com/fonts/foundry/mc-font/',
        selected_formats: ['TTF', 'OTF'],
      }),
      now,
      now
    )
    .run();

  await env.DB.prepare(
    `INSERT INTO order_items (id, order_id, font_id, font_name, price, created_at)
     VALUES (?, ?, 's1', 'Regular', 100000, ?)`
  )
    .bind(`item_${crypto.randomUUID()}`, orderId, now)
    .run();

  await env.DB.prepare(
    `INSERT INTO telegram_sessions (id, user_id, chat_id, workflow_token, checkout_token, status, created_at, updated_at)
     VALUES (?, ?, 123456789, 'wf_mc', 'chk_mc', 'ORDER_CREATED', ?, ?)`
  )
    .bind(`sess_${crypto.randomUUID()}`, userId, now, now)
    .run();

  await env.DB.prepare(
    `INSERT INTO fulfillment_jobs (id, order_id, status, attempt_count, max_attempts, created_at, updated_at)
     VALUES (?, ?, 'PENDING', 0, 3, ?, ?)`
  )
    .bind(jobId, orderId, now, now)
    .run();

  return { orderId, jobId };
}

async function uploadValidArtifactToR2(orderId: string, jobId: string, dummyBytes: Uint8Array) {
  const shaBuffer = await crypto.subtle.digest('SHA-256', dummyBytes);
  const sha256Hex = Array.from(new Uint8Array(shaBuffer))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
  const artifactKey = buildArtifactStorageKey(orderId, jobId, sha256Hex);

  await env.ARTIFACTS_BUCKET.put(artifactKey, dummyBytes, {
    sha256: shaBuffer,
    customMetadata: {
      job_id: jobId,
      order_id: orderId,
      sha256: sha256Hex,
    },
    httpMetadata: {
      contentType: 'application/zip',
      contentDisposition: `attachment; filename="${orderId}.zip"`,
    },
  });

  return { artifactKey, sha256Hex, size: dummyBytes.byteLength };
}

describe('Phase 7: Horizontal Multi-Consumer Concurrency & Recovery Proofs', () => {
  it('1. Worker loss during compute -> lease expiry -> reclaim by 2nd worker -> singular fulfillment & receipt', async () => {
    const { orderId, jobId } = await setupOrderAndJob();
    const jobService = new JobService(env.DB);

    // Worker 1 claims job
    const claim1 = await jobService.claimJob(jobId, 'worker-1', 60);
    expect(claim1.status).toBe('CLAIMED');
    const lease1 = claim1.payload!.lease_token;

    // Worker 1 crashes; simulate lease expiration
    await env.DB.prepare('UPDATE fulfillment_jobs SET lease_expires_at = ? WHERE id = ?')
      .bind(Date.now() - 5000, jobId)
      .run();

    // Worker 2 reclaims the expired job
    const claim2 = await jobService.claimJob(jobId, 'worker-2', 300);
    expect(claim2.status).toBe('CLAIMED');
    const lease2 = claim2.payload!.lease_token;
    expect(lease2).not.toBe(lease1);

    // Worker 2 uploads artifact and completes job
    const dummyZip = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x11, 0x22]);
    const { artifactKey, sha256Hex, size } = await uploadValidArtifactToR2(orderId, jobId, dummyZip);

    const completeRes2 = await jobService.completeJob({
      jobId,
      workerId: 'worker-2',
      leaseToken: lease2,
      artifactKey,
      artifactSha256: sha256Hex,
      artifactSizeBytes: size,
    });
    expect(completeRes2.status).toBe('COMPLETED');

    // Stale Worker 1 attempts complete -> safely acknowledged as ALREADY_COMPLETED / FENCED without duplicate receipts
    const completeRes1 = await jobService.completeJob({
      jobId,
      workerId: 'worker-1',
      leaseToken: lease1,
      artifactKey,
      artifactSha256: sha256Hex,
      artifactSizeBytes: size,
    });
    expect(['ALREADY_COMPLETED', 'EXPIRED_OR_FENCED']).toContain(completeRes1.status);
    expect(completeRes1.queue_action).toBe('ack');

    // Verify exactly ONE receipt exists in D1
    const receipts = await env.DB.prepare('SELECT * FROM fulfillment_receipts WHERE job_id = ?')
      .bind(jobId)
      .all();
    expect(receipts.results.length).toBe(1);

    // Verify order is COMPLETED
    const order = await env.DB.prepare('SELECT status, completed_at FROM orders WHERE id = ?')
      .bind(orderId)
      .first<{ status: string; completed_at: number }>();
    expect(order?.status).toBe('COMPLETED');
    expect(order?.completed_at).toBeGreaterThan(0);

    // Verify exactly ONE DELIVERY_READY outbox event exists
    const outbox = await env.DB.prepare(
      'SELECT * FROM outbox_events WHERE aggregate_id = ? AND event_type = "DELIVERY_READY"'
    )
      .bind(orderId)
      .all();
    expect(outbox.results.length).toBe(1);
  });

  it('2. Concurrent claim race between 3 logical workers produces exactly one winner', async () => {
    const { jobId } = await setupOrderAndJob();
    const jobService = new JobService(env.DB);

    // 3 workers attempt simultaneous claim
    const [c1, c2, c3] = await Promise.all([
      jobService.claimJob(jobId, 'worker-A', 300),
      jobService.claimJob(jobId, 'worker-B', 300),
      jobService.claimJob(jobId, 'worker-C', 300),
    ]);

    const claimed = [c1, c2, c3].filter((c) => c.status === 'CLAIMED');
    const rejected = [c1, c2, c3].filter((c) => c.status !== 'CLAIMED');

    expect(claimed.length).toBe(1);
    expect(rejected.length).toBe(2);
    for (const r of rejected) {
      expect(['CONFLICT', 'LEASED']).toContain(r.status);
      expect(r.queue_action).toBe('retry');
    }
  });

  it('3. Completion response ambiguity / retry returns 200 with queue_action: ack without duplicate mutations', async () => {
    const { orderId, jobId } = await setupOrderAndJob();
    const jobService = new JobService(env.DB);

    const claimRes = await jobService.claimJob(jobId, 'worker-1', 300);
    const leaseToken = claimRes.payload!.lease_token;

    const dummyZip = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x33, 0x44]);
    const { artifactKey, sha256Hex, size } = await uploadValidArtifactToR2(orderId, jobId, dummyZip);

    const reqComplete = () =>
      new Request(`http://example.com/internal/jobs/${jobId}/complete`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: leaseToken,
          artifact_key: artifactKey,
          sha256: sha256Hex,
          size,
        }),
      });

    // First completion
    const ctx1 = createExecutionContext();
    const res1 = await worker.fetch(reqComplete(), testEnv, ctx1);
    await waitOnExecutionContext(ctx1);
    expect(res1.status).toBe(200);

    // Second retry completion (simulating client retry after dropped connection)
    const ctx2 = createExecutionContext();
    const res2 = await worker.fetch(reqComplete(), testEnv, ctx2);
    await waitOnExecutionContext(ctx2);
    expect(res2.status).toBe(200);

    const data2 = (await res2.json()) as { success: boolean; status: string; queue_action: string };
    expect(data2.success).toBe(true);
    expect(data2.status).toBe('COMPLETED');
    expect(data2.queue_action).toBe('ack');

    // Assert singular receipt and singular outbox event
    const receipts = await env.DB.prepare('SELECT * FROM fulfillment_receipts WHERE job_id = ?')
      .bind(jobId)
      .all();
    expect(receipts.results.length).toBe(1);

    const outbox = await env.DB.prepare(
      'SELECT * FROM outbox_events WHERE aggregate_id = ? AND event_type = "DELIVERY_READY"'
    )
      .bind(orderId)
      .all();
    expect(outbox.results.length).toBe(1);
  });

  it('4. Queue ACK loss / redelivery on completed job returns TERMINAL 409 and ACKs Queue immediately', async () => {
    const { orderId, jobId } = await setupOrderAndJob();
    const jobService = new JobService(env.DB);

    const claimRes = await jobService.claimJob(jobId, 'worker-1', 300);
    const leaseToken = claimRes.payload!.lease_token;

    const dummyZip = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x55, 0x66]);
    const { artifactKey, sha256Hex, size } = await uploadValidArtifactToR2(orderId, jobId, dummyZip);

    // Complete job
    await jobService.completeJob({
      jobId,
      workerId: 'worker-1',
      leaseToken,
      artifactKey,
      artifactSha256: sha256Hex,
      artifactSizeBytes: size,
    });

    // Simulated Queue redelivery: Worker attempts to claim completed job
    const reqClaim = new Request(`http://example.com/internal/jobs/${jobId}/claim`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${NODE_SECRET}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        worker_id: 'worker-2',
        lease_seconds: 300,
      }),
    });

    const ctx = createExecutionContext();
    const resClaim = await worker.fetch(reqClaim, testEnv, ctx);
    await waitOnExecutionContext(ctx);

    expect(resClaim.status).toBe(409);
    const data = (await resClaim.json()) as { status: string; queue_action: string; reason: string };
    expect(data.status).toBe('TERMINAL');
    expect(data.queue_action).toBe('ack');
  });

  it('5. DELIVERY_READY outbox retry backoff delivers exactly once to Telegram', async () => {
    const { orderId, jobId } = await setupOrderAndJob();
    const now = Date.now();

    // Insert receipt and R2 artifact
    const artifactKey = `artifacts/${orderId}/${jobId}/bundle.zip`;
    const dummyZip = new TextEncoder().encode('PK\x05\x06dummy_zip_content');
    const shaBuf = await crypto.subtle.digest('SHA-256', dummyZip);
    const shaHex = Array.from(new Uint8Array(shaBuf))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');

    await env.DB.prepare(
      `INSERT INTO fulfillment_receipts (job_id, order_id, artifact_key, artifact_size_bytes, artifact_sha256, completed_at, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)`
    )
      .bind(jobId, orderId, artifactKey, dummyZip.byteLength, shaHex, now, now)
      .run();

    await env.ARTIFACTS_BUCKET.put(artifactKey, dummyZip);

    // Mark order COMPLETED
    await env.DB.prepare('UPDATE orders SET status = "COMPLETED", completed_at = ? WHERE id = ?')
      .bind(now, orderId)
      .run();

    // Clear prior pending outbox events
    await env.DB.prepare('DELETE FROM outbox_events WHERE status = "PENDING"').run();

    const eventId = `outbox_del_retry_${crypto.randomUUID()}`;
    await env.DB.prepare(
      `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, created_at)
       VALUES (?, 'DELIVERY_READY', 'order', ?, ?, 'PENDING', ?)`
    )
      .bind(eventId, orderId, JSON.stringify({ order_id: orderId }), now)
      .run();

    let telegramAttempt = 0;
    const originalFetch = globalThis.fetch;

    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('api.telegram.org')) {
        telegramAttempt++;
        if (telegramAttempt < 2) {
          // Transient failure on 1st attempt
          return new Response(JSON.stringify({ ok: false, description: 'Internal Telegram Error' }), {
            status: 500,
          });
        }
        // Success on 2nd attempt
        return new Response(JSON.stringify({ ok: true, result: { message_id: 999 } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return originalFetch(input, init);
    };

    try {
      const mockQueue = { send: async () => {}, sendBatch: async () => {} } as unknown as Queue<unknown>;
      const deliveryEnv: Env = {
        ...testEnv,
        ARTIFACTS_BUCKET: env.ARTIFACTS_BUCKET,
      };
      const outboxService = new OutboxService(env.DB, mockQueue, deliveryEnv);

      // 1st dispatch attempt -> fails, stays PENDING
      const res1 = await outboxService.dispatchPendingEvents({ batchSize: 10 });
      expect(res1.failureCount).toBe(1);
      expect(res1.dispatchedCount).toBe(0);

      const eventAfterFail = await env.DB.prepare('SELECT status, dispatch_attempts, next_dispatch_at FROM outbox_events WHERE id = ?')
        .bind(eventId)
        .first<{ status: string; dispatch_attempts: number; next_dispatch_at: number }>();
      expect(eventAfterFail?.status).toBe('PENDING');
      expect(eventAfterFail?.dispatch_attempts).toBe(1);

      // Fast-forward next_dispatch_at to enable retry
      await env.DB.prepare('UPDATE outbox_events SET next_dispatch_at = ? WHERE id = ?')
        .bind(Date.now() - 1000, eventId)
        .run();

      // 2nd dispatch attempt -> succeeds, marked SENT
      const res2 = await outboxService.dispatchPendingEvents({ batchSize: 10 });
      expect(res2.dispatchedCount).toBe(1);

      const eventAfterSuccess = await env.DB.prepare('SELECT status, dispatched_at FROM outbox_events WHERE id = ?')
        .bind(eventId)
        .first<{ status: string; dispatched_at: number }>();
      expect(eventAfterSuccess?.status).toBe('SENT');
      expect(eventAfterSuccess?.dispatched_at).toBeGreaterThan(0);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

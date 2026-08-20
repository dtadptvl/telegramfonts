import { describe, it, expect } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker, { type Env } from '../src/index';
import { JobService } from '../src/services/job-service';

const NODE_SECRET = 'a23_super_secret_node_key_999';

const testEnv: Env = {
  ...(env as unknown as Env),
  A23_NODE_SECRET: NODE_SECRET,
  A23_JOB_LEASE_SECONDS: '300',
};

async function setupTestJob(status = 'PENDING', customOrderId?: string) {
  const orderId = customOrderId || `ord_${crypto.randomUUID().replace(/-/g, '')}`;
  const jobId = `job_${crypto.randomUUID().replace(/-/g, '')}`;
  const paymentCode = `TF${crypto.randomUUID().replace(/[^a-zA-Z0-9]/g, '').slice(0, 6).toUpperCase()}`;
  const now = Date.now();

  const metadata = JSON.stringify({
    source_url: 'https://www.myfonts.com/collections/roboto-flex',
    family_name: 'Roboto Flex',
    foundry: 'Google Fonts',
    selected_formats: ['TTF', 'OTF'],
  });

  // 1. Insert order
  await env.DB.prepare(
    `INSERT INTO orders (id, user_id, status, total_amount, currency, metadata, payment_code, created_at, updated_at)
     VALUES (?, 12345, 'PAID', 100000, 'VND', ?, ?, ?, ?)`
  )
    .bind(orderId, metadata, paymentCode, now, now)
    .run();

  // 2. Insert order items
  await env.DB.prepare(
    `INSERT INTO order_items (id, order_id, font_id, font_name, price, created_at)
     VALUES (?, ?, 'rf_regular', 'Roboto Flex Regular', 50000, ?)`
  )
    .bind(`item_1_${crypto.randomUUID()}`, orderId, now)
    .run();

  await env.DB.prepare(
    `INSERT INTO order_items (id, order_id, font_id, font_name, price, created_at)
     VALUES (?, ?, 'rf_bold', 'Roboto Flex Bold', 50000, ?)`
  )
    .bind(`item_2_${crypto.randomUUID()}`, orderId, now)
    .run();

  // 3. Insert fulfillment job
  await env.DB.prepare(
    `INSERT INTO fulfillment_jobs (id, order_id, status, attempt_count, max_attempts, created_at, updated_at)
     VALUES (?, ?, ?, 0, 3, ?, ?)`
  )
    .bind(jobId, orderId, status, now, now)
    .run();

  return { orderId, jobId };
}

describe('Phase 4: A23 Internal Node Job Claim, Lease Fencing & Protocols', () => {
  describe('Internal Node Authentication & Security', () => {
    it('returns 503 when A23_NODE_SECRET is missing in env', async () => {
      const mockEnv: Env = {
        ...(env as unknown as Env),
        A23_NODE_SECRET: undefined,
      };

      const req = new Request('http://example.com/internal/jobs/job_1/claim', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${NODE_SECRET}`,
        },
        body: JSON.stringify({ worker_id: 'worker-1' }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, mockEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(503);
    });

    it('returns 401 on missing Authorization header or wrong bearer format', async () => {
      const reqNoAuth = new Request('http://example.com/internal/jobs/job_1/claim', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ worker_id: 'worker-1' }),
      });

      const ctx1 = createExecutionContext();
      const resNoAuth = await worker.fetch(reqNoAuth, testEnv, ctx1);
      await waitOnExecutionContext(ctx1);
      expect(resNoAuth.status).toBe(401);

      const reqBadScheme = new Request('http://example.com/internal/jobs/job_1/claim', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Basic ${NODE_SECRET}`,
        },
        body: JSON.stringify({ worker_id: 'worker-1' }),
      });

      const ctx2 = createExecutionContext();
      const resBadScheme = await worker.fetch(reqBadScheme, testEnv, ctx2);
      await waitOnExecutionContext(ctx2);
      expect(resBadScheme.status).toBe(401);
    });

    it('returns 401 on invalid / mismatched bearer secret', async () => {
      const req = new Request('http://example.com/internal/jobs/job_1/claim', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: 'Bearer wrong_secret_token_123',
        },
        body: JSON.stringify({ worker_id: 'worker-1' }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(401);
    });
  });

  describe('Job Claiming & Fenced Lease Protocol', () => {
    it('claims PENDING job -> transitions job to PROCESSING, order PAID -> PROCESSING atomically, and returns compute payload', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');

      const req = new Request(`http://example.com/internal/jobs/${jobId}/claim`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${NODE_SECRET}`,
        },
        body: JSON.stringify({ worker_id: 'worker-node-1' }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
      const data = (await res.json()) as any;

      // Verify returned compute contract
      expect(data.job_id).toBe(jobId);
      expect(data.order_id).toBe(orderId);
      expect(data.lease_token).toBeDefined();
      expect(data.lease_expires_at).toBeGreaterThan(Date.now());
      expect(data.source_url).toBe('https://www.myfonts.com/collections/roboto-flex');
      expect(data.family_name).toBe('Roboto Flex');
      expect(data.foundry).toBe('Google Fonts');
      expect(data.formats).toEqual(['TTF', 'OTF']);
      expect(data.styles).toEqual([
        { id: 'rf_regular', display_name: 'Roboto Flex Regular' },
        { id: 'rf_bold', display_name: 'Roboto Flex Bold' },
      ]);

      // Ensure no sensitive payment/telegram data in response
      expect(data.payment_code).toBeUndefined();
      expect(data.user_id).toBeUndefined();
      expect(data.amount).toBeUndefined();

      // Verify D1 state transitions
      const jobRow = await env.DB.prepare('SELECT * FROM fulfillment_jobs WHERE id = ?')
        .bind(jobId)
        .first<{
          status: string;
          lease_owner: string;
          lease_token: string;
          attempt_count: number;
        }>();

      expect(jobRow?.status).toBe('PROCESSING');
      expect(jobRow?.lease_owner).toBe('worker-node-1');
      expect(jobRow?.lease_token).toBe(data.lease_token);
      expect(jobRow?.attempt_count).toBe(1);

      const orderRow = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
        .bind(orderId)
        .first<{ status: string }>();
      expect(orderRow?.status).toBe('PROCESSING');
    });

    it('concurrent claim attempts for the same job result in exactly one active winner lease', async () => {
      const { jobId } = await setupTestJob('PENDING');

      const makeClaimReq = (workerId: string) =>
        new Request(`http://example.com/internal/jobs/${jobId}/claim`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${NODE_SECRET}`,
          },
          body: JSON.stringify({ worker_id: workerId }),
        });

      const ctxA = createExecutionContext();
      const ctxB = createExecutionContext();

      const [resA, resB] = await Promise.all([
        worker.fetch(makeClaimReq('worker-A'), testEnv, ctxA),
        worker.fetch(makeClaimReq('worker-B'), testEnv, ctxB),
      ]);

      await Promise.all([waitOnExecutionContext(ctxA), waitOnExecutionContext(ctxB)]);

      const statuses = [resA.status, resB.status].sort();
      expect(statuses).toEqual([200, 409]);

      const jobRow = await env.DB.prepare('SELECT status, attempt_count FROM fulfillment_jobs WHERE id = ?')
        .bind(jobId)
        .first<{ status: string; attempt_count: number }>();
      expect(jobRow?.status).toBe('PROCESSING');
      expect(jobRow?.attempt_count).toBe(1);
    });

    it('expired PROCESSING lease can be reclaimed with fresh token, fencing out stale worker', async () => {
      const { jobId } = await setupTestJob('PENDING');

      const jobService = new JobService(env.DB);
      // Claim initially with 10 second lease
      const claim1 = await jobService.claimJob(jobId, 'worker-1', 10);
      expect(claim1.status).toBe('CLAIMED');
      const staleToken = claim1.payload!.lease_token;

      // Expire lease by advancing DB timestamp
      const pastTime = Date.now() - 5000;
      await env.DB.prepare('UPDATE fulfillment_jobs SET lease_expires_at = ? WHERE id = ?')
        .bind(pastTime, jobId)
        .run();

      // Second worker reclaims expired job
      const claim2 = await jobService.claimJob(jobId, 'worker-2', 300);
      expect(claim2.status).toBe('CLAIMED');
      expect(claim2.payload!.lease_token).not.toBe(staleToken);

      // Stale worker tries to heartbeat -> rejected/fenced
      const staleHeartbeat = await jobService.heartbeat(jobId, 'worker-1', staleToken);
      expect(staleHeartbeat.status).toBe('EXPIRED_OR_FENCED');
      expect(staleHeartbeat.queue_action).toBe('ack');

      // Stale worker tries to fail -> rejected/fenced
      const staleFail = await jobService.failJob({
        jobId,
        workerId: 'worker-1',
        leaseToken: staleToken,
        retryable: true,
      });
      expect(staleFail.status).toBe('EXPIRED_OR_FENCED');
      expect(staleFail.queue_action).toBe('ack');
    });

    it('rejects claim on RETRY job before next_retry_at is due; allows claim once due', async () => {
      const { jobId } = await setupTestJob('RETRY');
      const futureTime = Date.now() + 60000; // 60s in future

      await env.DB.prepare('UPDATE fulfillment_jobs SET next_retry_at = ? WHERE id = ?')
        .bind(futureTime, jobId)
        .run();

      const jobService = new JobService(env.DB);

      // Attempt claim before due
      const earlyClaim = await jobService.claimJob(jobId, 'worker-1');
      expect(earlyClaim.status).toBe('RETRY_NOT_DUE');
      expect(earlyClaim.queue_action).toBe('retry');

      // Update next_retry_at to past
      await env.DB.prepare('UPDATE fulfillment_jobs SET next_retry_at = ? WHERE id = ?')
        .bind(Date.now() - 1000, jobId)
        .run();

      // Due retry succeeds
      const dueClaim = await jobService.claimJob(jobId, 'worker-1');
      expect(dueClaim.status).toBe('CLAIMED');
    });
  });

  describe('Heartbeat & Lease Extension', () => {
    it('extends active lease when token and worker_id match current lease', async () => {
      const { jobId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);

      const claim = await jobService.claimJob(jobId, 'worker-1', 100);
      const token = claim.payload!.lease_token;
      const originalExpiresAt = claim.payload!.lease_expires_at;

      const req = new Request(`http://example.com/internal/jobs/${jobId}/heartbeat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${NODE_SECRET}`,
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: token,
          extend_seconds: 500,
        }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
      const data = (await res.json()) as any;
      expect(data.success).toBe(true);
      expect(data.lease_expires_at).toBeGreaterThan(originalExpiresAt);
    });

    it('returns 409 when heartbeat is called with invalid or expired token', async () => {
      const { jobId } = await setupTestJob('PENDING');

      const req = new Request(`http://example.com/internal/jobs/${jobId}/heartbeat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${NODE_SECRET}`,
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: 'fake_non_existent_token',
        }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(409);
      const data = (await res.json()) as any;
      expect(data.queue_action).toBe('ack');
    });
  });

  describe('Failure & Retry Protocol', () => {
    it('transitions to RETRY with bounded delay when retryable is true and attempts remain', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);

      const claim = await jobService.claimJob(jobId, 'worker-1');
      const token = claim.payload!.lease_token;

      const req = new Request(`http://example.com/internal/jobs/${jobId}/fail`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${NODE_SECRET}`,
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: token,
          retryable: true,
          reason_code: 'DOWNLOAD_FAILED',
        }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
      const data = (await res.json()) as any;
      expect(data.status).toBe('RETRY');
      expect(data.queue_action).toBe('retry');
      expect(data.delay_seconds).toBeGreaterThan(0);
      expect(data.next_retry_at).toBeGreaterThan(Date.now());

      // Job is in RETRY, lease cleared
      const jobRow = await env.DB.prepare('SELECT * FROM fulfillment_jobs WHERE id = ?')
        .bind(jobId)
        .first<{ status: string; lease_owner: string | null; last_error: string }>();
      expect(jobRow?.status).toBe('RETRY');
      expect(jobRow?.lease_owner).toBeNull();
      expect(jobRow?.last_error).toBe('DOWNLOAD_FAILED');

      // Order remains in PROCESSING
      const orderRow = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
        .bind(orderId)
        .first<{ status: string }>();
      expect(orderRow?.status).toBe('PROCESSING');
    });

    it('transitions job and order atomically to FAILED when retryable is false', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);

      const claim = await jobService.claimJob(jobId, 'worker-1');
      const token = claim.payload!.lease_token;

      const req = new Request(`http://example.com/internal/jobs/${jobId}/fail`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${NODE_SECRET}`,
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: token,
          retryable: false,
          reason_code: 'UNSUPPORTED_FONT_FORMAT',
        }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
      const data = (await res.json()) as any;
      expect(data.status).toBe('FAILED');
      expect(data.queue_action).toBe('ack');
      expect(data.reason).toBe('UNSUPPORTED_FONT_FORMAT');

      // Job FAILED
      const jobRow = await env.DB.prepare('SELECT * FROM fulfillment_jobs WHERE id = ?')
        .bind(jobId)
        .first<{ status: string; last_error: string }>();
      expect(jobRow?.status).toBe('FAILED');
      expect(jobRow?.last_error).toBe('UNSUPPORTED_FONT_FORMAT');

      // Order FAILED
      const orderRow = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
        .bind(orderId)
        .first<{ status: string }>();
      expect(orderRow?.status).toBe('FAILED');
    });

    it('transitions job and order to FAILED when max_attempts is exhausted even if retryable is true', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);

      // Set attempt count near limit (2 of 3)
      await env.DB.prepare('UPDATE fulfillment_jobs SET attempt_count = 2 WHERE id = ?')
        .bind(jobId)
        .run();

      const claim = await jobService.claimJob(jobId, 'worker-1');
      expect(claim.status).toBe('CLAIMED');
      const token = claim.payload!.lease_token;

      // Now attempt count is 3 (max_attempts = 3)
      const req = new Request(`http://example.com/internal/jobs/${jobId}/fail`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${NODE_SECRET}`,
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: token,
          retryable: true, // requested retry, but exhausted
          reason_code: 'RETRYABLE_ERROR',
        }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
      const data = (await res.json()) as any;
      expect(data.status).toBe('FAILED');
      expect(data.queue_action).toBe('ack');
      expect(data.reason).toBe('max_attempts_exhausted');

      const jobRow = await env.DB.prepare('SELECT status FROM fulfillment_jobs WHERE id = ?')
        .bind(jobId)
        .first<{ status: string }>();
      expect(jobRow?.status).toBe('FAILED');

      const orderRow = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
        .bind(orderId)
        .first<{ status: string }>();
      expect(orderRow?.status).toBe('FAILED');
    });
  });

  describe('Defensive Metadata & Malformed Canonical State', () => {
    it('fails safely and returns recoverable error when persisted order metadata is corrupted JSON', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');

      // Corrupt order metadata in DB
      await env.DB.prepare('UPDATE orders SET metadata = "corrupt{invalid-json" WHERE id = ?')
        .bind(orderId)
        .run();

      const req = new Request(`http://example.com/internal/jobs/${jobId}/claim`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${NODE_SECRET}`,
        },
        body: JSON.stringify({ worker_id: 'worker-1' }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(500);
      const data = (await res.json()) as any;
      expect(data.queue_action).toBe('retry');
    });
  });
});

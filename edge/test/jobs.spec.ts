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

async function setupTestJob(
  status = 'PENDING',
  customOrderId?: string,
  orderStatus = 'PAID',
  attemptCount = 0
) {
  const orderId = customOrderId || `ord_${crypto.randomUUID().replace(/-/g, '')}`;
  const jobId = `job_${crypto.randomUUID().replace(/-/g, '')}`;
  const paymentCode = `TF${crypto.randomUUID().replace(/[^a-zA-Z0-9]/g, '').slice(0, 6).toUpperCase()}`;
  const now = Date.now();

  const metadata = JSON.stringify({
    source_url: 'https://www.myfonts.com/collections/roboto-flex',
    family_name: 'Roboto Flex',
    foundry: 'Google Fonts',
    selected_formats: ['TTF', 'OTF'],
    mode: 'ORIGINAL',
  });

  // 1. Insert order (mode = ORIGINAL: T-PRICE-01 durable mode identity)
  await env.DB.prepare(
    `INSERT INTO orders (id, user_id, status, total_amount, currency, metadata, payment_code, mode, created_at, updated_at)
     VALUES (?, 12345, ?, 100000, 'VND', ?, ?, 'ORIGINAL', ?, ?)`
  )
    .bind(orderId, orderStatus, metadata, paymentCode, now, now)
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
     VALUES (?, ?, ?, ?, 3, ?, ?)`
  )
    .bind(jobId, orderId, status, attemptCount, now, now)
    .run();

  return { orderId, jobId, paymentCode };
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

  describe('Internal Request Boundary Validation (BLOCK 6)', () => {
    it('rejects invalid worker_id characters or empty worker_id with 400', async () => {
      const reqInvalidChars = new Request('http://example.com/internal/jobs/job_1/claim', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${NODE_SECRET}`,
        },
        body: JSON.stringify({ worker_id: 'worker!#$invalid' }),
      });

      const ctx1 = createExecutionContext();
      const res1 = await worker.fetch(reqInvalidChars, testEnv, ctx1);
      await waitOnExecutionContext(ctx1);
      expect(res1.status).toBe(400);

      const reqEmpty = new Request('http://example.com/internal/jobs/job_1/claim', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${NODE_SECRET}`,
        },
        body: JSON.stringify({ worker_id: '' }),
      });

      const ctx2 = createExecutionContext();
      const res2 = await worker.fetch(reqEmpty, testEnv, ctx2);
      await waitOnExecutionContext(ctx2);
      expect(res2.status).toBe(400);
    });

    it('rejects out-of-bounds lease_seconds or extend_seconds with 400', async () => {
      const reqTooLarge = new Request('http://example.com/internal/jobs/job_1/claim', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${NODE_SECRET}`,
        },
        body: JSON.stringify({ worker_id: 'worker-1', lease_seconds: 99999 }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(reqTooLarge, testEnv, ctx);
      await waitOnExecutionContext(ctx);
      expect(res.status).toBe(400);
    });

    it('rejects non-boolean retryable or invalid reason_code with 400', async () => {
      const reqBadRetryable = new Request('http://example.com/internal/jobs/job_1/fail', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${NODE_SECRET}`,
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: '12345678-1234-1234-1234-123456789abc',
          retryable: 'yes', // not boolean
        }),
      });

      const ctx1 = createExecutionContext();
      const res1 = await worker.fetch(reqBadRetryable, testEnv, ctx1);
      await waitOnExecutionContext(ctx1);
      expect(res1.status).toBe(400);

      const reqBadReason = new Request('http://example.com/internal/jobs/job_1/fail', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${NODE_SECRET}`,
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: '12345678-1234-1234-1234-123456789abc',
          retryable: false,
          reason_code: 'error with spaces and lowercase!',
        }),
      });

      const ctx2 = createExecutionContext();
      const res2 = await worker.fetch(reqBadReason, testEnv, ctx2);
      await waitOnExecutionContext(ctx2);
      expect(res2.status).toBe(400);
    });
  });

  describe('Job Claiming & Fenced Lease Protocol (BLOCK 3)', () => {
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

    it('lost claim CAS results in zero order mutation (BLOCK 3)', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');

      // First worker successfully claims job
      const jobService = new JobService(env.DB);
      const winClaim = await jobService.claimJob(jobId, 'worker-1', 300);
      expect(winClaim.status).toBe('CLAIMED');

      // Now order is in PROCESSING. Attempting to claim again while active results in CONFLICT / LEASED
      const lostClaim = await jobService.claimJob(jobId, 'worker-2', 300);
      expect(lostClaim.status).toBe('LEASED');

      // Order must still be PROCESSING (no mutation)
      const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
        .bind(orderId)
        .first<{ status: string }>();
      expect(order?.status).toBe('PROCESSING');
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

      // Stale worker tries to fail -> rejected/fenced (BLOCK 4)
      const staleFail = await jobService.failJob({
        jobId,
        workerId: 'worker-1',
        leaseToken: staleToken,
        retryable: true,
      });
      expect(staleFail.status).toBe('EXPIRED_OR_FENCED');
      expect(staleFail.queue_action).toBe('ack');
    });

    it('rejects claim on RETRY job before next_retry_at is due; allows claim once due (with order in PROCESSING)', async () => {
      const { jobId } = await setupTestJob('RETRY', undefined, 'PROCESSING', 1);
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

    it('final-attempt crash with expired lease transitions job and order atomically to FAILED on next claim/redelivery (BLOCK 5)', async () => {
      const { jobId, orderId } = await setupTestJob('PROCESSING', undefined, 'PROCESSING', 3);

      // Set attempt count to 3 of 3 (max_attempts = 3), and expired lease
      const pastTime = Date.now() - 10000;
      await env.DB.prepare(
        'UPDATE fulfillment_jobs SET lease_expires_at = ?, lease_token = "prev_token", lease_owner = "crashed_worker" WHERE id = ?'
      )
        .bind(pastTime, jobId)
        .run();

      const jobService = new JobService(env.DB);
      const claimRes = await jobService.claimJob(jobId, 'worker-new');

      expect(claimRes.status).toBe('TERMINAL');
      expect(claimRes.queue_action).toBe('ack');
      expect(claimRes.reason).toBe('max_attempts_exhausted');

      // Assert job and order were transitioned to FAILED atomically (no stranded PROCESSING)
      const job = await env.DB.prepare('SELECT status, last_error FROM fulfillment_jobs WHERE id = ?')
        .bind(jobId)
        .first<{ status: string; last_error: string }>();
      expect(job?.status).toBe('FAILED');
      expect(job?.last_error).toBe('max_attempts_exhausted');

      const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
        .bind(orderId)
        .first<{ status: string }>();
      expect(order?.status).toBe('FAILED');
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
          lease_token: '12345678-1234-1234-1234-123456789abc',
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

  describe('Failure & Retry Protocol (BLOCK 4)', () => {
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

    it('expired-but-not-yet-reclaimed lease cannot call /fail (BLOCK 4)', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);

      const claim = await jobService.claimJob(jobId, 'worker-1', 10);
      const token = claim.payload!.lease_token;

      // Expire lease
      await env.DB.prepare('UPDATE fulfillment_jobs SET lease_expires_at = ? WHERE id = ?')
        .bind(Date.now() - 5000, jobId)
        .run();

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
          reason_code: 'UNRECOVERABLE_ERROR',
        }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(409);

      // Order must remain in PROCESSING, not modified to FAILED (BLOCK 4)
      const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
        .bind(orderId)
        .first<{ status: string }>();
      expect(order?.status).toBe('PROCESSING');
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

  describe('Defensive Metadata & Malformed Canonical State (BLOCK 6 & BLOCK B)', () => {
    it('fails safely and returns recoverable error when persisted order metadata is corrupted JSON or invalid schema', async () => {
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

    it('fails safely with MALFORMED_METADATA when source_url is not canonical HTTPS MyFonts URL (BLOCK B)', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');

      await env.DB.prepare(
        'UPDATE orders SET metadata = ? WHERE id = ?'
      )
        .bind(
          JSON.stringify({
            source_url: 'https://attacker.com/malicious-font',
            selected_formats: ['TTF'],
          }),
          orderId
        )
        .run();

      const jobService = new JobService(env.DB);
      const res = await jobService.claimJob(jobId, 'worker-1');
      expect(res.status).toBe('MALFORMED_METADATA');
      expect(res.queue_action).toBe('retry');
    });

    it('fails safely with MALFORMED_METADATA when formats is missing, empty, or contains invalid formats (BLOCK B)', async () => {
      const jobService = new JobService(env.DB);

      // 1. Missing formats
      const job1 = await setupTestJob('PENDING');
      await env.DB.prepare('UPDATE orders SET metadata = ? WHERE id = ?')
        .bind(
          JSON.stringify({
            source_url: 'https://www.myfonts.com/collections/roboto-flex',
            // missing selected_formats
          }),
          job1.orderId
        )
        .run();

      const resMissing = await jobService.claimJob(job1.jobId, 'worker-1');
      expect(resMissing.status).toBe('MALFORMED_METADATA');

      // 2. Empty formats
      const job2 = await setupTestJob('PENDING');
      await env.DB.prepare('UPDATE orders SET metadata = ? WHERE id = ?')
        .bind(
          JSON.stringify({
            source_url: 'https://www.myfonts.com/collections/roboto-flex',
            selected_formats: [],
          }),
          job2.orderId
        )
        .run();

      const resEmpty = await jobService.claimJob(job2.jobId, 'worker-1');
      expect(resEmpty.status).toBe('MALFORMED_METADATA');

      // 3. Invalid format entry
      const job3 = await setupTestJob('PENDING');
      await env.DB.prepare('UPDATE orders SET metadata = ? WHERE id = ?')
        .bind(
          JSON.stringify({
            source_url: 'https://www.myfonts.com/collections/roboto-flex',
            selected_formats: ['WOFF2'],
          }),
          job3.orderId
        )
        .run();

      const resInvalid = await jobService.claimJob(job3.jobId, 'worker-1');
      expect(resInvalid.status).toBe('MALFORMED_METADATA');
    });

    it('fails safely with MALFORMED_METADATA when order items are empty or contain invalid font IDs (BLOCK B)', async () => {
      const jobService = new JobService(env.DB);

      // 1. Delete all order items
      const job1 = await setupTestJob('PENDING');
      await env.DB.prepare('DELETE FROM order_items WHERE order_id = ?')
        .bind(job1.orderId)
        .run();

      const resNoItems = await jobService.claimJob(job1.jobId, 'worker-1');
      expect(resNoItems.status).toBe('MALFORMED_METADATA');

      // 2. Insert invalid empty font_id
      const job2 = await setupTestJob('PENDING');
      await env.DB.prepare('DELETE FROM order_items WHERE order_id = ?')
        .bind(job2.orderId)
        .run();
      await env.DB.prepare(
        'INSERT INTO order_items (id, order_id, font_id, font_name, price, created_at) VALUES (?, ?, "", "Bad Item", 50000, ?)'
      )
        .bind(`item_bad_${Date.now()}`, job2.orderId, Date.now())
        .run();

      const resBadId = await jobService.claimJob(job2.jobId, 'worker-1');
      expect(resBadId.status).toBe('MALFORMED_METADATA');
    });
  });

  describe('Phase 6: Private R2 Artifact Upload & Fenced Completion', () => {
    it('handles PUT /internal/jobs/:job_id/artifact with authentication and lease validation', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);

      // Claim job to obtain valid lease token
      const claimRes = await jobService.claimJob(jobId, 'worker-1', 300);
      expect(claimRes.status).toBe('CLAIMED');
      const leaseToken = claimRes.payload!.lease_token;

      const dummyZip = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x00, 0x00, 0x00, 0x00]);
      // Calculate SHA256 of dummyZip
      const shaBuffer = await crypto.subtle.digest('SHA-256', dummyZip);
      const sha256Hex = Array.from(new Uint8Array(shaBuffer))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('');

      // 1. Upload with wrong lease token -> 409
      const reqBadToken = new Request(`http://example.com/internal/jobs/${jobId}/artifact`, {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'X-Worker-Id': 'worker-1',
          'X-Lease-Token': crypto.randomUUID(),
          'X-Artifact-SHA256': sha256Hex,
          'Content-Type': 'application/zip',
          'Content-Length': dummyZip.byteLength.toString(),
        },
        body: dummyZip,
      });

      const ctx1 = createExecutionContext();
      const resBadToken = await worker.fetch(reqBadToken, testEnv, ctx1);
      await waitOnExecutionContext(ctx1);
      expect(resBadToken.status).toBe(409);

      // 2. Upload with valid lease -> 200
      const reqValid = new Request(`http://example.com/internal/jobs/${jobId}/artifact`, {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'X-Worker-Id': 'worker-1',
          'X-Lease-Token': leaseToken,
          'X-Artifact-SHA256': sha256Hex,
          'Content-Type': 'application/zip',
          'Content-Length': dummyZip.byteLength.toString(),
        },
        body: dummyZip,
      });

      const ctx2 = createExecutionContext();
      const resValid = await worker.fetch(reqValid, testEnv, ctx2);
      await waitOnExecutionContext(ctx2);
      expect(resValid.status).toBe(200);

      const validData = (await resValid.json()) as { success: boolean; artifact_key: string; sha256: string; size: number };
      expect(validData.success).toBe(true);
      expect(validData.sha256).toBe(sha256Hex);
      expect(validData.artifact_key).toBe(`artifacts/${orderId}/${jobId}/${sha256Hex}.zip`);

      // 3. Verify object in R2 bucket
      const r2Obj = await env.ARTIFACTS_BUCKET.head(validData.artifact_key);
      expect(r2Obj).not.toBeNull();
      expect(r2Obj?.size).toBe(dummyZip.byteLength);
      expect(r2Obj?.customMetadata?.job_id).toBe(jobId);
      expect(r2Obj?.customMetadata?.order_id).toBe(orderId);

      // 4. Duplicate matching upload returns 200 idempotently
      const reqDup = new Request(`http://example.com/internal/jobs/${jobId}/artifact`, {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'X-Worker-Id': 'worker-1',
          'X-Lease-Token': leaseToken,
          'X-Artifact-SHA256': sha256Hex,
          'Content-Type': 'application/zip',
          'Content-Length': dummyZip.byteLength.toString(),
        },
        body: dummyZip,
      });

      const ctx3 = createExecutionContext();
      const resDup = await worker.fetch(reqDup, testEnv, ctx3);
      await waitOnExecutionContext(ctx3);
      expect(resDup.status).toBe(200);
    });

    it('handles POST /internal/jobs/:job_id/complete atomically and idempotently', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);

      // Claim job
      const claimRes = await jobService.claimJob(jobId, 'worker-1', 300);
      expect(claimRes.status).toBe('CLAIMED');
      const leaseToken = claimRes.payload!.lease_token;

      // Upload artifact to R2 first
      const dummyZip = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x01, 0x02, 0x03, 0x04]);
      const shaBuffer = await crypto.subtle.digest('SHA-256', dummyZip);
      const sha256Hex = Array.from(new Uint8Array(shaBuffer))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('');
      const expectedKey = `artifacts/${orderId}/${jobId}/${sha256Hex}.zip`;

      await env.ARTIFACTS_BUCKET.put(expectedKey, dummyZip, {
        sha256: sha256Hex,
        customMetadata: {
          job_id: jobId,
          order_id: orderId,
          sha256: sha256Hex,
        },
      });

      // 1. Complete with wrong lease token -> 409
      const reqBadComplete = new Request(`http://example.com/internal/jobs/${jobId}/complete`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: crypto.randomUUID(),
          artifact_key: expectedKey,
          sha256: sha256Hex,
          size: dummyZip.byteLength,
        }),
      });

      const ctx1 = createExecutionContext();
      const resBad = await worker.fetch(reqBadComplete, testEnv, ctx1);
      await waitOnExecutionContext(ctx1);
      expect(resBad.status).toBe(409);

      // 2. Valid complete -> 200
      const reqValidComplete = new Request(`http://example.com/internal/jobs/${jobId}/complete`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: leaseToken,
          artifact_key: expectedKey,
          sha256: sha256Hex,
          size: dummyZip.byteLength,
        }),
      });

      const ctx2 = createExecutionContext();
      const resValid = await worker.fetch(reqValidComplete, testEnv, ctx2);
      await waitOnExecutionContext(ctx2);
      expect(resValid.status).toBe(200);

      const completeData = (await resValid.json()) as { success: boolean; status: string; queue_action: string };
      expect(completeData.success).toBe(true);
      expect(completeData.status).toBe('COMPLETED');
      expect(completeData.queue_action).toBe('ack');

      // 3. Verify D1 state transitions:
      // Receipt created
      const receipt = await env.DB.prepare('SELECT * FROM fulfillment_receipts WHERE job_id = ?')
        .bind(jobId)
        .first<{ order_id: string; artifact_key: string }>();
      expect(receipt).not.toBeNull();
      expect(receipt?.order_id).toBe(orderId);
      expect(receipt?.artifact_key).toBe(expectedKey);

      // Job COMPLETED
      const job = await env.DB.prepare('SELECT * FROM fulfillment_jobs WHERE id = ?')
        .bind(jobId)
        .first<{ status: string; artifact_key: string }>();
      expect(job?.status).toBe('COMPLETED');
      expect(job?.artifact_key).toBe(expectedKey);

      // Order COMPLETED
      const order = await env.DB.prepare('SELECT * FROM orders WHERE id = ?')
        .bind(orderId)
        .first<{ status: string; completed_at: number }>();
      expect(order?.status).toBe('COMPLETED');
      expect(order?.completed_at).toBeGreaterThan(0);

      // Exactly one DELIVERY_READY outbox event created
      const outbox = await env.DB.prepare('SELECT * FROM outbox_events WHERE aggregate_id = ? AND event_type = "DELIVERY_READY"')
        .bind(orderId)
        .all<{ id: string; status: string; payload: string }>();
      expect(outbox.results.length).toBe(1);
      expect(outbox.results[0].status).toBe('PENDING');

      // 4. Duplicate completion replay returns 200 with queue_action: ack (no duplicate outbox event)
      const reqReplay = new Request(`http://example.com/internal/jobs/${jobId}/complete`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: leaseToken,
          artifact_key: expectedKey,
          sha256: sha256Hex,
          size: dummyZip.byteLength,
        }),
      });

      const ctx3 = createExecutionContext();
      const resReplay = await worker.fetch(reqReplay, testEnv, ctx3);
      await waitOnExecutionContext(ctx3);
      expect(resReplay.status).toBe(200);

      const outboxAfterReplay = await env.DB.prepare('SELECT * FROM outbox_events WHERE aggregate_id = ? AND event_type = "DELIVERY_READY"')
        .bind(orderId)
        .all();
      expect(outboxAfterReplay.results.length).toBe(1);
    });

    it('rejects PUT /internal/jobs/:job_id/artifact duplicate upload when existing metadata or size mismatches', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);
      const claimRes = await jobService.claimJob(jobId, 'worker-1', 300);
      const leaseToken = claimRes.payload!.lease_token;

      const dummyZip = new Uint8Array([0x50, 0x4b, 0x03, 0x04]);
      const sha256Hex = '11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff';
      const key = `artifacts/${orderId}/${jobId}/${sha256Hex}.zip`;

      // Pre-seed an existing object with different size/metadata
      await env.ARTIFACTS_BUCKET.put(key, new Uint8Array([0x01, 0x02]), {
        customMetadata: { job_id: 'other-job', order_id: orderId, sha256: sha256Hex },
      });

      const req = new Request(`http://example.com/internal/jobs/${jobId}/artifact`, {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'X-Worker-Id': 'worker-1',
          'X-Lease-Token': leaseToken,
          'X-Artifact-SHA256': sha256Hex,
          'Content-Type': 'application/zip',
          'Content-Length': dummyZip.byteLength.toString(),
        },
        body: dummyZip,
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(409);
      const data = (await res.json()) as { error: string; queue_action: string };
      expect(data.error).toContain('mismatch');
    });

    it('rejects PUT /internal/jobs/:job_id/artifact when post-put R2 checksum is missing or mismatched', async () => {
      const { jobId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);
      const claimRes = await jobService.claimJob(jobId, 'worker-1', 300);
      const leaseToken = claimRes.payload!.lease_token;

      const dummyZip = new Uint8Array([0x50, 0x4b, 0x03, 0x04]);
      const shaBuffer = await crypto.subtle.digest('SHA-256', dummyZip);
      const sha256Hex = Array.from(new Uint8Array(shaBuffer))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('');

      // 1. Mock bucket where put returns an object with MISSING checksums.sha256
      const mockBucketMissing = {
        ...env.ARTIFACTS_BUCKET,
        head: async () => null,
        put: async (key: string) => ({
          key,
          size: dummyZip.byteLength,
          etag: 'dummy_etag',
          version: 'v1',
          checksums: {}, // missing sha256
        } as unknown as R2Object),
      };

      const mockEnvMissing: Env = {
        ...testEnv,
        ARTIFACTS_BUCKET: mockBucketMissing as unknown as R2Bucket,
      };

      const reqMissing = new Request(`http://example.com/internal/jobs/${jobId}/artifact`, {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'X-Worker-Id': 'worker-1',
          'X-Lease-Token': leaseToken,
          'X-Artifact-SHA256': sha256Hex,
          'Content-Type': 'application/zip',
          'Content-Length': dummyZip.byteLength.toString(),
        },
        body: dummyZip,
      });

      const ctx1 = createExecutionContext();
      const resMissing = await worker.fetch(reqMissing, mockEnvMissing, ctx1);
      await waitOnExecutionContext(ctx1);

      expect(resMissing.status).toBe(500);
      const dataMissing = (await resMissing.json()) as { error: string };
      expect(dataMissing.error).toContain('checksum missing or mismatch');

      // 2. Mock bucket where put returns an object with MISMATCHED checksums.sha256
      const mockBucketMismatch = {
        ...env.ARTIFACTS_BUCKET,
        head: async () => null,
        put: async (key: string) => ({
          key,
          size: dummyZip.byteLength,
          etag: 'dummy_etag',
          version: 'v1',
          checksums: {
            sha256: 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
          },
        } as unknown as R2Object),
      };

      const mockEnvMismatch: Env = {
        ...testEnv,
        ARTIFACTS_BUCKET: mockBucketMismatch as unknown as R2Bucket,
      };

      const reqMismatch = new Request(`http://example.com/internal/jobs/${jobId}/artifact`, {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'X-Worker-Id': 'worker-1',
          'X-Lease-Token': leaseToken,
          'X-Artifact-SHA256': sha256Hex,
          'Content-Type': 'application/zip',
          'Content-Length': dummyZip.byteLength.toString(),
        },
        body: dummyZip,
      });

      const ctx2 = createExecutionContext();
      const resMismatch = await worker.fetch(reqMismatch, mockEnvMismatch, ctx2);
      await waitOnExecutionContext(ctx2);

      expect(resMismatch.status).toBe(500);
      const dataMismatch = (await resMismatch.json()) as { error: string };
      expect(dataMismatch.error).toContain('checksum missing or mismatch');
    });

    it('rejects POST /internal/jobs/:job_id/complete when R2 metadata or checksum mismatches', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);
      const claimRes = await jobService.claimJob(jobId, 'worker-1', 300);
      const leaseToken = claimRes.payload!.lease_token;

      const sha256Hex = 'aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899';
      const key = `artifacts/${orderId}/${jobId}/${sha256Hex}.zip`;

      // Seed R2 object with mismatched size
      await env.ARTIFACTS_BUCKET.put(key, new Uint8Array([0x50, 0x4b]), {
        customMetadata: { job_id: jobId, order_id: orderId, sha256: sha256Hex },
      });

      const req = new Request(`http://example.com/internal/jobs/${jobId}/complete`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: leaseToken,
          artifact_key: key,
          sha256: sha256Hex,
          size: 1024, // Claims size is 1024, but R2 object is 2 bytes
        }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(400);
      const data = (await res.json()) as { error: string };
      expect(data.error).toContain('size mismatch');
    });

    it('rejects POST /internal/jobs/:job_id/complete when lease is fenced and rolls back D1 without side effects', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);
      const claimRes = await jobService.claimJob(jobId, 'worker-1', 300);
      const leaseToken = claimRes.payload!.lease_token;

      const dummyZip = new Uint8Array([0x50, 0x4b, 0x03, 0x04]);
      const shaBuffer = await crypto.subtle.digest('SHA-256', dummyZip);
      const sha256Hex = Array.from(new Uint8Array(shaBuffer))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('');
      const key = `artifacts/${orderId}/${jobId}/${sha256Hex}.zip`;

      await env.ARTIFACTS_BUCKET.put(key, dummyZip, {
        sha256: shaBuffer,
        customMetadata: { job_id: jobId, order_id: orderId, sha256: sha256Hex },
      });

      // Steal or expire the lease by setting lease_expires_at to the past
      await env.DB.prepare('UPDATE fulfillment_jobs SET lease_expires_at = ? WHERE id = ?')
        .bind(Date.now() - 10000, jobId)
        .run();

      const req = new Request(`http://example.com/internal/jobs/${jobId}/complete`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: leaseToken,
          artifact_key: key,
          sha256: sha256Hex,
          size: dummyZip.byteLength,
        }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(409);

      // Verify ZERO mutations occurred in D1:
      const receipt = await env.DB.prepare('SELECT * FROM fulfillment_receipts WHERE job_id = ?')
        .bind(jobId)
        .first();
      expect(receipt).toBeNull();

      const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
        .bind(orderId)
        .first<{ status: string }>();
      expect(order?.status).toBe('PROCESSING');

      const outbox = await env.DB.prepare('SELECT * FROM outbox_events WHERE aggregate_id = ? AND event_type = "DELIVERY_READY"')
        .bind(orderId)
        .all();
      expect(outbox.results.length).toBe(0);
    });

    it('rejects POST /internal/jobs/:job_id/complete with conflict when already completed with different artifact', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);
      const claimRes = await jobService.claimJob(jobId, 'worker-1', 300);
      const leaseToken = claimRes.payload!.lease_token;

      const dummyZip1 = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x01]);
      const shaBuffer1 = await crypto.subtle.digest('SHA-256', dummyZip1);
      const sha1 = Array.from(new Uint8Array(shaBuffer1))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('');
      const key1 = `artifacts/${orderId}/${jobId}/${sha1}.zip`;

      await env.ARTIFACTS_BUCKET.put(key1, dummyZip1, {
        sha256: shaBuffer1,
        customMetadata: { job_id: jobId, order_id: orderId, sha256: sha1 },
      });

      // Complete first time
      const completeRes = await jobService.completeJob({
        jobId,
        workerId: 'worker-1',
        leaseToken,
        artifactKey: key1,
        artifactSha256: sha1,
        artifactSizeBytes: dummyZip1.byteLength,
      });
      expect(completeRes.status).toBe('COMPLETED');

      // Attempt second completion with different artifact
      const dummyZip2 = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x02]);
      const shaBuffer2 = await crypto.subtle.digest('SHA-256', dummyZip2);
      const sha2 = Array.from(new Uint8Array(shaBuffer2))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('');
      const key2 = `artifacts/${orderId}/${jobId}/${sha2}.zip`;

      await env.ARTIFACTS_BUCKET.put(key2, dummyZip2, {
        sha256: shaBuffer2,
        customMetadata: { job_id: jobId, order_id: orderId, sha256: sha2 },
      });

      const reqDiff = new Request(`http://example.com/internal/jobs/${jobId}/complete`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: leaseToken,
          artifact_key: key2,
          sha256: sha2,
          size: dummyZip2.byteLength,
        }),
      });

      const ctx = createExecutionContext();
      const resDiff = await worker.fetch(reqDiff, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(resDiff.status).toBe(409);
      const data = (await resDiff.json()) as { error: string; queue_action: string };
      expect(data.queue_action).toBe('ack');
      expect(data.error).toContain('different artifact');
    });

    it('rejects POST /internal/jobs/:job_id/complete when stored SHA-256 checksum is missing in R2', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);
      const claimRes = await jobService.claimJob(jobId, 'worker-1', 300);
      const leaseToken = claimRes.payload!.lease_token;

      const dummyZip = new Uint8Array([0x50, 0x4b, 0x03, 0x04]);
      const shaBuffer = await crypto.subtle.digest('SHA-256', dummyZip);
      const sha256Hex = Array.from(new Uint8Array(shaBuffer))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('');
      const key = `artifacts/${orderId}/${jobId}/${sha256Hex}.zip`;

      // Object in R2 without sha256 checksum in put options
      await env.ARTIFACTS_BUCKET.put(key, dummyZip, {
        customMetadata: { job_id: jobId, order_id: orderId, sha256: sha256Hex },
      });

      const req = new Request(`http://example.com/internal/jobs/${jobId}/complete`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: leaseToken,
          artifact_key: key,
          sha256: sha256Hex,
          size: dummyZip.byteLength,
        }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      // Must reject when stored checksum is missing
      expect(res.status).toBe(400);
      const data = (await res.json()) as { error: string };
      expect(data.error).toContain('stored checksum');
    });

    it('rejects POST /internal/jobs/:job_id/complete when stored SHA-256 checksum mismatches in R2', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);
      const claimRes = await jobService.claimJob(jobId, 'worker-1', 300);
      const leaseToken = claimRes.payload!.lease_token;

      const dummyZip = new Uint8Array([0x50, 0x4b, 0x03, 0x04]);
      const shaBuffer = await crypto.subtle.digest('SHA-256', dummyZip);
      const shaReal = Array.from(new Uint8Array(shaBuffer))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('');
      const shaClaimed = '9999999999999999999999999999999999999999999999999999999999999999';
      const keyClaimed = `artifacts/${orderId}/${jobId}/${shaClaimed}.zip`;

      // Upload with real shaBuffer under keyClaimed, but matching metadata shaClaimed
      await env.ARTIFACTS_BUCKET.put(keyClaimed, dummyZip, {
        sha256: shaBuffer,
        customMetadata: { job_id: jobId, order_id: orderId, sha256: shaClaimed },
      });

      const req = new Request(`http://example.com/internal/jobs/${jobId}/complete`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${NODE_SECRET}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          worker_id: 'worker-1',
          lease_token: leaseToken,
          artifact_key: keyClaimed,
          sha256: shaClaimed,
          size: dummyZip.byteLength,
        }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(400);
      const data = (await res.json()) as { error: string };
      expect(data.error).toContain('stored checksum');
    });

    it('rolls back entire D1 batch (receipt, job, order, outbox) when mid-batch database error is injected', async () => {
      const { jobId, orderId } = await setupTestJob('PENDING');
      const jobService = new JobService(env.DB);
      const claimRes = await jobService.claimJob(jobId, 'worker-1', 300);
      const leaseToken = claimRes.payload!.lease_token;

      const dummyZip = new Uint8Array([0x50, 0x4b, 0x03, 0x04]);
      const shaBuffer = await crypto.subtle.digest('SHA-256', dummyZip);
      const shaHex = Array.from(new Uint8Array(shaBuffer))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('');
      const key = `artifacts/${orderId}/${jobId}/${shaHex}.zip`;

      await env.ARTIFACTS_BUCKET.put(key, dummyZip, {
        sha256: shaBuffer,
        customMetadata: { job_id: jobId, order_id: orderId, sha256: shaHex },
      });

      // Inject a mid-batch failure via SQLite trigger on orders UPDATE (statement 3 in the batch)
      await env.DB.prepare(
        `CREATE TRIGGER test_fail_orders_update BEFORE UPDATE ON orders BEGIN SELECT RAISE(FAIL, 'INJECTED_MID_BATCH_TRANSACTION_FAILURE'); END;`
      ).run();

      try {
        const completeRes = await jobService.completeJob({
          jobId,
          workerId: 'worker-1',
          leaseToken,
          artifactKey: key,
          artifactSha256: shaHex,
          artifactSizeBytes: dummyZip.byteLength,
        });

        expect(completeRes.status).toBe('ERROR');

        // Prove that Statement 1 (fulfillment_receipts INSERT) was rolled back:
        const receipt = await env.DB.prepare('SELECT * FROM fulfillment_receipts WHERE job_id = ?')
          .bind(jobId)
          .first();
        expect(receipt).toBeNull();

        // Prove that Statement 2 (fulfillment_jobs UPDATE) was rolled back:
        const job = await env.DB.prepare('SELECT status FROM fulfillment_jobs WHERE id = ?')
          .bind(jobId)
          .first<{ status: string }>();
        expect(job?.status).toBe('PROCESSING');

        // Prove that Statement 3 (orders UPDATE) was rolled back:
        const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
          .bind(orderId)
          .first<{ status: string }>();
        expect(order?.status).toBe('PROCESSING');

        // Prove that Statement 4 (outbox_events INSERT) was rolled back:
        const outbox = await env.DB.prepare('SELECT * FROM outbox_events WHERE aggregate_id = ? AND event_type = "DELIVERY_READY"')
          .bind(orderId)
          .all();
        expect(outbox.results.length).toBe(0);
      } finally {
        await env.DB.prepare('DROP TRIGGER IF EXISTS test_fail_orders_update;').run();
      }
    });
  });
});


/**
 * T-FAST30-A23-FIX F4: config-driven job-age backstop.
 *
 * (a) heartbeat() refuses lease extensions once now - leased_at >
 *     MAX_JOB_AGE_MS and returns the fenced/expired result;
 * (b) finalizeExpiredExhaustedJobs() gains an age-based candidate clause
 *     (leased_at <= now - MAX_JOB_AGE_MS AND attempt_count >= max_attempts)
 *     with CAS consistent with the existing fencing.
 *
 * Existing fencing/CAS is never weakened: these tests assert both the new
 * backstop and the unchanged fresh-lease behavior.
 */
import { describe, it, expect } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker, { type Env } from '../src/index';
import {
  JobService,
  DEFAULT_MAX_JOB_AGE_MS,
  resolveMaxJobAgeMs,
} from '../src/services/job-service';

const NODE_SECRET = 'a23_super_secret_node_key_999';

const testEnv: Env = {
  ...(env as unknown as Env),
  A23_NODE_SECRET: NODE_SECRET,
  A23_JOB_LEASE_SECONDS: '300',
};

async function setupTestJob(
  status = 'PENDING',
  orderStatus = 'PAID',
  attemptCount = 0
) {
  const orderId = `ord_${crypto.randomUUID().replace(/-/g, '')}`;
  const jobId = `job_${crypto.randomUUID().replace(/-/g, '')}`;
  const paymentCode = `TF${crypto.randomUUID().replace(/[^a-zA-Z0-9]/g, '').slice(0, 6).toUpperCase()}`;
  const now = Date.now();

  const metadata = JSON.stringify({
    source_url: 'https://www.myfonts.com/collections/roboto-flex',
    family_name: 'Roboto Flex',
    foundry: 'Google Fonts',
    selected_formats: ['TTF'],
    mode: 'ORIGINAL',
  });

  await env.DB.prepare(
    `INSERT INTO orders (id, user_id, status, total_amount, currency, metadata, payment_code, mode, created_at, updated_at)
     VALUES (?, 12345, ?, 100000, 'VND', ?, ?, 'ORIGINAL', ?, ?)`
  )
    .bind(orderId, orderStatus, metadata, paymentCode, now, now)
    .run();

  await env.DB.prepare(
    `INSERT INTO order_items (id, order_id, font_id, font_name, price, created_at)
     VALUES (?, ?, 'rf_regular', 'Roboto Flex Regular', 50000, ?)`
  )
    .bind(`item_1_${crypto.randomUUID()}`, orderId, now)
    .run();

  await env.DB.prepare(
    `INSERT INTO fulfillment_jobs (id, order_id, status, attempt_count, max_attempts, created_at, updated_at)
     VALUES (?, ?, ?, ?, 3, ?, ?)`
  )
    .bind(jobId, orderId, status, attemptCount, now, now)
    .run();

  return { orderId, jobId };
}

describe('resolveMaxJobAgeMs config resolution', () => {
  it('uses the documented 35-minute default and bounds invalid values', () => {
    expect(DEFAULT_MAX_JOB_AGE_MS).toBe(2_100_000);
    expect(resolveMaxJobAgeMs(undefined)).toBe(DEFAULT_MAX_JOB_AGE_MS);
    expect(resolveMaxJobAgeMs('')).toBe(DEFAULT_MAX_JOB_AGE_MS);
    expect(resolveMaxJobAgeMs('not-a-number')).toBe(DEFAULT_MAX_JOB_AGE_MS);
    expect(resolveMaxJobAgeMs('5')).toBe(DEFAULT_MAX_JOB_AGE_MS); // below 60s floor
    expect(resolveMaxJobAgeMs('2100000')).toBe(2_100_000);
    expect(resolveMaxJobAgeMs('120000')).toBe(120_000);
  });
});

describe('F4 heartbeat age backstop', () => {
  it('still extends a fresh lease (unchanged behavior under the cap)', async () => {
    const { jobId } = await setupTestJob();
    const jobService = new JobService(env.DB, 60_000);

    const claim = await jobService.claimJob(jobId, 'worker-1', 120);
    expect(claim.status).toBe('CLAIMED');
    const token = claim.payload!.lease_token;

    const hb = await jobService.heartbeat(jobId, 'worker-1', token, 120);
    expect(hb.status).toBe('EXTENDED');
    expect(hb.lease_expires_at).toBeGreaterThan(Date.now());
  });

  it('refuses extension and returns fenced/expired once the lease age exceeds the cap', async () => {
    const { jobId } = await setupTestJob();
    const jobService = new JobService(env.DB, 60_000);

    const claim = await jobService.claimJob(jobId, 'worker-1', 120);
    expect(claim.status).toBe('CLAIMED');
    const token = claim.payload!.lease_token;

    // Age the current lease beyond the cap without touching lease_expires_at.
    const agedLeasedAt = Date.now() - 120_000;
    await env.DB.prepare('UPDATE fulfillment_jobs SET leased_at = ? WHERE id = ?')
      .bind(agedLeasedAt, jobId)
      .run();
    const before = await env.DB.prepare(
      'SELECT lease_expires_at FROM fulfillment_jobs WHERE id = ?'
    )
      .bind(jobId)
      .first<{ lease_expires_at: number }>();

    const hb = await jobService.heartbeat(jobId, 'worker-1', token, 120);
    expect(hb.status).toBe('EXPIRED_OR_FENCED');
    expect(hb.queue_action).toBe('ack');
    expect(hb.lease_expires_at).toBeUndefined();

    // The lease was NOT extended.
    const after = await env.DB.prepare(
      'SELECT lease_expires_at, status FROM fulfillment_jobs WHERE id = ?'
    )
      .bind(jobId)
      .first<{ lease_expires_at: number; status: string }>();
    expect(after?.lease_expires_at).toBe(before?.lease_expires_at);
    expect(after?.status).toBe('PROCESSING');
  });

  it('a fresh re-claim resets the lease age and heartbeats work again', async () => {
    const { jobId } = await setupTestJob();
    const jobService = new JobService(env.DB, 60_000);

    const claimA = await jobService.claimJob(jobId, 'worker-1', 120);
    expect(claimA.status).toBe('CLAIMED');

    // Expire the lease, then re-claim (claim path resets leased_at).
    await env.DB.prepare('UPDATE fulfillment_jobs SET lease_expires_at = ? WHERE id = ?')
      .bind(Date.now() - 1000, jobId)
      .run();
    const claimB = await jobService.claimJob(jobId, 'worker-2', 120);
    expect(claimB.status).toBe('CLAIMED');
    const tokenB = claimB.payload!.lease_token;

    const hb = await jobService.heartbeat(jobId, 'worker-2', tokenB, 120);
    expect(hb.status).toBe('EXTENDED');
  });

  it('refuses aged heartbeats through the HTTP route with MAX_JOB_AGE_MS wiring', async () => {
    const { jobId } = await setupTestJob();
    const ageEnv: Env = { ...testEnv, MAX_JOB_AGE_MS: '60000' };

    const claimReq = new Request(`http://example.com/internal/jobs/${jobId}/claim`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${NODE_SECRET}`,
      },
      body: JSON.stringify({ worker_id: 'worker-1' }),
    });
    const ctxClaim = createExecutionContext();
    const claimRes = await worker.fetch(claimReq, ageEnv, ctxClaim);
    await waitOnExecutionContext(ctxClaim);
    expect(claimRes.status).toBe(200);
    const claimData = (await claimRes.json()) as any;
    const token = claimData.lease_token as string;
    expect(token).toBeTruthy();

    await env.DB.prepare('UPDATE fulfillment_jobs SET leased_at = ? WHERE id = ?')
      .bind(Date.now() - 120_000, jobId)
      .run();

    const hbReq = new Request(`http://example.com/internal/jobs/${jobId}/heartbeat`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${NODE_SECRET}`,
      },
      body: JSON.stringify({ worker_id: 'worker-1', lease_token: token }),
    });
    const ctx = createExecutionContext();
    const res = await worker.fetch(hbReq, ageEnv, ctx);
    await waitOnExecutionContext(ctx);
    expect(res.status).toBe(409);
    const data = (await res.json()) as any;
    expect(data.queue_action).toBe('ack');
  });
});

describe('F4 finalizer age backstop clause', () => {
  it('finalizes an exhausted job whose lease is still alive but aged beyond the cap', async () => {
    const { jobId, orderId } = await setupTestJob('PROCESSING', 'PROCESSING', 3);

    // Live lease (far future) but the claim-time leased_at is aged out.
    const now = Date.now();
    await env.DB.prepare(
      'UPDATE fulfillment_jobs SET lease_owner = ?, lease_token = ?, leased_at = ?, lease_expires_at = ? WHERE id = ?'
    )
      .bind('zombie-worker', '12345678-1234-1234-1234-123456789abc', now - 3_600_000, now + 300_000, jobId)
      .run();

    const jobService = new JobService(env.DB, 2_100_000);
    const result = await jobService.finalizeExpiredExhaustedJobs(now);
    expect(result.finalizedJobs).toBe(1);
    expect(result.transitionedOrders).toBe(1);

    const job = await env.DB.prepare('SELECT status, last_error, lease_token FROM fulfillment_jobs WHERE id = ?')
      .bind(jobId)
      .first<{ status: string; last_error: string; lease_token: string | null }>();
    expect(job?.status).toBe('FAILED');
    expect(job?.last_error).toBe('max_attempts_exhausted');
    expect(job?.lease_token).toBeNull();

    const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
      .bind(orderId)
      .first<{ status: string }>();
    expect(order?.status).toBe('FAILED');
  });

  it('does NOT finalize via the age clause while attempts remain (backstop never weakens retry)', async () => {
    const { jobId, orderId } = await setupTestJob('PROCESSING', 'PROCESSING', 1);

    const now = Date.now();
    await env.DB.prepare(
      'UPDATE fulfillment_jobs SET lease_owner = ?, lease_token = ?, leased_at = ?, lease_expires_at = ? WHERE id = ?'
    )
      .bind('zombie-worker', '12345678-1234-1234-1234-123456789abc', now - 3_600_000, now + 300_000, jobId)
      .run();

    const jobService = new JobService(env.DB, 2_100_000);
    const result = await jobService.finalizeExpiredExhaustedJobs(now);
    expect(result.finalizedJobs).toBe(0);
    expect(result.transitionedOrders).toBe(0);

    const job = await env.DB.prepare('SELECT status FROM fulfillment_jobs WHERE id = ?')
      .bind(jobId)
      .first<{ status: string }>();
    expect(job?.status).toBe('PROCESSING');
    const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
      .bind(orderId)
      .first<{ status: string }>();
    expect(order?.status).toBe('PROCESSING');
  });

  it('does NOT finalize a fresh exhausted job under a live lease (age cap not reached)', async () => {
    const { jobId } = await setupTestJob('PROCESSING', 'PROCESSING', 3);

    const now = Date.now();
    await env.DB.prepare(
      'UPDATE fulfillment_jobs SET lease_owner = ?, lease_token = ?, leased_at = ?, lease_expires_at = ? WHERE id = ?'
    )
      .bind('live-worker', '12345678-1234-1234-1234-123456789abc', now - 10_000, now + 300_000, jobId)
      .run();

    const jobService = new JobService(env.DB, 2_100_000);
    const result = await jobService.finalizeExpiredExhaustedJobs(now);
    expect(result.finalizedJobs).toBe(0);

    const job = await env.DB.prepare('SELECT status FROM fulfillment_jobs WHERE id = ?')
      .bind(jobId)
      .first<{ status: string }>();
    expect(job?.status).toBe('PROCESSING');
  });

  it('still finalizes the unchanged lease-expiry clause (regression)', async () => {
    const { jobId, orderId } = await setupTestJob('PROCESSING', 'PROCESSING', 3);

    const now = Date.now();
    await env.DB.prepare(
      'UPDATE fulfillment_jobs SET lease_owner = ?, lease_token = ?, leased_at = ?, lease_expires_at = ? WHERE id = ?'
    )
      .bind('crashed-worker', '12345678-1234-1234-1234-123456789abc', now - 60_000, now - 10_000, jobId)
      .run();

    const jobService = new JobService(env.DB, 2_100_000);
    const result = await jobService.finalizeExpiredExhaustedJobs(now);
    expect(result.finalizedJobs).toBe(1);
    expect(result.transitionedOrders).toBe(1);

    const job = await env.DB.prepare('SELECT status, last_error FROM fulfillment_jobs WHERE id = ?')
      .bind(jobId)
      .first<{ status: string; last_error: string }>();
    expect(job?.status).toBe('FAILED');
    expect(job?.last_error).toBe('max_attempts_exhausted');
  });
});

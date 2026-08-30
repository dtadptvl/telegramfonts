/**
 * Focused spec for expired-lease recovery reaper (incident 2026-08-30).
 *
 * Requirements:
 * (a) expired lease + attempts remaining -> RETRY + queue send with correct payload;
 * (b) exhausted expired job -> NOT recovered (finalizer's job);
 * (c) valid lease -> untouched;
 * (d) CAS safety: concurrent lease renewal (changed lease_expires_at) -> no-op;
 * (e) order stays PROCESSING during recovery.
 */
import { describe, it, expect } from 'vitest';
import { env } from 'cloudflare:test';
import { JobService } from '../src/services/job-service';

function unique(prefix: string): string {
  return `${prefix}_${crypto.randomUUID().replace(/-/g, '')}`;
}

async function setupTestJob(
  status = 'PROCESSING',
  orderStatus = 'PROCESSING',
  attemptCount = 1,
  maxAttempts = 3,
  leaseExpiresAt: number | null = Date.now() - 60_000
) {
  const orderId = unique('ord');
  const jobId = unique('job');
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
    `INSERT INTO fulfillment_jobs (
       id, order_id, status, leased_at, lease_expires_at, lease_owner, lease_token,
       attempt_count, max_attempts, next_retry_at, last_error, created_at, updated_at
     ) VALUES (?, ?, ?, ?, ?, 'worker-1', '00000000-0000-0000-0000-000000000001', ?, ?, NULL, NULL, ?, ?)`
  )
    .bind(jobId, orderId, status, now - 120_000, leaseExpiresAt, attemptCount, maxAttempts, now, now)
    .run();

  return { orderId, jobId, leaseExpiresAt };
}

describe('Expired lease recovery reaper', () => {
  it('(a) expired lease + attempts remaining -> RETRY + queue send with correct payload', async () => {
    const { jobId, orderId } = await setupTestJob('PROCESSING', 'PROCESSING', 1, 3);
    const sentMessages: unknown[] = [];
    const mockQueue = {
      send: async (msg: unknown) => {
        sentMessages.push(msg);
      },
      sendBatch: async () => {},
    } as unknown as Queue<unknown>;

    const service = new JobService(env.DB, 2_100_000, mockQueue);
    const now = Date.now();
    const result = await service.recoverExpiredLeaseJobs(now);

    expect(result).toEqual({ recoveredJobs: 1, requeued: 1 });
    expect(sentMessages).toEqual([{ job_id: jobId }]);

    const job = await env.DB.prepare(
      'SELECT status, lease_owner, lease_token, leased_at, lease_expires_at, next_retry_at, last_error, attempt_count, max_attempts FROM fulfillment_jobs WHERE id = ?'
    )
      .bind(jobId)
      .first<Record<string, unknown>>();

    expect(job).toMatchObject({
      status: 'RETRY',
      lease_owner: null,
      lease_token: null,
      leased_at: null,
      lease_expires_at: null,
      next_retry_at: now,
      last_error: 'lease_expired_recovery',
      attempt_count: 1,
      max_attempts: 3,
    });
  });

  it('(b) exhausted expired job -> NOT recovered (handled by finalizeExpiredExhaustedJobs)', async () => {
    const { jobId } = await setupTestJob('PROCESSING', 'PROCESSING', 3, 3);
    const sentMessages: unknown[] = [];
    const mockQueue = {
      send: async (msg: unknown) => {
        sentMessages.push(msg);
      },
      sendBatch: async () => {},
    } as unknown as Queue<unknown>;

    const service = new JobService(env.DB, 2_100_000, mockQueue);
    const result = await service.recoverExpiredLeaseJobs(Date.now());

    expect(result).toEqual({ recoveredJobs: 0, requeued: 0 });
    expect(sentMessages).toEqual([]);

    const job = await env.DB.prepare('SELECT status, attempt_count, max_attempts FROM fulfillment_jobs WHERE id = ?')
      .bind(jobId)
      .first<Record<string, unknown>>();
    expect(job?.status).toBe('PROCESSING');
  });

  it('(c) valid lease -> untouched', async () => {
    const futureExpiry = Date.now() + 180_000;
    const { jobId } = await setupTestJob('PROCESSING', 'PROCESSING', 1, 3, futureExpiry);
    const sentMessages: unknown[] = [];
    const mockQueue = {
      send: async (msg: unknown) => {
        sentMessages.push(msg);
      },
      sendBatch: async () => {},
    } as unknown as Queue<unknown>;

    const service = new JobService(env.DB, 2_100_000, mockQueue);
    const result = await service.recoverExpiredLeaseJobs(Date.now());

    expect(result).toEqual({ recoveredJobs: 0, requeued: 0 });
    expect(sentMessages).toEqual([]);

    const job = await env.DB.prepare('SELECT status, lease_expires_at FROM fulfillment_jobs WHERE id = ?')
      .bind(jobId)
      .first<Record<string, unknown>>();
    expect(job?.status).toBe('PROCESSING');
    expect(job?.lease_expires_at).toBe(futureExpiry);
  });

  it('(d) CAS safety: concurrent lease renewal (changed lease_expires_at) -> no-op', async () => {
    const expiredTimestamp = Date.now() - 60_000;
    const { jobId } = await setupTestJob('PROCESSING', 'PROCESSING', 1, 3, expiredTimestamp);

    // Simulate concurrent heartbeat extending lease right before CAS runs
    await env.DB.prepare('UPDATE fulfillment_jobs SET lease_expires_at = ? WHERE id = ?')
      .bind(Date.now() + 300_000, jobId)
      .run();

    const sentMessages: unknown[] = [];
    const mockQueue = {
      send: async (msg: unknown) => {
        sentMessages.push(msg);
      },
      sendBatch: async () => {},
    } as unknown as Queue<unknown>;

    const service = new JobService(env.DB, 2_100_000, mockQueue);
    const result = await service.recoverExpiredLeaseJobs(Date.now());

    expect(result).toEqual({ recoveredJobs: 0, requeued: 0 });
    expect(sentMessages).toEqual([]);

    const job = await env.DB.prepare('SELECT status FROM fulfillment_jobs WHERE id = ?')
      .bind(jobId)
      .first<Record<string, unknown>>();
    expect(job?.status).toBe('PROCESSING');
  });

  it('(e) order stays PROCESSING during recovery', async () => {
    const { jobId, orderId } = await setupTestJob('PROCESSING', 'PROCESSING', 1, 3);
    const service = new JobService(env.DB);
    const result = await service.recoverExpiredLeaseJobs(Date.now());

    expect(result.recoveredJobs).toBe(1);

    const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
      .bind(orderId)
      .first<Record<string, unknown>>();
    expect(order?.status).toBe('PROCESSING');
  });
});

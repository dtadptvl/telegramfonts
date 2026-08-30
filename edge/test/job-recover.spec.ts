import { describe, expect, it } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker, { type Env } from '../src/index';
import {
  JobService,
  MAX_ATTEMPTS_EXHAUSTED_REASON,
  MAX_ATTEMPTS_EXHAUSTED_RESCUE_REASON,
  TERMINAL_FAILED_RECOVERY_REASON,
} from '../src/services/job-service';
import { OutboxService } from '../src/services/outbox-service';

const NODE_SECRET = 'a23_recover_test_secret_901';
const testEnv: Env = {
  ...(env as unknown as Env),
  A23_NODE_SECRET: NODE_SECRET,
  A23_JOB_LEASE_SECONDS: '300',
};

type FixtureOptions = {
  jobStatus?: 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED';
  orderStatus?: 'PAID' | 'PROCESSING' | 'COMPLETED' | 'FAILED';
  retainLease?: boolean;
  leaseResidue?: 'expired' | 'live';
  attemptCount?: number;
  maxAttempts?: number;
  lastError?: string | null;
  outboxStatus?: 'SENT' | 'PENDING';
  includeOutbox?: boolean;
  includeSecondOutbox?: boolean;
  payloadJobId?: string;
  includeReceipt?: boolean;
  includeArtifact?: boolean;
  paymentAmount?: number;
  paymentCurrency?: string;
};

type Fixture = {
  jobId: string;
  orderId: string;
  outboxId: string;
  paymentId: string;
  paymentTransactionId: string;
  paymentCode: string;
  leaseOwner: string;
  leaseToken: string;
  expected: Record<string, unknown>;
};

function unique(prefix: string): string {
  return `${prefix}_${crypto.randomUUID().replace(/-/g, '')}`;
}

// Default fixture shape reproduces the failJob-terminal lineage exactly as
// observed in production: job FAILED with attempt_count == max_attempts,
// every lease field cleared to NULL, order FAILED, outbox JOB_READY SENT,
// exactly one SEPAY VERIFIED payment. retainLease builds the cron
// finalization-path shape (lease retained) that belongs to rescue instead.
// leaseResidue builds the reaper-terminal shape (issue 90 attempt 5): lease_token
// cleared by the cron fencing write with owner/leased_at/expires residue left behind.
async function createFixture(options: FixtureOptions = {}): Promise<Fixture> {
  const now = Date.now();
  const jobId = unique('job');
  const orderId = unique('ord');
  const outboxId = unique('outbox');
  const paymentId = unique('payment');
  const paymentTransactionId = unique('txn');
  const paymentCode = unique('TF');
  const leaseOwner = 'a23-worker-1';
  const leaseToken = '00000000-0000-0000-0000-000000000002';
  const attemptCount = options.attemptCount ?? 3;
  const maxAttempts = options.maxAttempts ?? 3;
  const paymentAmount = options.paymentAmount ?? 100000;
  const paymentCurrency = options.paymentCurrency ?? 'VND';
  const jobStatus = options.jobStatus ?? 'FAILED';
  const orderStatus = options.orderStatus ?? 'FAILED';
  const retainLease = options.retainLease ?? false;
  const leaseResidue = options.leaseResidue ?? 'none';
  const leasedAt = retainLease ? now - 120_000 : leaseResidue === 'none' ? null : now - 420_000;
  const leaseExpiresAt = retainLease
    ? now - 60_000
    : leaseResidue === 'expired'
      ? now - 60_000
      : leaseResidue === 'live'
        ? now + 240_000
        : null;
  const leaseOwnerValue = retainLease ? leaseOwner : leaseResidue === 'none' ? null : leaseOwner;
  const leaseTokenValue = retainLease ? leaseToken : null;
  const lastError = options.lastError === undefined
    ? (jobStatus === 'FAILED' ? MAX_ATTEMPTS_EXHAUSTED_REASON : null)
    : options.lastError;
  const completedShape = jobStatus === 'COMPLETED';
  const artifactKey = completedShape ? `artifacts/${orderId}/${jobId}/done.zip` : null;
  const artifactSha = completedShape ? 'c'.repeat(64) : null;
  const metadata = JSON.stringify({
    source_url: 'https://www.myfonts.com/collections/roboto-flex',
    family_name: 'Roboto Flex',
    selected_formats: ['TTF'],
    mode: 'ORIGINAL',
  });

  await env.DB.prepare(
    `INSERT INTO orders (id, user_id, status, total_amount, currency, metadata, payment_code, mode, created_at, updated_at)
     VALUES (?, '12345', ?, 100000, 'VND', ?, ?, 'ORIGINAL', ?, ?)`
  ).bind(orderId, orderStatus, metadata, paymentCode, now, now).run();
  await env.DB.prepare(
    `INSERT INTO order_items (id, order_id, font_id, font_name, price, created_at)
     VALUES (?, ?, 'roboto-flex', 'Roboto Flex', 100000, ?)`
  ).bind(unique('item'), orderId, now).run();
  await env.DB.prepare(
    `INSERT INTO payments (id, order_id, provider, transaction_id, amount, currency, status, created_at, updated_at)
      VALUES (?, ?, 'SEPAY', ?, ?, ?, 'VERIFIED', ?, ?)`
  ).bind(paymentId, orderId, paymentTransactionId, paymentAmount, paymentCurrency, now, now).run();
  await env.DB.prepare(
    `INSERT INTO fulfillment_jobs (
       id, order_id, status, leased_at, lease_expires_at, lease_owner, lease_token,
       attempt_count, max_attempts, next_retry_at, last_error,
       artifact_key, artifact_sha256, artifact_size_bytes, artifact_parts, completed_at,
       created_at, updated_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, ?)`
  ).bind(
    jobId,
    orderId,
    jobStatus,
    leasedAt,
    leaseExpiresAt,
    leaseOwnerValue,
    leaseTokenValue,
    attemptCount,
    maxAttempts,
    lastError,
    artifactKey,
    artifactSha,
    completedShape ? 4 : null,
    completedShape ? now - 10_000 : null,
    now - 120_000,
    now - 30_000,
  ).run();

  if (options.includeOutbox !== false) {
    await env.DB.prepare(
      `INSERT INTO outbox_events (
         id, event_type, aggregate_type, aggregate_id, payload, status, dispatched_at, created_at,
         dispatch_lease_token, dispatch_leased_at, dispatch_lease_expires_at,
         dispatch_attempts, next_dispatch_at, last_dispatch_error
       ) VALUES (?, 'JOB_READY', 'ORDER', ?, ?, ?, ?, ?, NULL, NULL, NULL, 1, NULL, NULL)`
    ).bind(
      outboxId,
      orderId,
      JSON.stringify({ job_id: options.payloadJobId ?? jobId }),
      options.outboxStatus ?? 'SENT',
      options.outboxStatus === 'PENDING' ? null : now - 30_000,
      now - 60_000,
    ).run();

    if (options.includeSecondOutbox) {
      await env.DB.prepare(
        `INSERT INTO outbox_events (
           id, event_type, aggregate_type, aggregate_id, payload, status, dispatched_at, created_at,
           dispatch_lease_token, dispatch_leased_at, dispatch_lease_expires_at,
           dispatch_attempts, next_dispatch_at, last_dispatch_error
         ) VALUES (?, 'JOB_READY', 'ORDER_DUPLICATE', ?, ?, 'SENT', ?, ?, NULL, NULL, NULL, 1, NULL, NULL)`
      ).bind(
        unique('outbox'),
        orderId,
        JSON.stringify({ job_id: jobId }),
        now - 30_000,
        now - 20_000,
      ).run();
    }
  }

  if (options.includeReceipt || completedShape) {
    await env.DB.prepare(
      `INSERT INTO fulfillment_receipts (
         job_id, order_id, artifact_key, artifact_sha256, artifact_size_bytes,
         artifact_parts, completed_at, created_at
       ) VALUES (?, ?, ?, ?, 4, NULL, ?, ?)`
    ).bind(
      jobId,
      orderId,
      artifactKey ?? `artifacts/${orderId}/${jobId}/receipt.zip`,
      artifactSha ?? 'a'.repeat(64),
      now,
      now,
    ).run();
  }

  if (options.includeArtifact) {
    await env.DB.prepare(
      `INSERT INTO artifacts (
         id, order_id, job_id, storage_key, file_name, file_size, mime_type, created_at
       ) VALUES (?, ?, ?, ?, ?, 4, 'application/zip', ?)`
    ).bind(
      unique('artifact'),
      orderId,
      jobId,
      `artifacts/${orderId}/${jobId}/artifact.zip`,
      `${orderId}.zip`,
      now,
    ).run();
  }

  return {
    jobId,
    orderId,
    outboxId,
    paymentId,
    paymentTransactionId,
    paymentCode,
    leaseOwner,
    leaseToken,
    expected: {
      order_id: orderId,
      outbox_id: outboxId,
      payment_id: paymentId,
      payment_transaction_id: paymentTransactionId,
      payment_code: paymentCode,
      attempt_count: attemptCount,
      max_attempts: maxAttempts,
      last_error: lastError ?? MAX_ATTEMPTS_EXHAUSTED_REASON,
      dispatch_attempts: 1,
    },
  };
}

async function callRecover(
  fixture: Fixture,
  body: Record<string, unknown> = fixture.expected,
  authorization: string | null = `Bearer ${NODE_SECRET}`,
): Promise<{ response: Response; data: Record<string, unknown> }> {
  const headers = new Headers({ 'Content-Type': 'application/json' });
  if (authorization !== null) headers.set('Authorization', authorization);
  const request = new Request(`http://example.com/internal/jobs/${fixture.jobId}/recover`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
  const context = createExecutionContext();
  const response = await worker.fetch(request, testEnv, context);
  await waitOnExecutionContext(context);
  return { response, data: (await response.json()) as Record<string, unknown> };
}

async function callRescue(
  fixture: Fixture,
  body: Record<string, unknown>,
): Promise<{ response: Response; data: Record<string, unknown> }> {
  const request = new Request(`http://example.com/internal/jobs/${fixture.jobId}/rescue`, {
    method: 'POST',
    headers: new Headers({
      'Content-Type': 'application/json',
      Authorization: `Bearer ${NODE_SECRET}`,
    }),
    body: JSON.stringify(body),
  });
  const context = createExecutionContext();
  const response = await worker.fetch(request, testEnv, context);
  await waitOnExecutionContext(context);
  return { response, data: (await response.json()) as Record<string, unknown> };
}

describe('terminal FAILED fulfillment recovery', () => {
  it('recovers a failJob-terminal FAILED job once and recomputes attempts for one more pass', async () => {
    const fixture = await createFixture();
    const first = await callRecover(fixture);
    expect(first.response.status).toBe(200);
    expect(first.data).toEqual({ success: true, status: 'RECOVERED' });

    const job = await env.DB.prepare(
      `SELECT status, attempt_count, max_attempts, next_retry_at, last_error,
              lease_owner, lease_token, leased_at, lease_expires_at
       FROM fulfillment_jobs WHERE id = ?`
    ).bind(fixture.jobId).first<Record<string, unknown>>();
    const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
      .bind(fixture.orderId).first<{ status: string }>();
    const outbox = await env.DB.prepare(
      'SELECT status, dispatch_attempts, dispatched_at, last_dispatch_error FROM outbox_events WHERE id = ?'
    ).bind(fixture.outboxId).first<Record<string, unknown>>();

    expect(job).toEqual({
      status: 'RETRY',
      attempt_count: 3,
      max_attempts: 4,
      next_retry_at: null,
      last_error: TERMINAL_FAILED_RECOVERY_REASON,
      lease_owner: null,
      lease_token: null,
      leased_at: null,
      lease_expires_at: null,
    });
    expect(order?.status).toBe('PROCESSING');
    expect(outbox).toMatchObject({
      status: 'PENDING',
      dispatch_attempts: 1,
      dispatched_at: null,
      last_dispatch_error: TERMINAL_FAILED_RECOVERY_REASON,
    });

    expect((await env.DB.prepare('SELECT COUNT(*) AS count FROM payments WHERE order_id = ?')
      .bind(fixture.orderId).first<{ count: number }>())?.count).toBe(1);
    expect((await env.DB.prepare('SELECT COUNT(*) AS count FROM fulfillment_jobs WHERE order_id = ?')
      .bind(fixture.orderId).first<{ count: number }>())?.count).toBe(1);
    expect((await env.DB.prepare('SELECT COUNT(*) AS count FROM outbox_events WHERE aggregate_id = ?')
      .bind(fixture.orderId).first<{ count: number }>())?.count).toBe(1);
  });

  it('re-enqueues through the canonical cron outbox dispatch', async () => {
    const fixture = await createFixture();
    expect((await callRecover(fixture)).response.status).toBe(200);

    const queueSentMessages: unknown[] = [];
    const outboxService = new OutboxService(env.DB, {
      send: async (message: unknown) => queueSentMessages.push(message),
      sendBatch: async () => {},
    } as unknown as Queue<unknown>);
    await outboxService.dispatchPendingEvents({ batchSize: 50 });

    const dispatched = await env.DB.prepare(
      'SELECT status, dispatch_attempts FROM outbox_events WHERE id = ?'
    ).bind(fixture.outboxId).first<{ status: string; dispatch_attempts: number }>();
    expect(queueSentMessages).toContainEqual({ job_id: fixture.jobId });
    expect(dispatched).toEqual({ status: 'SENT', dispatch_attempts: 2 });
  });

  it('is claimable exactly like a fresh job and preserves the single active lease invariant', async () => {
    const fixture = await createFixture();
    expect((await callRecover(fixture)).response.status).toBe(200);

    const service = new JobService(env.DB);
    const claim = await service.claimJob(fixture.jobId, 'recover-worker', 60);
    expect(claim.status).toBe('CLAIMED');
    expect(claim.payload?.order_id).toBe(fixture.orderId);
    expect(claim.payload?.lease_token).toMatch(/^[0-9a-f-]{36}$/);

    const claimed = await env.DB.prepare(
      'SELECT attempt_count, max_attempts, status, lease_owner FROM fulfillment_jobs WHERE id = ?'
    ).bind(fixture.jobId).first<Record<string, unknown>>();
    expect(claimed).toMatchObject({
      attempt_count: 4,
      max_attempts: 4,
      status: 'PROCESSING',
      lease_owner: 'recover-worker',
    });

    // The granted pass consumes the final attempt, so a rival/duplicate claim
    // is fenced exactly like a fresh job at max attempts: TERMINAL with ack,
    // never a second active lease.
    const rivalClaim = await service.claimJob(fixture.jobId, 'recover-worker-2', 60);
    expect(rivalClaim.status).toBe('TERMINAL');
    expect(rivalClaim.queue_action).toBe('ack');
    expect(rivalClaim.reason).toBe(MAX_ATTEMPTS_EXHAUSTED_REASON);

    const fenced = await env.DB.prepare(
      'SELECT lease_owner, lease_token FROM fulfillment_jobs WHERE id = ?'
    ).bind(fixture.jobId).first<Record<string, unknown>>();
    expect(fenced?.lease_owner).toBe('recover-worker');

    const staleHeartbeat = await service.heartbeat(
      fixture.jobId,
      'recover-worker',
      '11111111-1111-1111-1111-111111111111',
    );
    expect(staleHeartbeat.status).toBe('EXPIRED_OR_FENCED');
  });

  it('grants exactly one additional fulfillment pass before terminal exhaustion', async () => {
    const fixture = await createFixture();
    expect((await callRecover(fixture)).response.status).toBe(200);

    const service = new JobService(env.DB);
    const claim = await service.claimJob(fixture.jobId, 'recover-worker', 60);
    expect(claim.status).toBe('CLAIMED');

    const fail = await service.failJob({
      jobId: fixture.jobId,
      workerId: 'recover-worker',
      leaseToken: claim.payload!.lease_token,
      retryable: true,
    });
    expect(fail.status).toBe('FAILED');
    expect(fail.reason).toBe(MAX_ATTEMPTS_EXHAUSTED_REASON);

    const job = await env.DB.prepare(
      'SELECT status, attempt_count, max_attempts, lease_owner FROM fulfillment_jobs WHERE id = ?'
    ).bind(fixture.jobId).first<Record<string, unknown>>();
    const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
      .bind(fixture.orderId).first<{ status: string }>();
    expect(job).toMatchObject({
      status: 'FAILED',
      attempt_count: 4,
      max_attempts: 4,
      lease_owner: null,
    });
    expect(order?.status).toBe('FAILED');
  });

  it('is idempotent for a second call and bounded to the recovery window', async () => {
    const fixture = await createFixture();
    expect((await callRecover(fixture)).data).toEqual({ success: true, status: 'RECOVERED' });

    const second = await callRecover(fixture);
    expect(second.response.status).toBe(200);
    expect(second.data).toEqual({ success: true, status: 'ALREADY_RECOVERED' });

    const job = await env.DB.prepare(
      'SELECT attempt_count, max_attempts FROM fulfillment_jobs WHERE id = ?'
    ).bind(fixture.jobId).first<Record<string, unknown>>();
    const outbox = await env.DB.prepare(
      'SELECT status, dispatch_attempts FROM outbox_events WHERE id = ?'
    ).bind(fixture.outboxId).first<Record<string, unknown>>();
    expect(job).toEqual({ attempt_count: 3, max_attempts: 4 });
    expect(outbox).toEqual({ status: 'PENDING', dispatch_attempts: 1 });

    // Once the canonical cron dispatch moves the outbox row back to SENT,
    // the bounded recovery window is closed: a late replay is a conflict.
    const outboxService = new OutboxService(env.DB, {
      send: async () => {},
      sendBatch: async () => {},
    } as unknown as Queue<unknown>);
    await outboxService.dispatchPendingEvents({ batchSize: 50 });
    expect((await callRecover(fixture)).response.status).toBe(409);
  });

  it('rejects non-FAILED jobs and stays disjoint from the lease-retained rescue surface', async () => {
    const processing = await createFixture({
      jobStatus: 'PROCESSING',
      orderStatus: 'PROCESSING',
      retainLease: true,
    });
    expect((await callRecover(processing)).response.status).toBe(409);

    const pending = await createFixture({ jobStatus: 'PENDING', orderStatus: 'PAID' });
    expect((await callRecover(pending)).response.status).toBe(409);

    const completed = await createFixture({ jobStatus: 'COMPLETED', orderStatus: 'COMPLETED' });
    expect((await callRecover(completed)).response.status).toBe(409);

    // finalize-path FAILED retains its lease: recover must reject while the
    // existing rescue surface still owns exactly that lineage.
    const leaseRetained = await createFixture({ retainLease: true });
    expect((await callRecover(leaseRetained)).response.status).toBe(409);
    const rescueBody = {
      ...leaseRetained.expected,
      lease_owner: leaseRetained.leaseOwner,
      lease_expires_at: (await env.DB.prepare(
        'SELECT lease_expires_at FROM fulfillment_jobs WHERE id = ?'
      ).bind(leaseRetained.jobId).first<{ lease_expires_at: number }>())!.lease_expires_at,
    };
    const rescue = await callRescue(leaseRetained, rescueBody);
    expect(rescue.response.status).toBe(200);
    expect(rescue.data).toEqual({ success: true, status: 'RESCUED' });
    const rescued = await env.DB.prepare(
      'SELECT status, last_error FROM fulfillment_jobs WHERE id = ?'
    ).bind(leaseRetained.jobId).first<Record<string, unknown>>();
    expect(rescued).toEqual({ status: 'RETRY', last_error: MAX_ATTEMPTS_EXHAUSTED_RESCUE_REASON });
  });

  it('fails closed for authorization, schema drift, and lineage drift', async () => {
    const unauthorizedFixture = await createFixture();
    const unauthorized = await callRecover(
      unauthorizedFixture,
      unauthorizedFixture.expected,
      'Bearer wrong',
    );
    expect(unauthorized.response.status).toBe(401);

    const malformedFixture = await createFixture();
    const extraKey = await callRecover(malformedFixture, {
      ...malformedFixture.expected,
      extra: true,
    });
    expect(extraKey.response.status).toBe(400);
    const missingBody = { ...malformedFixture.expected };
    delete missingBody.payment_code;
    expect((await callRecover(malformedFixture, missingBody)).response.status).toBe(400);

    const cases: Array<{
      options: FixtureOptions;
      mutate?: (body: Record<string, unknown>) => Record<string, unknown>;
    }> = [
      { options: {}, mutate: (body) => ({ ...body, payment_id: unique('wrong-payment') }) },
      { options: { paymentAmount: 99999 } },
      { options: { paymentCurrency: 'USD' } },
      { options: { includeOutbox: false } },
      { options: { includeSecondOutbox: true } },
      { options: { outboxStatus: 'PENDING' } },
      { options: { payloadJobId: unique('other-job') } },
      { options: {}, mutate: (body) => ({ ...body, attempt_count: 2 }) },
      { options: {}, mutate: (body) => ({ ...body, last_error: 'OTHER_FAILURE' }) },
      { options: { includeReceipt: true } },
      { options: { includeArtifact: true } },
      { options: { leaseResidue: 'live' } },
    ];

    for (const testCase of cases) {
      const fixture = await createFixture(testCase.options);
      const body = testCase.mutate ? testCase.mutate(fixture.expected) : fixture.expected;
      const result = await callRecover(fixture, body);
      expect(result.response.status).toBe(409);
    }
  });

  it('serializes concurrent recover calls to exactly one recovery', async () => {
    const fixture = await createFixture();
    const results = await Promise.all([callRecover(fixture), callRecover(fixture)]);
    expect(results.map((result) => result.response.status).sort()).toEqual([200, 200]);
    expect(results.map((result) => result.data.status).sort()).toEqual([
      'ALREADY_RECOVERED',
      'RECOVERED',
    ]);

    const job = await env.DB.prepare(
      'SELECT status, attempt_count, max_attempts FROM fulfillment_jobs WHERE id = ?'
    ).bind(fixture.jobId).first<Record<string, unknown>>();
    const outbox = await env.DB.prepare(
      'SELECT status, dispatch_attempts FROM outbox_events WHERE id = ?'
    ).bind(fixture.outboxId).first<Record<string, unknown>>();
    expect(job).toEqual({ status: 'RETRY', attempt_count: 3, max_attempts: 4 });
    expect(outbox).toEqual({ status: 'PENDING', dispatch_attempts: 1 });
  });

  it('recovers a reaper-terminal FAILED row (token cleared, expired owner residue) and normalizes all four lease fields', async () => {
    const fixture = await createFixture({ leaseResidue: 'expired' });

    // Production reaper shape (issue 90 attempt 5): the cron fencing write
    // cleared lease_token but left owner/leased_at/expires residue past expiry.
    const before = await env.DB.prepare(
      `SELECT status, lease_owner, lease_token, leased_at, lease_expires_at
       FROM fulfillment_jobs WHERE id = ?`
    ).bind(fixture.jobId).first<Record<string, unknown>>();
    expect(before).toMatchObject({
      status: 'FAILED',
      lease_owner: fixture.leaseOwner,
      lease_token: null,
    });
    expect(typeof before?.leased_at).toBe('number');
    expect(typeof before?.lease_expires_at).toBe('number');
    expect(before?.lease_expires_at as number).toBeLessThan(Date.now());

    const first = await callRecover(fixture);
    expect(first.response.status).toBe(200);
    expect(first.data).toEqual({ success: true, status: 'RECOVERED' });

    const job = await env.DB.prepare(
      `SELECT status, attempt_count, max_attempts, next_retry_at, last_error,
              lease_owner, lease_token, leased_at, lease_expires_at
       FROM fulfillment_jobs WHERE id = ?`
    ).bind(fixture.jobId).first<Record<string, unknown>>();
    const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
      .bind(fixture.orderId).first<{ status: string }>();
    const outbox = await env.DB.prepare(
      'SELECT status, dispatch_attempts, dispatched_at, last_dispatch_error FROM outbox_events WHERE id = ?'
    ).bind(fixture.outboxId).first<Record<string, unknown>>();

    // The same atomic UPDATE normalizes the expired residue to NULL.
    expect(job).toEqual({
      status: 'RETRY',
      attempt_count: 3,
      max_attempts: 4,
      next_retry_at: null,
      last_error: TERMINAL_FAILED_RECOVERY_REASON,
      lease_owner: null,
      lease_token: null,
      leased_at: null,
      lease_expires_at: null,
    });
    expect(order?.status).toBe('PROCESSING');
    expect(outbox).toMatchObject({
      status: 'PENDING',
      dispatch_attempts: 1,
      dispatched_at: null,
      last_dispatch_error: TERMINAL_FAILED_RECOVERY_REASON,
    });
  });

  it('rejects live leases: unexpired owner residue stays rejected with zero writes', async () => {
    const liveResidue = await createFixture({ leaseResidue: 'live' });
    const live = await callRecover(liveResidue);
    expect(live.response.status).toBe(409);
    expect(live.data).toEqual({
      error: 'Recover preconditions not met',
      status: 'CONFLICT',
      http_status: 409,
    });

    // Row byte-identical: the rejected batch leaks no partial write.
    const job = await env.DB.prepare(
      `SELECT status, attempt_count, max_attempts, lease_owner, lease_token, leased_at, lease_expires_at
       FROM fulfillment_jobs WHERE id = ?`
    ).bind(liveResidue.jobId).first<Record<string, unknown>>();
    expect(job).toMatchObject({
      status: 'FAILED',
      attempt_count: 3,
      max_attempts: 3,
      lease_owner: liveResidue.leaseOwner,
      lease_token: null,
    });
    expect(typeof job?.leased_at).toBe('number');
    expect(job?.lease_expires_at as number).toBeGreaterThan(Date.now());

    const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
      .bind(liveResidue.orderId).first<{ status: string }>();
    const outbox = await env.DB.prepare('SELECT status FROM outbox_events WHERE id = ?')
      .bind(liveResidue.outboxId).first<{ status: string }>();
    expect(order?.status).toBe('FAILED');
    expect(outbox?.status).toBe('SENT');
  });

  it('preserves fencing invariants after reaper-terminal recovery (single claim, rival fenced, stale heartbeat rejected)', async () => {
    const fixture = await createFixture({ leaseResidue: 'expired' });
    expect((await callRecover(fixture)).response.status).toBe(200);

    const service = new JobService(env.DB);
    const claim = await service.claimJob(fixture.jobId, 'recover-worker', 60);
    expect(claim.status).toBe('CLAIMED');
    expect(claim.payload?.order_id).toBe(fixture.orderId);
    expect(claim.payload?.lease_token).toMatch(/^[0-9a-f-]{36}$/);

    const claimed = await env.DB.prepare(
      'SELECT attempt_count, max_attempts, status, lease_owner FROM fulfillment_jobs WHERE id = ?'
    ).bind(fixture.jobId).first<Record<string, unknown>>();
    expect(claimed).toMatchObject({
      attempt_count: 4,
      max_attempts: 4,
      status: 'PROCESSING',
      lease_owner: 'recover-worker',
    });

    // The granted pass consumes the final attempt, so a rival claim is fenced
    // exactly like a fresh job at max attempts: TERMINAL with ack.
    const rivalClaim = await service.claimJob(fixture.jobId, 'recover-worker-2', 60);
    expect(rivalClaim.status).toBe('TERMINAL');
    expect(rivalClaim.queue_action).toBe('ack');
    expect(rivalClaim.reason).toBe(MAX_ATTEMPTS_EXHAUSTED_REASON);

    const fenced = await env.DB.prepare(
      'SELECT lease_owner FROM fulfillment_jobs WHERE id = ?'
    ).bind(fixture.jobId).first<Record<string, unknown>>();
    expect(fenced?.lease_owner).toBe('recover-worker');

    const staleHeartbeat = await service.heartbeat(
      fixture.jobId,
      'recover-worker',
      '11111111-1111-1111-1111-111111111111',
    );
    expect(staleHeartbeat.status).toBe('EXPIRED_OR_FENCED');
  });

  it('is idempotent across replays of a reaper-terminal recovery', async () => {
    const fixture = await createFixture({ leaseResidue: 'expired' });
    expect((await callRecover(fixture)).data).toEqual({ success: true, status: 'RECOVERED' });

    const second = await callRecover(fixture);
    expect(second.response.status).toBe(200);
    expect(second.data).toEqual({ success: true, status: 'ALREADY_RECOVERED' });

    const job = await env.DB.prepare(
      `SELECT attempt_count, max_attempts, lease_owner, lease_token, leased_at, lease_expires_at
       FROM fulfillment_jobs WHERE id = ?`
    ).bind(fixture.jobId).first<Record<string, unknown>>();
    expect(job).toEqual({
      attempt_count: 3,
      max_attempts: 4,
      lease_owner: null,
      lease_token: null,
      leased_at: null,
      lease_expires_at: null,
    });
  });

  it('serializes concurrent recover calls on a reaper-terminal row to exactly one recovery', async () => {
    const fixture = await createFixture({ leaseResidue: 'expired' });
    const results = await Promise.all([callRecover(fixture), callRecover(fixture)]);
    expect(results.map((result) => result.response.status).sort()).toEqual([200, 200]);
    expect(results.map((result) => result.data.status).sort()).toEqual([
      'ALREADY_RECOVERED',
      'RECOVERED',
    ]);

    const job = await env.DB.prepare(
      'SELECT status, attempt_count, max_attempts FROM fulfillment_jobs WHERE id = ?'
    ).bind(fixture.jobId).first<Record<string, unknown>>();
    expect(job).toEqual({ status: 'RETRY', attempt_count: 3, max_attempts: 4 });
  });
});

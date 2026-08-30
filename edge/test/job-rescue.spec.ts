import { describe, expect, it } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker, { type Env } from '../src/index';
import {
  JobService,
  MAX_ATTEMPTS_EXHAUSTED_REASON,
  MAX_ATTEMPTS_EXHAUSTED_RESCUE_REASON,
} from '../src/services/job-service';
import { OutboxService } from '../src/services/outbox-service';

const NODE_SECRET = 'a23_rescue_test_secret_999';
const testEnv: Env = {
  ...(env as unknown as Env),
  A23_NODE_SECRET: NODE_SECRET,
  A23_JOB_LEASE_SECONDS: '300',
};

type FixtureOptions = {
  jobStatus?: 'PROCESSING' | 'FAILED';
  orderStatus?: 'PROCESSING' | 'FAILED';
  leaseExpiresAt?: number;
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
  leaseExpiresAt: number;
  expected: Record<string, unknown>;
};

function unique(prefix: string): string {
  return `${prefix}_${crypto.randomUUID().replace(/-/g, '')}`;
}

async function createFixture(options: FixtureOptions = {}): Promise<Fixture> {
  const now = Date.now();
  const jobId = unique('job');
  const orderId = unique('ord');
  const outboxId = unique('outbox');
  const paymentId = unique('payment');
  const paymentTransactionId = unique('txn');
  const paymentCode = unique('TF');
  const leaseOwner = 'a23-worker-1';
  const leaseToken = '00000000-0000-0000-0000-000000000001';
  const leaseExpiresAt = options.leaseExpiresAt ?? now - 60_000;
  const attemptCount = options.attemptCount ?? 3;
  const maxAttempts = options.maxAttempts ?? 3;
  const paymentAmount = options.paymentAmount ?? 100000;
  const paymentCurrency = options.paymentCurrency ?? 'VND';
  const jobStatus = options.jobStatus ?? 'PROCESSING';
  const orderStatus = options.orderStatus ?? 'PROCESSING';
  const lastError = options.lastError === undefined
    ? (jobStatus === 'FAILED' ? MAX_ATTEMPTS_EXHAUSTED_REASON : null)
    : options.lastError;
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
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, NULL, NULL, NULL, ?, ?)`
  ).bind(
    jobId,
    orderId,
    jobStatus,
    now - 120_000,
    leaseExpiresAt,
    leaseOwner,
    leaseToken,
    attemptCount,
    maxAttempts,
    lastError,
    now - 120_000,
    now - 30_000,
  ).run();

  if (options.includeOutbox !== false) {
    await env.DB.prepare(
      `INSERT INTO outbox_events (
         id, event_type, aggregate_type, aggregate_id, payload, status, dispatched_at, created_at,
         dispatch_lease_token, dispatch_leased_at, dispatch_lease_expires_at,
         dispatch_attempts, next_dispatch_at, last_dispatch_error
       ) VALUES (?, 'JOB_READY', 'ORDER', ?, ?, ?, ?, ?, NULL, NULL, NULL, 2, NULL, NULL)`
    ).bind(
      outboxId,
      orderId,
      JSON.stringify({ job_id: options.payloadJobId ?? jobId }),
      options.outboxStatus ?? 'SENT',
      now - 30_000,
      now - 60_000,
    ).run();

    if (options.includeSecondOutbox) {
      await env.DB.prepare(
        `INSERT INTO outbox_events (
           id, event_type, aggregate_type, aggregate_id, payload, status, dispatched_at, created_at,
           dispatch_lease_token, dispatch_leased_at, dispatch_lease_expires_at,
           dispatch_attempts, next_dispatch_at, last_dispatch_error
         ) VALUES (?, 'JOB_READY', 'ORDER_DUPLICATE', ?, ?, 'SENT', ?, ?, NULL, NULL, NULL, 2, NULL, NULL)`
      ).bind(
        unique('outbox'),
        orderId,
        JSON.stringify({ job_id: jobId }),
        now - 30_000,
        now - 20_000,
      ).run();
    }
  }

  if (options.includeReceipt) {
    await env.DB.prepare(
      `INSERT INTO fulfillment_receipts (
         job_id, order_id, artifact_key, artifact_sha256, artifact_size_bytes,
         artifact_parts, completed_at, created_at
       ) VALUES (?, ?, ?, ?, 4, NULL, ?, ?)`
    ).bind(jobId, orderId, `artifacts/${orderId}/${jobId}/receipt.zip`, 'a'.repeat(64), now, now).run();
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
    leaseExpiresAt,
    expected: {
      order_id: orderId,
      outbox_id: outboxId,
      payment_id: paymentId,
      payment_transaction_id: paymentTransactionId,
      payment_code: paymentCode,
      lease_owner: leaseOwner,
      lease_expires_at: leaseExpiresAt,
      attempt_count: attemptCount,
      max_attempts: maxAttempts,
      last_error: lastError ?? MAX_ATTEMPTS_EXHAUSTED_REASON,
      dispatch_attempts: 2,
    },
  };
}

async function callRescue(
  fixture: Fixture,
  body: Record<string, unknown> = fixture.expected,
  authorization: string | null = `Bearer ${NODE_SECRET}`,
): Promise<{ response: Response; data: Record<string, unknown> }> {
  const headers = new Headers({ 'Content-Type': 'application/json' });
  if (authorization !== null) headers.set('Authorization', authorization);
  const request = new Request(`http://example.com/internal/jobs/${fixture.jobId}/rescue`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
  const context = createExecutionContext();
  const response = await worker.fetch(request, testEnv, context);
  await waitOnExecutionContext(context);
  return { response, data: (await response.json()) as Record<string, unknown> };
}

describe('exact exhausted fulfillment rescue', () => {
  it('lets a winning heartbeat prevent scheduled finalization', async () => {
    const fixture = await createFixture({ leaseExpiresAt: Date.now() + 60_000 });
    const service = new JobService(env.DB);

    expect((await service.heartbeat(
      fixture.jobId,
      fixture.leaseOwner,
      fixture.leaseToken,
    )).status).toBe('EXTENDED');
    expect(await service.finalizeExpiredExhaustedJobs(Date.now())).toEqual({
      finalizedJobs: 0,
      transitionedOrders: 0,
    });

    const job = await env.DB.prepare('SELECT status, lease_expires_at FROM fulfillment_jobs WHERE id = ?')
      .bind(fixture.jobId).first<{ status: string; lease_expires_at: number }>();
    const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
      .bind(fixture.orderId).first<{ status: string }>();
    expect(job?.status).toBe('PROCESSING');
    expect(job?.lease_expires_at).toBeGreaterThan(Date.now());
    expect(order?.status).toBe('PROCESSING');
  });

  it('lets completion win against scheduled finalization with one receipt, artifact, and delivery event', async () => {
    const fixture = await createFixture({ leaseExpiresAt: Date.now() + 60_000 });
    const service = new JobService(env.DB);
    const artifactKey = `artifacts/${fixture.orderId}/${fixture.jobId}/complete.zip`;
    const result = await service.completeJob({
      jobId: fixture.jobId,
      workerId: fixture.leaseOwner,
      leaseToken: fixture.leaseToken,
      artifactKey,
      artifactSha256: 'b'.repeat(64),
      artifactSizeBytes: 4,
    });

    expect(result.status).toBe('COMPLETED');
    expect(await service.finalizeExpiredExhaustedJobs(Date.now())).toEqual({
      finalizedJobs: 0,
      transitionedOrders: 0,
    });

    const job = await env.DB.prepare('SELECT status FROM fulfillment_jobs WHERE id = ?')
      .bind(fixture.jobId).first<{ status: string }>();
    const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
      .bind(fixture.orderId).first<{ status: string }>();
    const receipt = await env.DB.prepare('SELECT COUNT(*) AS count FROM fulfillment_receipts WHERE job_id = ?')
      .bind(fixture.jobId).first<{ count: number }>();
    const artifact = await env.DB.prepare('SELECT COUNT(*) AS count FROM artifacts WHERE job_id = ?')
      .bind(fixture.jobId).first<{ count: number }>();
    const delivery = await env.DB.prepare(
      "SELECT COUNT(*) AS count FROM outbox_events WHERE aggregate_id = ? AND event_type = 'DELIVERY_READY'"
    ).bind(fixture.orderId).first<{ count: number }>();

    expect(job?.status).toBe('COMPLETED');
    expect(order?.status).toBe('COMPLETED');
    expect(receipt?.count).toBe(1);
    expect(artifact?.count).toBe(1);
    expect(delivery?.count).toBe(1);
  });

  it('scheduled maintenance finalizes expired max-attempt jobs and fences stale completion', async () => {
    const fixture = await createFixture();

    await worker.scheduled({} as ScheduledEvent, testEnv, {} as ExecutionContext);

    const job = await env.DB.prepare(
      'SELECT status, attempt_count, max_attempts, last_error, lease_owner, lease_token, lease_expires_at FROM fulfillment_jobs WHERE id = ?'
    ).bind(fixture.jobId).first<Record<string, unknown>>();
    const order = await env.DB.prepare('SELECT status FROM orders WHERE id = ?')
      .bind(fixture.orderId).first<{ status: string }>();

    expect(job).toMatchObject({
      status: 'FAILED',
      attempt_count: 3,
      max_attempts: 3,
      last_error: MAX_ATTEMPTS_EXHAUSTED_REASON,
      lease_owner: fixture.leaseOwner,
      lease_token: null,
      lease_expires_at: fixture.leaseExpiresAt,
    });
    expect(order?.status).toBe('FAILED');

    const service = new JobService(env.DB);
    expect((await service.heartbeat(
      fixture.jobId,
      fixture.leaseOwner,
      fixture.leaseToken,
    )).status).toBe('EXPIRED_OR_FENCED');
    expect((await service.completeJob({
      jobId: fixture.jobId,
      workerId: fixture.leaseOwner,
      leaseToken: fixture.leaseToken,
      artifactKey: 'artifacts/should-not-complete.zip',
      artifactSha256: 'b'.repeat(64),
      artifactSizeBytes: 4,
    })).status).toBe('EXPIRED_OR_FENCED');

    const receipts = await env.DB.prepare('SELECT COUNT(*) AS count FROM fulfillment_receipts WHERE job_id = ?')
      .bind(fixture.jobId).first<{ count: number }>();
    const artifacts = await env.DB.prepare('SELECT COUNT(*) AS count FROM artifacts WHERE job_id = ?')
      .bind(fixture.jobId).first<{ count: number }>();
    expect(receipts?.count).toBe(0);
    expect(artifacts?.count).toBe(0);
  });

  it('performs one exact rescue, preserves lineage, and grants only one added claim attempt', async () => {
    const fixture = await createFixture();
    await new JobService(env.DB).finalizeExpiredExhaustedJobs(Date.now());
    const first = await callRescue(fixture);
    expect(first.response.status).toBe(200);
    expect(first.data).toEqual({ success: true, status: 'RESCUED' });

    const job = await env.DB.prepare(
      'SELECT status, attempt_count, max_attempts, next_retry_at, last_error, lease_token FROM fulfillment_jobs WHERE id = ?'
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
      last_error: MAX_ATTEMPTS_EXHAUSTED_RESCUE_REASON,
      lease_token: null,
    });
    expect(order?.status).toBe('PROCESSING');
    expect(outbox).toMatchObject({
      status: 'PENDING',
      dispatch_attempts: 2,
      dispatched_at: null,
      last_dispatch_error: MAX_ATTEMPTS_EXHAUSTED_RESCUE_REASON,
    });

    expect((await env.DB.prepare('SELECT COUNT(*) AS count FROM payments WHERE order_id = ?')
      .bind(fixture.orderId).first<{ count: number }>())?.count).toBe(1);
    expect((await env.DB.prepare('SELECT COUNT(*) AS count FROM fulfillment_jobs WHERE order_id = ?')
      .bind(fixture.orderId).first<{ count: number }>())?.count).toBe(1);
    expect((await env.DB.prepare('SELECT COUNT(*) AS count FROM outbox_events WHERE aggregate_id = ?')
      .bind(fixture.orderId).first<{ count: number }>())?.count).toBe(1);

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
    expect(dispatched).toEqual({ status: 'SENT', dispatch_attempts: 3 });

    const replay = await callRescue(fixture);
    expect(replay.response.status).toBe(409);

    const claim = await new JobService(env.DB).claimJob(fixture.jobId, 'rescue-worker', 60);
    expect(claim.status).toBe('CLAIMED');
    const claimed = await env.DB.prepare('SELECT attempt_count, max_attempts FROM fulfillment_jobs WHERE id = ?')
      .bind(fixture.jobId).first<{ attempt_count: number; max_attempts: number }>();
    expect(claimed).toEqual({ attempt_count: 4, max_attempts: 4 });
  });

  it('fails closed for authorization, schema drift, lineage drift, artifacts, and outbox drift', async () => {
    const unauthorizedFixture = await createFixture();
    const unauthorized = await callRescue(unauthorizedFixture, unauthorizedFixture.expected, 'Bearer wrong');
    expect(unauthorized.response.status).toBe(401);

    const malformedFixture = await createFixture();
    const malformed = await callRescue(malformedFixture, {
      ...malformedFixture.expected,
      extra: true,
    });
    expect(malformed.response.status).toBe(400);

    const cases: Array<{ options: FixtureOptions; mutate?: (body: Record<string, unknown>) => Record<string, unknown> }> = [
      { options: { jobStatus: 'FAILED', orderStatus: 'FAILED', leaseExpiresAt: Date.now() + 60_000 } },
      { options: { jobStatus: 'FAILED', orderStatus: 'FAILED', attemptCount: 2 } },
      { options: { jobStatus: 'FAILED', orderStatus: 'FAILED', lastError: 'OTHER_FAILURE' } },
      { options: { jobStatus: 'FAILED', orderStatus: 'FAILED', includeReceipt: true } },
      { options: { jobStatus: 'FAILED', orderStatus: 'FAILED', includeArtifact: true } },
      { options: { jobStatus: 'FAILED', orderStatus: 'FAILED', includeOutbox: false } },
      { options: { jobStatus: 'FAILED', orderStatus: 'FAILED', includeSecondOutbox: true } },
      {
        options: { jobStatus: 'FAILED', orderStatus: 'FAILED', payloadJobId: unique('other-job') },
      },
      { options: { jobStatus: 'FAILED', orderStatus: 'FAILED', paymentAmount: 99999 } },
      { options: { jobStatus: 'FAILED', orderStatus: 'FAILED', paymentCurrency: 'USD' } },
      {
        options: { jobStatus: 'FAILED', orderStatus: 'FAILED' },
        mutate: (body) => ({ ...body, payment_id: unique('wrong-payment') }),
      },
    ];

    for (const testCase of cases) {
      const fixture = await createFixture(testCase.options);
      const body = testCase.mutate ? testCase.mutate(fixture.expected) : fixture.expected;
      const result = await callRescue(fixture, body);
      expect(result.response.status).toBe(409);
    }
  });

  it('serializes concurrent rescue calls to one attempt and one outbox rearm', async () => {
    const fixture = await createFixture();
    await new JobService(env.DB).finalizeExpiredExhaustedJobs(Date.now());
    const results = await Promise.all([callRescue(fixture), callRescue(fixture)]);
    expect(results.map((result) => result.response.status).sort()).toEqual([200, 409]);

    const job = await env.DB.prepare('SELECT status, attempt_count, max_attempts FROM fulfillment_jobs WHERE id = ?')
      .bind(fixture.jobId).first<Record<string, unknown>>();
    const outbox = await env.DB.prepare('SELECT status, dispatch_attempts FROM outbox_events WHERE id = ?')
      .bind(fixture.outboxId).first<Record<string, unknown>>();
    expect(job).toEqual({ status: 'RETRY', attempt_count: 3, max_attempts: 4 });
    expect(outbox).toEqual({ status: 'PENDING', dispatch_attempts: 2 });
  });
});

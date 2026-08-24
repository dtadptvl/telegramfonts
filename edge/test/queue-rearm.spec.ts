import { describe, expect, it } from 'vitest';
import { createExecutionContext, env, waitOnExecutionContext } from 'cloudflare:test';
import worker, { type Env } from '../src/index';

const NODE_SECRET = 'a23_super_secret_node_key_999';
const testEnv: Env = {
  ...(env as unknown as Env),
  A23_NODE_SECRET: NODE_SECRET,
  A23_JOB_LEASE_SECONDS: '300',
};
const RECOVERY_REASON = 'QUEUE_RETENTION_EXPIRED_RECOVERY';

type FixtureOptions = {
  jobStatus?: string;
  orderStatus?: string;
  leaseOwner?: string | null;
  leaseExpiresAt?: number | null;
  attemptCount?: number;
  maxAttempts?: number;
  outboxStatus?: string;
  dispatchedAt?: number | null;
  dispatchAttempts?: number;
  payloadJobId?: string;
  includeOutbox?: boolean;
  includeReceipt?: boolean;
  includeArtifact?: boolean;
  includeJobArtifact?: boolean;
};

type Fixture = {
  jobId: string;
  orderId: string;
  outboxId: string;
  paymentId: string;
  expected: {
    order_id: string;
    outbox_id: string;
    lease_owner: string;
    lease_expires_at: number;
    attempt_count: number;
    dispatch_attempts: number;
  };
};

function unique(prefix: string): string {
  return `${prefix}_${crypto.randomUUID().replace(/-/g, '')}`;
}

async function createFixture(options: FixtureOptions = {}): Promise<Fixture> {
  const now = Date.now();
  const jobId = unique('job');
  const orderId = unique('ord');
  const outboxId = unique('outbox');
  const paymentId = unique('pay');
  const leaseOwner = options.leaseOwner === undefined ? 'a23-worker-1' : options.leaseOwner;
  const leaseExpiresAt = options.leaseExpiresAt === undefined ? now - 60_000 : options.leaseExpiresAt;
  const attemptCount = options.attemptCount ?? 1;
  const maxAttempts = options.maxAttempts ?? 3;
  const dispatchAttempts = options.dispatchAttempts ?? 2;
  const payloadJobId = options.payloadJobId ?? jobId;

  await env.DB.prepare(
    `INSERT INTO orders (
       id, user_id, status, total_amount, currency, metadata, payment_code, created_at, updated_at
     ) VALUES (?, ?, ?, 100000, 'VND', ?, ?, ?, ?)`
  )
    .bind(
      orderId,
      '12345',
      options.orderStatus ?? 'PROCESSING',
      '{"source":"rearm-test"}',
      unique('PAY'),
      now,
      now
    )
    .run();

  await env.DB.prepare(
    `INSERT INTO payments (
       id, order_id, provider, transaction_id, amount, currency, status, created_at, updated_at
     ) VALUES (?, ?, 'SEPAY', ?, 100000, 'VND', 'VERIFIED', ?, ?)`
  )
    .bind(paymentId, orderId, unique('txn'), now, now)
    .run();

  const jobArtifactKey = options.includeJobArtifact ? `artifacts/${orderId}/${jobId}/job.zip` : null;
  const jobArtifactSha = options.includeJobArtifact ? 'a'.repeat(64) : null;
  const jobArtifactSize = options.includeJobArtifact ? 4 : null;

  await env.DB.prepare(
    `INSERT INTO fulfillment_jobs (
       id, order_id, status, leased_at, lease_expires_at, lease_owner, lease_token,
       attempt_count, max_attempts, next_retry_at, last_error,
       artifact_key, artifact_sha256, artifact_size_bytes, artifact_parts, completed_at,
       created_at, updated_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  )
    .bind(
      jobId,
      orderId,
      options.jobStatus ?? 'PROCESSING',
      now - 120_000,
      leaseExpiresAt,
      leaseOwner,
      '00000000-0000-0000-0000-000000000001',
      attemptCount,
      maxAttempts,
      now - 30_000,
      'preserve-this-job-error',
      jobArtifactKey,
      jobArtifactSha,
      jobArtifactSize,
      null,
      null,
      now - 120_000,
      now - 30_000
    )
    .run();

  if (options.includeOutbox !== false) {
    await env.DB.prepare(
      `INSERT INTO outbox_events (
         id, event_type, aggregate_type, aggregate_id, payload, status, dispatched_at, created_at,
         dispatch_lease_token, dispatch_leased_at, dispatch_lease_expires_at,
         dispatch_attempts, next_dispatch_at, last_dispatch_error
       ) VALUES (?, 'JOB_READY', 'ORDER', ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL)`
    )
      .bind(
        outboxId,
        orderId,
        JSON.stringify({ job_id: payloadJobId }),
        options.outboxStatus ?? 'SENT',
        options.dispatchedAt === undefined ? now - 30_000 : options.dispatchedAt,
        now - 60_000,
        dispatchAttempts
      )
      .run();
  }

  if (options.includeReceipt) {
    await env.DB.prepare(
      `INSERT INTO fulfillment_receipts (
         job_id, order_id, artifact_key, artifact_sha256, artifact_size_bytes,
         artifact_parts, completed_at, created_at
       ) VALUES (?, ?, ?, ?, 4, NULL, ?, ?)`
    )
      .bind(jobId, orderId, `artifacts/${orderId}/${jobId}/receipt.zip`, 'b'.repeat(64), now, now)
      .run();
  }

  if (options.includeArtifact) {
    await env.DB.prepare(
      `INSERT INTO artifacts (
         id, order_id, job_id, storage_key, file_name, file_size, mime_type, created_at
       ) VALUES (?, ?, ?, ?, ?, 4, 'application/zip', ?)`
    )
      .bind(
        unique('artifact'),
        orderId,
        jobId,
        `artifacts/${orderId}/${jobId}/artifact.zip`,
        `${orderId}.zip`,
        now
      )
      .run();
  }

  return {
    jobId,
    orderId,
    outboxId,
    paymentId,
    expected: {
      order_id: orderId,
      outbox_id: outboxId,
      lease_owner: leaseOwner || 'a23-worker-1',
      lease_expires_at: leaseExpiresAt === null ? now - 60_000 : leaseExpiresAt,
      attempt_count: attemptCount,
      dispatch_attempts: dispatchAttempts,
    },
  };
}

async function callRearm(
  fixture: Fixture,
  body: Record<string, unknown> = fixture.expected,
  authorization: string | null = `Bearer ${NODE_SECRET}`
): Promise<{ response: Response; data: Record<string, unknown> }> {
  const headers = new Headers({ 'Content-Type': 'application/json' });
  if (authorization !== null) headers.set('Authorization', authorization);
  const request = new Request(`http://example.com/internal/jobs/${fixture.jobId}/rearm`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
  const context = createExecutionContext();
  const response = await worker.fetch(request, testEnv, context);
  await waitOnExecutionContext(context);
  return { response, data: (await response.json()) as Record<string, unknown> };
}

async function readOutbox(fixture: Fixture): Promise<Record<string, unknown> | null> {
  return env.DB.prepare('SELECT * FROM outbox_events WHERE id = ?')
    .bind(fixture.outboxId)
    .first<Record<string, unknown>>();
}

describe('authenticated stale-Queue rearm transition', () => {
  it('rearms the exact expired event and preserves job, order, payment, and dispatch-attempt state', async () => {
    const fixture = await createFixture();
    const beforeJob = await env.DB.prepare('SELECT * FROM fulfillment_jobs WHERE id = ?')
      .bind(fixture.jobId)
      .first();
    const beforeOrder = await env.DB.prepare('SELECT * FROM orders WHERE id = ?')
      .bind(fixture.orderId)
      .first();
    const beforePayment = await env.DB.prepare('SELECT * FROM payments WHERE id = ?')
      .bind(fixture.paymentId)
      .first();

    const { response, data } = await callRearm(fixture);
    expect(response.status).toBe(200);
    expect(data).toEqual({ success: true, status: 'REARMED' });

    const afterJob = await env.DB.prepare('SELECT * FROM fulfillment_jobs WHERE id = ?')
      .bind(fixture.jobId)
      .first();
    const afterOrder = await env.DB.prepare('SELECT * FROM orders WHERE id = ?')
      .bind(fixture.orderId)
      .first();
    const afterPayment = await env.DB.prepare('SELECT * FROM payments WHERE id = ?')
      .bind(fixture.paymentId)
      .first();
    const outbox = await readOutbox(fixture);

    expect(afterJob).toEqual(beforeJob);
    expect(afterOrder).toEqual(beforeOrder);
    expect(afterPayment).toEqual(beforePayment);
    expect(outbox).toMatchObject({
      id: fixture.outboxId,
      event_type: 'JOB_READY',
      aggregate_type: 'ORDER',
      aggregate_id: fixture.orderId,
      status: 'PENDING',
      dispatch_attempts: 2,
      dispatched_at: null,
      dispatch_lease_token: null,
      dispatch_leased_at: null,
      dispatch_lease_expires_at: null,
      next_dispatch_at: null,
      last_dispatch_error: RECOVERY_REASON,
    });
  });

  it('fails closed for unauthorized, malformed, extra-field, and out-of-bounds requests', async () => {
    const fixture = await createFixture();
    const unauthorized = await callRearm(fixture, fixture.expected, 'Bearer wrong-secret');
    expect(unauthorized.response.status).toBe(401);

    const methodRequest = new Request(`http://example.com/internal/jobs/${fixture.jobId}/rearm`, {
      method: 'GET',
      headers: { Authorization: `Bearer ${NODE_SECRET}` },
    });
    const methodContext = createExecutionContext();
    const methodResponse = await worker.fetch(methodRequest, testEnv, methodContext);
    await waitOnExecutionContext(methodContext);
    expect(methodResponse.status).toBe(405);

    const malformedRequest = new Request(`http://example.com/internal/jobs/${fixture.jobId}/rearm`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${NODE_SECRET}`, 'Content-Type': 'application/json' },
      body: '{not-json',
    });
    const malformedContext = createExecutionContext();
    const malformedResponse = await worker.fetch(malformedRequest, testEnv, malformedContext);
    await waitOnExecutionContext(malformedContext);
    expect(malformedResponse.status).toBe(400);

    const extraField = await callRearm(fixture, { ...fixture.expected, extra: true });
    expect(extraField.response.status).toBe(400);

    const invalidNumber = await callRearm(fixture, { ...fixture.expected, attempt_count: -1 });
    expect(invalidNumber.response.status).toBe(400);
  });

  it('rejects active, null, and changed leases, exhausted or terminal jobs, and a wrong order', async () => {
    const cases: Array<{ options: FixtureOptions; body?: (fixture: Fixture) => Record<string, unknown> }> = [
      { options: { leaseExpiresAt: Date.now() + 60_000 } },
      { options: { leaseExpiresAt: null } },
      {
        options: { leaseExpiresAt: Date.now() - 60_000 },
        body: (fixture) => ({ ...fixture.expected, lease_expires_at: fixture.expected.lease_expires_at - 1 }),
      },
      { options: { attemptCount: 3, maxAttempts: 3 } },
      { options: { jobStatus: 'COMPLETED' } },
      {
        options: {},
        body: (fixture) => ({ ...fixture.expected, order_id: unique('wrong-order') }),
      },
    ];

    for (const testCase of cases) {
      const fixture = await createFixture(testCase.options);
      const result = await callRearm(fixture, testCase.body ? testCase.body(fixture) : fixture.expected);
      expect(result.response.status).toBe(409);
      expect(result.data).toEqual({ error: 'Rearm preconditions not met', status: 'CONFLICT' });
    }
  });

  it('rejects missing, wrong, already-PENDING, payload-mismatched, and changed-attempt outboxes', async () => {
    const missing = await createFixture();
    await env.DB.prepare('DELETE FROM outbox_events WHERE id = ?').bind(missing.outboxId).run();
    const missingResult = await callRearm(missing);
    expect(missingResult.response.status).toBe(409);

    const wrongId = await createFixture();
    const wrongIdResult = await callRearm(wrongId, { ...wrongId.expected, outbox_id: unique('wrong-outbox') });
    expect(wrongIdResult.response.status).toBe(409);

    const alreadyPending = await createFixture({ outboxStatus: 'PENDING' });
    const pendingResult = await callRearm(alreadyPending);
    expect(pendingResult.response.status).toBe(409);

    const payloadMismatch = await createFixture({ payloadJobId: unique('other-job') });
    const payloadResult = await callRearm(payloadMismatch);
    expect(payloadResult.response.status).toBe(409);

    const changedAttempts = await createFixture();
    const attemptsResult = await callRearm(changedAttempts, {
      ...changedAttempts.expected,
      dispatch_attempts: changedAttempts.expected.dispatch_attempts + 1,
    });
    expect(attemptsResult.response.status).toBe(409);
  });

  it('rejects when a receipt, artifact row, or job artifact identity is already present', async () => {
    const receipt = await createFixture({ includeReceipt: true });
    expect((await callRearm(receipt)).response.status).toBe(409);

    const artifact = await createFixture({ includeArtifact: true });
    expect((await callRearm(artifact)).response.status).toBe(409);

    const jobArtifact = await createFixture({ includeJobArtifact: true });
    expect((await callRearm(jobArtifact)).response.status).toBe(409);
  });

  it('is idempotent under concurrent replay and proves the exact recovery marker', async () => {
    const fixture = await createFixture();
    const results = await Promise.all([callRearm(fixture), callRearm(fixture)]);
    expect(results.map(({ data }) => data.status).sort()).toEqual(['ALREADY_REARMED', 'REARMED']);
    expect(results.every(({ response }) => response.status === 200)).toBe(true);

    const replay = await callRearm(fixture);
    expect(replay.response.status).toBe(200);
    expect(replay.data).toEqual({ success: true, status: 'ALREADY_REARMED' });
  });
});

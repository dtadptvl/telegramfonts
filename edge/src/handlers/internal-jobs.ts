import type { Env } from '../env';
import { JobService, resolveMaxJobAgeMs, buildArtifactStorageKey } from '../services/job-service';
import { emitStructuredLog } from '../utils/logger';

const INTERNAL_AUTH_COMPARE_BYTES = 1024;
const INTERNAL_AUTH_LENGTH_BYTES = 4;
const INTERNAL_AUTH_PAYLOAD_BYTES = INTERNAL_AUTH_COMPARE_BYTES - INTERNAL_AUTH_LENGTH_BYTES;

function fixedAuthValue(value: string, encoder: TextEncoder): { bytes: Uint8Array; withinLimit: boolean } {
  const encoded = encoder.encode(value);
  const bytes = new Uint8Array(INTERNAL_AUTH_COMPARE_BYTES);
  new DataView(bytes.buffer).setUint32(0, encoded.byteLength, false);
  bytes.set(encoded.subarray(0, INTERNAL_AUTH_PAYLOAD_BYTES), INTERNAL_AUTH_LENGTH_BYTES);
  return {
    bytes,
    withinLimit: encoded.byteLength <= INTERNAL_AUTH_PAYLOAD_BYTES,
  };
}

export function verifyInternalAuth(request: Request, secret: string | undefined): boolean {
  if (!secret || !secret.trim()) return false;
  const authHeader = request.headers.get('Authorization');
  if (!authHeader || !authHeader.startsWith('Bearer ')) return false;

  const token = authHeader.slice(7).trim();
  if (!token) return false;

  const enc = new TextEncoder();
  const tokenValue = fixedAuthValue(token, enc);
  const secretValue = fixedAuthValue(secret.trim(), enc);
  const equal = crypto.subtle.timingSafeEqual(tokenValue.bytes, secretValue.bytes);
  return tokenValue.withinLimit && secretValue.withinLimit && equal;
}

export function hexToArrayBuffer(hex: string): ArrayBuffer {
  const bytes = new Uint8Array(Math.ceil(hex.length / 2));
  for (let i = 0; i < bytes.length; i++) {
    bytes[i] = parseInt(hex.substring(i * 2, i * 2 + 2), 16);
  }
  return bytes.buffer;
}

export function checksumToHex(checksum: unknown): string | null {
  if (!checksum) return null;
  if (typeof checksum === 'string') return checksum.toLowerCase();
  if (checksum instanceof ArrayBuffer) {
    return Array.from(new Uint8Array(checksum))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');
  }
  if (ArrayBuffer.isView(checksum)) {
    return Array.from(new Uint8Array(checksum.buffer, checksum.byteOffset, checksum.byteLength))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');
  }
  return null;
}

export async function handleInternalJobs(
  request: Request,
  env: Env,
  _ctx: ExecutionContext
): Promise<Response> {
  // 1. Fail closed if server secret is missing
  if (!env.A23_NODE_SECRET || !env.A23_NODE_SECRET.trim()) {
    return new Response(JSON.stringify({ error: 'Internal node authentication not configured' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // 2. Validate Bearer token
  if (!verifyInternalAuth(request, env.A23_NODE_SECRET)) {
    return new Response(JSON.stringify({ error: 'Unauthorized' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const url = new URL(request.url);
  const path = url.pathname;

  // Match /internal/jobs/:job_id/:action
  const match = path.match(/^\/internal\/jobs\/([a-zA-Z0-9_-]+)\/(claim|heartbeat|fail|artifact|complete|rearm|rescue|recover)$/);
  if (!match) {
    return new Response(JSON.stringify({ error: 'Not Found' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const jobId = match[1];
  const action = match[2];

  const jobService = new JobService(env.DB, resolveMaxJobAgeMs(env.MAX_JOB_AGE_MS));
  const defaultLeaseSeconds = env.A23_JOB_LEASE_SECONDS
    ? Math.max(10, Math.min(parseInt(env.A23_JOB_LEASE_SECONDS, 10) || 300, 1800))
    : 300;

  // Route: PUT /internal/jobs/:job_id/artifact (Private R2 upload)
  if (action === 'artifact') {
    if (request.method !== 'PUT') {
      return new Response(JSON.stringify({ error: 'Method Not Allowed' }), {
        status: 405,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (!env.ARTIFACTS_BUCKET) {
      return new Response(JSON.stringify({ error: 'Artifact storage not configured' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    // 1. Validate headers
    const workerId = request.headers.get('X-Worker-Id')?.trim();
    const leaseToken = request.headers.get('X-Lease-Token')?.trim();
    const rawSha256 = request.headers.get('X-Artifact-SHA256')?.trim();
    const contentType = request.headers.get('Content-Type')?.trim().toLowerCase();
    const contentLengthStr = request.headers.get('Content-Length')?.trim();

    if (!workerId || !/^[a-zA-Z0-9_-]{1,64}$/.test(workerId)) {
      return new Response(JSON.stringify({ error: 'Valid X-Worker-Id header required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (!leaseToken || !/^[0-9a-fA-F-]{36}$/.test(leaseToken)) {
      return new Response(JSON.stringify({ error: 'Valid X-Lease-Token header required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (!rawSha256 || !/^[0-9a-fA-F]{64}$/.test(rawSha256)) {
      return new Response(JSON.stringify({ error: 'Valid X-Artifact-SHA256 header (64-hex) required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (contentType !== 'application/zip') {
      return new Response(JSON.stringify({ error: 'Content-Type must be application/zip' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (!contentLengthStr || !/^\d+$/.test(contentLengthStr)) {
      return new Response(JSON.stringify({ error: 'Exact Content-Length header required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const contentLength = parseInt(contentLengthStr, 10);
    if (contentLength <= 0 || contentLength > 50 * 1024 * 1024) {
      return new Response(JSON.stringify({ error: 'Content-Length must be between 1 byte and 50 MiB' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (!request.body) {
      return new Response(JSON.stringify({ error: 'Request body required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    // 2. Validate active unexpired lease with safety margin before accepting upload
    const leaseValidation = await jobService.validateLeaseForArtifactUpload(jobId, workerId, leaseToken);
    if (!leaseValidation.valid || !leaseValidation.orderId) {
      return new Response(
        JSON.stringify({ error: 'Lease expired, superseded, or near deadline', queue_action: 'ack', reason: leaseValidation.reason }),
        {
          status: 409,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    const orderId = leaseValidation.orderId;
    const sha256Hex = rawSha256.toLowerCase();
    const objectKey = buildArtifactStorageKey(orderId, jobId, sha256Hex);

    // Check if object already exists in R2 (idempotent duplicate upload)
    try {
      const existing = await env.ARTIFACTS_BUCKET.head(objectKey);
      if (existing) {
        const matchSize = existing.size === contentLength;
        const matchSha = existing.customMetadata?.sha256 === sha256Hex;
        const matchJob = existing.customMetadata?.job_id === jobId;
        const matchOrder = existing.customMetadata?.order_id === orderId;
        const storedChecksum = checksumToHex(existing.checksums?.sha256);
        const matchChecksum = storedChecksum !== null && storedChecksum === sha256Hex;

        if (matchSize && matchSha && matchJob && matchOrder && matchChecksum) {
          return new Response(
            JSON.stringify({
              success: true,
              artifact_key: objectKey,
              sha256: sha256Hex,
              size: existing.size,
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        } else {
          return new Response(
            JSON.stringify({ error: 'Duplicate artifact upload metadata or checksum mismatch', queue_action: 'ack' }),
            {
              status: 409,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }
      }
    } catch {
      // Proceed with upload
    }

    // 3. Stream request.body directly to R2 bucket with sha256 integrity verification
    try {
      const shaBuffer = hexToArrayBuffer(sha256Hex);
      const r2Obj = await env.ARTIFACTS_BUCKET.put(objectKey, request.body, {
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

      if (!r2Obj || r2Obj.size !== contentLength) {
        return new Response(
          JSON.stringify({ error: 'Uploaded object size mismatch in R2' }),
          { status: 500, headers: { 'Content-Type': 'application/json' } }
        );
      }

      const putChecksum = checksumToHex(r2Obj.checksums?.sha256);
      if (putChecksum === null || putChecksum !== sha256Hex) {
        return new Response(
          JSON.stringify({ error: 'Uploaded object checksum missing or mismatch in R2' }),
          { status: 500, headers: { 'Content-Type': 'application/json' } }
        );
      }

      return new Response(
        JSON.stringify({
          success: true,
          artifact_key: objectKey,
          sha256: sha256Hex,
          size: contentLength,
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    } catch (err: unknown) {
      return new Response(
        JSON.stringify({ error: 'R2 upload failed or checksum mismatch', reason: err instanceof Error ? err.message : 'r2_put_failed' }),
        {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }
  }

  // Route: POST /internal/jobs/:job_id/complete (Fenced D1 completion)
  if (action === 'complete') {
    if (request.method !== 'POST') {
      return new Response(JSON.stringify({ error: 'Method Not Allowed' }), {
        status: 405,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    let body: Record<string, unknown>;
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      return new Response(JSON.stringify({ error: 'Invalid JSON payload' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      return new Response(JSON.stringify({ error: 'Invalid JSON body object' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const workerId = typeof body.worker_id === 'string' ? body.worker_id.trim() : '';
    const leaseToken = typeof body.lease_token === 'string' ? body.lease_token.trim() : '';
    const artifactKey = typeof body.artifact_key === 'string' ? body.artifact_key.trim() : '';
    const sha256 = typeof body.sha256 === 'string' ? body.sha256.trim().toLowerCase() : '';
    const size = typeof body.size === 'number' ? body.size : 0;

    if (!workerId || !/^[a-zA-Z0-9_-]{1,64}$/.test(workerId)) {
      return new Response(JSON.stringify({ error: 'Valid worker_id is required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (!leaseToken || !/^[0-9a-fA-F-]{36}$/.test(leaseToken)) {
      return new Response(JSON.stringify({ error: 'Valid lease_token is required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (!sha256 || !/^[0-9a-f]{64}$/.test(sha256)) {
      return new Response(JSON.stringify({ error: 'Valid sha256 (64-hex) is required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (!size || size <= 0) {
      return new Response(JSON.stringify({ error: 'Valid size is required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const job = await jobService.getJobById(jobId);
    if (!job) {
      return new Response(JSON.stringify({ error: 'Job not found', queue_action: 'ack' }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    // Verify artifactKey matches the authoritative derived key
    const expectedKey = buildArtifactStorageKey(job.order_id, jobId, sha256);
    if (artifactKey !== expectedKey) {
      return new Response(JSON.stringify({ error: 'Invalid artifact_key path' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    // Verify object exists in R2 and matches metadata before D1 completion
    if (!env.ARTIFACTS_BUCKET) {
      return new Response(JSON.stringify({ error: 'Artifact storage not configured' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const r2Head = await env.ARTIFACTS_BUCKET.head(artifactKey);
    if (!r2Head) {
      return new Response(JSON.stringify({ error: 'Artifact not found in R2 bucket' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (r2Head.size !== size) {
      return new Response(JSON.stringify({ error: 'Artifact size mismatch in R2' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (r2Head.customMetadata?.sha256 !== sha256 || r2Head.customMetadata?.job_id !== jobId || r2Head.customMetadata?.order_id !== job.order_id) {
      return new Response(JSON.stringify({ error: 'Artifact metadata mismatch in R2' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const storedChecksum = checksumToHex(r2Head.checksums?.sha256);
    if (storedChecksum === null || storedChecksum !== sha256) {
      return new Response(JSON.stringify({ error: 'Artifact stored checksum missing or mismatch in R2' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    let partsMeta: import('../services/job-service').ArtifactPartMeta[] | undefined;
    if (Array.isArray(body.parts) && body.parts.length > 0) {
      partsMeta = [];
      for (const p of body.parts as Array<Record<string, unknown>>) {
        const pIndex = typeof p.part_index === 'number' ? p.part_index : 0;
        const pTotal = typeof p.total_parts === 'number' ? p.total_parts : 0;
        const pName = typeof p.filename === 'string' ? p.filename.trim() : '';
        const pKey = typeof p.artifact_key === 'string' ? p.artifact_key.trim() : '';
        const pSha = typeof p.artifact_sha256 === 'string' ? p.artifact_sha256.trim().toLowerCase() : '';
        const pSize = typeof p.artifact_size_bytes === 'number' ? p.artifact_size_bytes : 0;

        if (pIndex < 1 || pTotal < 1 || !pName || !pKey || !/^[0-9a-f]{64}$/.test(pSha) || pSize <= 0) {
          return new Response(JSON.stringify({ error: 'Invalid part metadata in parts array' }), {
            status: 400,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        const expectedPartKey = buildArtifactStorageKey(job.order_id, jobId, pSha);
        if (pKey !== expectedPartKey) {
          return new Response(JSON.stringify({ error: `Invalid artifact_key path for part ${pIndex}` }), {
            status: 400,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        const r2PartHead = await env.ARTIFACTS_BUCKET.head(pKey);
        if (!r2PartHead || r2PartHead.size !== pSize) {
          return new Response(JSON.stringify({ error: `Artifact part ${pIndex} not found or size mismatch in R2` }), {
            status: 400,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        partsMeta.push({
          part_index: pIndex,
          total_parts: pTotal,
          filename: pName,
          artifact_key: pKey,
          artifact_size_bytes: pSize,
          artifact_sha256: pSha,
        });
      }
    }

    // Atomic D1 completion
    const completeResult = await jobService.completeJob({
      jobId,
      workerId,
      leaseToken,
      artifactKey,
      artifactSha256: sha256,
      artifactSizeBytes: size,
      parts: partsMeta,
    });

    if (completeResult.status === 'COMPLETED' || completeResult.status === 'ALREADY_COMPLETED') {
      emitStructuredLog({
        event: 'job_completed',
        job_id: jobId,
        order_id: job.order_id,
        artifact_key: completeResult.artifact_key || artifactKey,
        size_bytes: size,
      });

      return new Response(
        JSON.stringify({
          success: true,
          status: 'COMPLETED',
          queue_action: 'ack',
          completed_at: completeResult.completed_at,
          artifact_key: completeResult.artifact_key,
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    if (completeResult.status === 'CONFLICT_DIFFERENT_ARTIFACT') {
      return new Response(
        JSON.stringify({
          error: 'Conflict: job already completed with different artifact',
          status: 'CONFLICT',
          queue_action: 'ack',
          reason: completeResult.reason,
        }),
        {
          status: 409,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    return new Response(
      JSON.stringify({
        error: 'Lease expired or fenced',
        status: completeResult.status,
        queue_action: completeResult.queue_action,
        reason: completeResult.reason,
      }),
      {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // All other POST actions: claim, heartbeat, fail
  if (request.method !== 'POST') {
    return new Response(JSON.stringify({ error: 'Method Not Allowed' }), {
      status: 405,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return new Response(JSON.stringify({ error: 'Invalid JSON payload' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return new Response(JSON.stringify({ error: 'Invalid JSON body object' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // Route: POST /internal/jobs/:job_id/rearm (authenticated stale-Queue recovery CAS)
  if (action === 'rearm') {
    if (!/^[a-zA-Z0-9_-]{1,64}$/.test(jobId)) {
      return new Response(JSON.stringify({ error: 'Valid job_id is required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const expectedKeys = [
      'order_id',
      'outbox_id',
      'lease_owner',
      'lease_expires_at',
      'attempt_count',
      'dispatch_attempts',
    ];
    const bodyKeys = Object.keys(body);
    if (
      bodyKeys.length !== expectedKeys.length ||
      expectedKeys.some((key) => !Object.prototype.hasOwnProperty.call(body, key))
    ) {
      return new Response(JSON.stringify({ error: 'Exact rearm request fields required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const orderId = typeof body.order_id === 'string' ? body.order_id.trim() : '';
    const outboxId = typeof body.outbox_id === 'string' ? body.outbox_id.trim() : '';
    const leaseOwner = typeof body.lease_owner === 'string' ? body.lease_owner.trim() : '';
    const leaseExpiresAt = body.lease_expires_at;
    const attemptCount = body.attempt_count;
    const dispatchAttempts = body.dispatch_attempts;

    if (
      !/^[a-zA-Z0-9_-]{1,64}$/.test(orderId) ||
      !/^[a-zA-Z0-9_-]{1,64}$/.test(outboxId) ||
      !/^[a-zA-Z0-9_-]{1,64}$/.test(leaseOwner) ||
      typeof leaseExpiresAt !== 'number' ||
      !Number.isSafeInteger(leaseExpiresAt) ||
      leaseExpiresAt < 0 ||
      typeof attemptCount !== 'number' ||
      !Number.isInteger(attemptCount) ||
      attemptCount < 0 ||
      attemptCount > 1000 ||
      typeof dispatchAttempts !== 'number' ||
      !Number.isInteger(dispatchAttempts) ||
      dispatchAttempts < 0 ||
      dispatchAttempts > 1_000_000
    ) {
      return new Response(JSON.stringify({ error: 'Invalid rearm request bounds' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    try {
      const result = await jobService.rearmExpiredQueueEvent({
        jobId,
        orderId,
        outboxId,
        leaseOwner,
        leaseExpiresAt,
        attemptCount,
        dispatchAttempts,
      });

      if (result.status === 'REARMED' || result.status === 'ALREADY_REARMED') {
        return new Response(JSON.stringify({ success: true, status: result.status }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }

      return new Response(JSON.stringify({ error: 'Rearm preconditions not met', status: 'CONFLICT' }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      });
    } catch {
      return new Response(JSON.stringify({ error: 'Rearm failed' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      });
    }
  }

  // Route: POST /internal/jobs/:job_id/rescue (exact exhausted-job recovery CAS)
  if (action === 'rescue') {
    if (!/^[a-zA-Z0-9_-]{1,64}$/.test(jobId)) {
      return new Response(JSON.stringify({ error: 'Valid job_id is required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const expectedKeys = [
      'order_id',
      'outbox_id',
      'payment_id',
      'payment_transaction_id',
      'payment_code',
      'lease_owner',
      'lease_expires_at',
      'attempt_count',
      'max_attempts',
      'last_error',
      'dispatch_attempts',
    ];
    const bodyKeys = Object.keys(body);
    if (
      bodyKeys.length !== expectedKeys.length ||
      expectedKeys.some((key) => !Object.prototype.hasOwnProperty.call(body, key))
    ) {
      return new Response(JSON.stringify({ error: 'Exact rescue request fields required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const orderId = typeof body.order_id === 'string' ? body.order_id.trim() : '';
    const outboxId = typeof body.outbox_id === 'string' ? body.outbox_id.trim() : '';
    const paymentId = typeof body.payment_id === 'string' ? body.payment_id.trim() : '';
    const paymentTransactionId = typeof body.payment_transaction_id === 'string'
      ? body.payment_transaction_id.trim()
      : '';
    const paymentCode = typeof body.payment_code === 'string' ? body.payment_code.trim() : '';
    const leaseOwner = typeof body.lease_owner === 'string' ? body.lease_owner.trim() : '';
    const leaseExpiresAt = body.lease_expires_at;
    const attemptCount = body.attempt_count;
    const maxAttempts = body.max_attempts;
    const lastError = typeof body.last_error === 'string' ? body.last_error.trim() : '';
    const dispatchAttempts = body.dispatch_attempts;

    if (
      !/^[a-zA-Z0-9_-]{1,64}$/.test(orderId) ||
      !/^[a-zA-Z0-9_-]{1,64}$/.test(outboxId) ||
      !/^[a-zA-Z0-9_-]{1,64}$/.test(paymentId) ||
      !/^[a-zA-Z0-9_.:-]{1,128}$/.test(paymentTransactionId) ||
      !/^[a-zA-Z0-9_-]{1,64}$/.test(paymentCode) ||
      !/^[a-zA-Z0-9_-]{1,64}$/.test(leaseOwner) ||
      typeof leaseExpiresAt !== 'number' ||
      !Number.isSafeInteger(leaseExpiresAt) ||
      leaseExpiresAt < 0 ||
      typeof attemptCount !== 'number' ||
      !Number.isInteger(attemptCount) ||
      attemptCount < 0 ||
      attemptCount > 1000 ||
      typeof maxAttempts !== 'number' ||
      !Number.isInteger(maxAttempts) ||
      maxAttempts < 1 ||
      maxAttempts >= 1000 ||
      !/^[a-zA-Z0-9_]{1,64}$/.test(lastError) ||
      typeof dispatchAttempts !== 'number' ||
      !Number.isInteger(dispatchAttempts) ||
      dispatchAttempts < 0 ||
      dispatchAttempts > 1_000_000
    ) {
      return new Response(JSON.stringify({ error: 'Invalid rescue request bounds' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    try {
      const result = await jobService.rescueExactExhaustedJob({
        jobId,
        orderId,
        outboxId,
        paymentId,
        paymentTransactionId,
        paymentCode,
        leaseOwner,
        leaseExpiresAt,
        attemptCount,
        maxAttempts,
        lastError,
        dispatchAttempts,
      });

      if (result.status === 'RESCUED') {
        return new Response(JSON.stringify({ success: true, status: result.status }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }

      return new Response(JSON.stringify({ error: 'Rescue preconditions not met', status: 'CONFLICT' }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      });
    } catch {
      return new Response(JSON.stringify({ error: 'Rescue failed' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      });
    }
  }

  // Route: POST /internal/jobs/:job_id/recover (terminal FAILED-job recovery CAS)
  if (action === 'recover') {
    if (!/^[a-zA-Z0-9_-]{1,64}$/.test(jobId)) {
      return new Response(JSON.stringify({ error: 'Valid job_id is required', status: 'INVALID_REQUEST', http_status: 400 }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const expectedKeys = [
      'order_id',
      'outbox_id',
      'payment_id',
      'payment_transaction_id',
      'payment_code',
      'attempt_count',
      'max_attempts',
      'last_error',
      'dispatch_attempts',
    ];
    const bodyKeys = Object.keys(body);
    if (
      bodyKeys.length !== expectedKeys.length ||
      expectedKeys.some((key) => !Object.prototype.hasOwnProperty.call(body, key))
    ) {
      return new Response(JSON.stringify({ error: 'Exact recover request fields required', status: 'INVALID_REQUEST', http_status: 400 }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const orderId = typeof body.order_id === 'string' ? body.order_id.trim() : '';
    const outboxId = typeof body.outbox_id === 'string' ? body.outbox_id.trim() : '';
    const paymentId = typeof body.payment_id === 'string' ? body.payment_id.trim() : '';
    const paymentTransactionId = typeof body.payment_transaction_id === 'string'
      ? body.payment_transaction_id.trim()
      : '';
    const paymentCode = typeof body.payment_code === 'string' ? body.payment_code.trim() : '';
    const attemptCount = body.attempt_count;
    const maxAttempts = body.max_attempts;
    const lastError = typeof body.last_error === 'string' ? body.last_error.trim() : '';
    const dispatchAttempts = body.dispatch_attempts;

    if (
      !/^[a-zA-Z0-9_-]{1,64}$/.test(orderId) ||
      !/^[a-zA-Z0-9_-]{1,64}$/.test(outboxId) ||
      !/^[a-zA-Z0-9_-]{1,64}$/.test(paymentId) ||
      !/^[a-zA-Z0-9_.:-]{1,128}$/.test(paymentTransactionId) ||
      !/^[a-zA-Z0-9_-]{1,64}$/.test(paymentCode) ||
      typeof attemptCount !== 'number' ||
      !Number.isInteger(attemptCount) ||
      attemptCount < 0 ||
      attemptCount > 1000 ||
      typeof maxAttempts !== 'number' ||
      !Number.isInteger(maxAttempts) ||
      maxAttempts < 1 ||
      maxAttempts >= 1000 ||
      !/^[a-zA-Z0-9_]{1,64}$/.test(lastError) ||
      typeof dispatchAttempts !== 'number' ||
      !Number.isInteger(dispatchAttempts) ||
      dispatchAttempts < 0 ||
      dispatchAttempts > 1_000_000
    ) {
      return new Response(JSON.stringify({ error: 'Invalid recover request bounds', status: 'INVALID_REQUEST', http_status: 400 }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    try {
      const result = await jobService.recoverTerminalFailedJob({
        jobId,
        orderId,
        outboxId,
        paymentId,
        paymentTransactionId,
        paymentCode,
        attemptCount,
        maxAttempts,
        lastError,
        dispatchAttempts,
      });

      if (result.status === 'RECOVERED' || result.status === 'ALREADY_RECOVERED') {
        return new Response(JSON.stringify({ success: true, status: result.status }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }

      return new Response(JSON.stringify({ error: 'Recover preconditions not met', status: 'CONFLICT', http_status: 409 }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      });
    } catch (error) {
      emitStructuredLog({
        event: 'recover_error',
        job_id: jobId,
        http_status: 500,
        reason: error instanceof Error ? error.message.slice(0, 256) : 'unknown',
      });
      return new Response(JSON.stringify({ error: 'Recover failed', status: 'ERROR', http_status: 500 }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      });
    }
  }

  // Route: POST /internal/jobs/:job_id/claim
  if (action === 'claim') {
    if (
      typeof body.worker_id !== 'string' ||
      !/^[a-zA-Z0-9_-]{1,64}$/.test(body.worker_id.trim())
    ) {
      return new Response(JSON.stringify({ error: 'Valid worker_id is required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const workerId = body.worker_id.trim();
    let leaseDuration = defaultLeaseSeconds;
    if (body.lease_seconds !== undefined) {
      if (
        typeof body.lease_seconds !== 'number' ||
        !Number.isInteger(body.lease_seconds) ||
        body.lease_seconds < 10 ||
        body.lease_seconds > 1800
      ) {
        return new Response(
          JSON.stringify({ error: 'lease_seconds must be an integer between 10 and 1800' }),
          {
            status: 400,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }
      leaseDuration = body.lease_seconds;
    }

    const result = await jobService.claimJob(jobId, workerId, leaseDuration);

    if (result.status === 'CLAIMED' && result.payload) {
      emitStructuredLog({
        event: 'job_claimed',
        job_id: jobId,
        worker_id: workerId,
        lease_duration_sec: leaseDuration,
      });

      return new Response(JSON.stringify(result.payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (result.status === 'NOT_FOUND') {
      return new Response(JSON.stringify({ error: 'Job not found', queue_action: 'ack' }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (result.status === 'TERMINAL') {
      return new Response(
        JSON.stringify({ status: 'TERMINAL', queue_action: 'ack', reason: result.reason }),
        {
          status: 409,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    if (result.status === 'MALFORMED_METADATA') {
      return new Response(
        JSON.stringify({ error: 'Malformed metadata in order', queue_action: 'retry' }),
        {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    return new Response(
      JSON.stringify({
        status: result.status,
        queue_action: result.queue_action || 'retry',
        reason: result.reason,
      }),
      {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // Route: POST /internal/jobs/:job_id/heartbeat
  if (action === 'heartbeat') {
    if (
      typeof body.worker_id !== 'string' ||
      !/^[a-zA-Z0-9_-]{1,64}$/.test(body.worker_id.trim())
    ) {
      return new Response(JSON.stringify({ error: 'Valid worker_id is required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (
      typeof body.lease_token !== 'string' ||
      !/^[0-9a-fA-F-]{36}$/.test(body.lease_token.trim())
    ) {
      return new Response(JSON.stringify({ error: 'Valid lease_token is required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const workerId = body.worker_id.trim();
    const leaseToken = body.lease_token.trim();

    let extendSeconds = defaultLeaseSeconds;
    if (body.extend_seconds !== undefined) {
      if (
        typeof body.extend_seconds !== 'number' ||
        !Number.isInteger(body.extend_seconds) ||
        body.extend_seconds < 10 ||
        body.extend_seconds > 1800
      ) {
        return new Response(
          JSON.stringify({ error: 'extend_seconds must be an integer between 10 and 1800' }),
          {
            status: 400,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }
      extendSeconds = body.extend_seconds;
    }

    const result = await jobService.heartbeat(jobId, workerId, leaseToken, extendSeconds);

    if (result.status === 'EXTENDED') {
      emitStructuredLog({
        event: 'job_heartbeat',
        job_id: jobId,
        worker_id: workerId,
        lease_expires_at: result.lease_expires_at || 0,
      });

      return new Response(
        JSON.stringify({ success: true, lease_expires_at: result.lease_expires_at }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    return new Response(
      JSON.stringify({ error: 'Lease expired, superseded, or invalid', queue_action: 'ack' }),
      {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // Route: POST /internal/jobs/:job_id/fail
  if (action === 'fail') {
    if (
      typeof body.worker_id !== 'string' ||
      !/^[a-zA-Z0-9_-]{1,64}$/.test(body.worker_id.trim())
    ) {
      return new Response(JSON.stringify({ error: 'Valid worker_id is required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (
      typeof body.lease_token !== 'string' ||
      !/^[0-9a-fA-F-]{36}$/.test(body.lease_token.trim())
    ) {
      return new Response(JSON.stringify({ error: 'Valid lease_token is required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (typeof body.retryable !== 'boolean') {
      return new Response(JSON.stringify({ error: 'retryable must be a boolean' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    let reasonCode: string | undefined;
    if (body.reason_code !== undefined) {
      if (
        typeof body.reason_code !== 'string' ||
        !/^[A-Z0-9_]{1,64}$/.test(body.reason_code.trim())
      ) {
        return new Response(
          JSON.stringify({
            error: 'reason_code must be an uppercase alphanumeric code (e.g. EXTRACTION_FAILED)',
          }),
          {
            status: 400,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }
      reasonCode = body.reason_code.trim();
    }

    const workerId = body.worker_id.trim();
    const leaseToken = body.lease_token.trim();
    const retryable = body.retryable;

    const result = await jobService.failJob({
      jobId,
      workerId,
      leaseToken,
      retryable,
      reasonCode,
    });

    if (result.status === 'RETRY') {
      return new Response(
        JSON.stringify({
          success: true,
          status: 'RETRY',
          queue_action: 'retry',
          delay_seconds: result.delay_seconds,
          next_retry_at: result.next_retry_at,
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    if (result.status === 'FAILED') {
      return new Response(
        JSON.stringify({
          success: true,
          status: 'FAILED',
          queue_action: 'ack',
          reason: result.reason,
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    return new Response(
      JSON.stringify({
        error: 'Lease expired or superseded',
        queue_action: 'ack',
        reason: result.reason,
      }),
      {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  return new Response(JSON.stringify({ error: 'Not Found' }), {
    status: 404,
    headers: { 'Content-Type': 'application/json' },
  });
}

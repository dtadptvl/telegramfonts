import type { Env } from '../env';
import { JobService } from '../services/job-service';

export function verifyInternalAuth(request: Request, secret: string | undefined): boolean {
  if (!secret || !secret.trim()) return false;
  const authHeader = request.headers.get('Authorization');
  if (!authHeader || !authHeader.startsWith('Bearer ')) return false;

  const token = authHeader.slice(7).trim();
  if (!token) return false;

  const enc = new TextEncoder();
  const tokenBytes = enc.encode(token);
  const secretBytes = enc.encode(secret.trim());

  if (tokenBytes.byteLength !== secretBytes.byteLength) return false;
  return crypto.subtle.timingSafeEqual(tokenBytes, secretBytes);
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
  const path = url.pathname; // e.g. /internal/jobs/:job_id/claim

  // Match /internal/jobs/:job_id/:action
  const match = path.match(/^\/internal\/jobs\/([a-zA-Z0-9_-]+)\/(claim|heartbeat|fail)$/);
  if (!match || request.method !== 'POST') {
    return new Response(JSON.stringify({ error: 'Not Found' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const jobId = match[1];
  const action = match[2];

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

  const jobService = new JobService(env.DB);
  const defaultLeaseSeconds = env.A23_JOB_LEASE_SECONDS
    ? Math.max(10, Math.min(parseInt(env.A23_JOB_LEASE_SECONDS, 10) || 300, 1800))
    : 300;

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

    if (result.status === 'CLAIMED') {
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

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

  const jobService = new JobService(env.DB);
  const defaultLeaseSeconds = env.A23_JOB_LEASE_SECONDS
    ? parseInt(env.A23_JOB_LEASE_SECONDS, 10) || 300
    : 300;

  // Route: POST /internal/jobs/:job_id/claim
  if (action === 'claim') {
    const workerId = typeof body.worker_id === 'string' ? body.worker_id.trim() : '';
    if (!workerId) {
      return new Response(JSON.stringify({ error: 'worker_id is required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const leaseDuration =
      typeof body.lease_seconds === 'number' && body.lease_seconds > 0
        ? body.lease_seconds
        : defaultLeaseSeconds;

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
      JSON.stringify({ status: result.status, queue_action: result.queue_action || 'retry', reason: result.reason }),
      {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // Route: POST /internal/jobs/:job_id/heartbeat
  if (action === 'heartbeat') {
    const workerId = typeof body.worker_id === 'string' ? body.worker_id.trim() : '';
    const leaseToken = typeof body.lease_token === 'string' ? body.lease_token.trim() : '';

    if (!workerId || !leaseToken) {
      return new Response(JSON.stringify({ error: 'worker_id and lease_token are required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const extendSeconds =
      typeof body.extend_seconds === 'number' && body.extend_seconds > 0
        ? body.extend_seconds
        : defaultLeaseSeconds;

    const result = await jobService.heartbeat(jobId, workerId, leaseToken, extendSeconds);

    if (result.status === 'EXTENDED') {
      return new Response(JSON.stringify({ success: true, lease_expires_at: result.lease_expires_at }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
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
    const workerId = typeof body.worker_id === 'string' ? body.worker_id.trim() : '';
    const leaseToken = typeof body.lease_token === 'string' ? body.lease_token.trim() : '';
    const retryable = typeof body.retryable === 'boolean' ? body.retryable : true;
    const reasonCode = typeof body.reason_code === 'string' ? body.reason_code.trim() : undefined;

    if (!workerId || !leaseToken) {
      return new Response(JSON.stringify({ error: 'worker_id and lease_token are required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

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
      JSON.stringify({ error: 'Lease expired or superseded', queue_action: 'ack', reason: result.reason }),
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

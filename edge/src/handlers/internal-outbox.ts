import type { Env } from '../env';
import { OutboxService } from '../services/outbox-service';
import { verifyInternalAuth } from './internal-jobs';

export async function handleInternalOutbox(
  request: Request,
  env: Env,
  _ctx: ExecutionContext
): Promise<Response> {
  try {
    if (!env.A23_NODE_SECRET || !env.A23_NODE_SECRET.trim()) {
      return new Response(JSON.stringify({ error: 'Internal node authentication not configured' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (!verifyInternalAuth(request, env.A23_NODE_SECRET)) {
      return new Response(JSON.stringify({ error: 'Unauthorized' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (request.method !== 'POST') {
      return new Response(JSON.stringify({ error: 'Method Not Allowed' }), {
        status: 405,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (!env.DB) {
      return new Response(JSON.stringify({ error: 'Database not configured' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    let batchSize = 20;
    try {
      const body = (await request.json()) as { batchSize?: number };
      if (body && typeof body.batchSize === 'number') {
        batchSize = body.batchSize;
      }
    } catch {
      // Body is optional
    }

    const outboxService = new OutboxService(env.DB, env.FULFILLMENT_QUEUE, env);
    const result = await outboxService.dispatchPendingEvents({ batchSize });

    return new Response(JSON.stringify({ status: 'ok', result }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (err: any) {
    return new Response(
      JSON.stringify({ error: 'Internal Server Error', message: String(err?.message || err), stack: String(err?.stack || '') }),
      {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }
}

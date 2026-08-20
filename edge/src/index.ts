import type { Env } from './env';

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    if (request.method === 'GET' && url.pathname === '/health') {
      return new Response(JSON.stringify({ status: 'ok' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (request.method === 'GET' && url.pathname === '/ready') {
      try {
        if (!env.DB) {
          return new Response(
            JSON.stringify({ status: 'error', reason: 'D1 binding DB is not configured' }),
            {
              status: 503,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        const result = await env.DB.prepare('SELECT 1 as healthy').first<{ healthy: number }>();
        if (result && result.healthy === 1) {
          return new Response(JSON.stringify({ status: 'ready', database: 'connected' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        return new Response(
          JSON.stringify({ status: 'error', reason: 'Unexpected database response' }),
          {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : 'Unknown database error';
        return new Response(JSON.stringify({ status: 'error', reason: message }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    }

    return new Response(JSON.stringify({ error: 'Not Found' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    });
  },
};
export type { Env };

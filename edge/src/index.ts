import type { Env } from './env';
import { handleTelegramWebhook } from './handlers/telegram-webhook';

const REQUIRED_TABLES_COUNT = 12;
const SCHEMA_CHECK_QUERY = `SELECT count(*) as count FROM sqlite_master WHERE type='table' AND name IN (
  'orders', 'order_items', 'payments', 'fulfillment_jobs', 'outbox_events', 'artifacts',
  'telegram_users', 'telegram_updates', 'catalogs', 'catalog_styles', 'catalog_requests', 'telegram_sessions'
)`;

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
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
          return new Response(JSON.stringify({ status: 'unavailable' }), {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        const result = await env.DB.prepare(SCHEMA_CHECK_QUERY).first<{ count: number }>();
        if (result && result.count === REQUIRED_TABLES_COUNT) {
          return new Response(JSON.stringify({ status: 'ready', database: 'connected' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        return new Response(JSON.stringify({ status: 'unavailable' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        });
      } catch {
        return new Response(JSON.stringify({ status: 'unavailable' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    }

    if (request.method === 'POST' && url.pathname === '/webhooks/telegram') {
      return handleTelegramWebhook(request, env, ctx);
    }

    return new Response(JSON.stringify({ error: 'Not Found' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    });
  },
};
export type { Env };

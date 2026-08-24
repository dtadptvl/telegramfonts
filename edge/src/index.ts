import type { Env } from './env';
import { handleTelegramWebhook } from './handlers/telegram-webhook';
import { handleSePayWebhook } from './handlers/sepay-webhook';
import { handleInternalJobs } from './handlers/internal-jobs';
import { handleInternalCatalog } from './handlers/internal-catalog';
import { handleInternalOutbox } from './handlers/internal-outbox';
import { handleDownload } from './handlers/downloads';
import { OutboxService } from './services/outbox-service';
import { JobService } from './services/job-service';

const REQUIRED_TABLES_COUNT = 13;
const SCHEMA_CHECK_QUERY = `
  SELECT
    (SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN (
      'orders', 'order_items', 'payments', 'fulfillment_jobs', 'outbox_events', 'artifacts',
      'telegram_users', 'telegram_updates', 'catalogs', 'catalog_styles', 'catalog_requests',
      'telegram_sessions', 'fulfillment_receipts'
    )) as table_count,
    (SELECT count(*) FROM pragma_table_info('outbox_events') WHERE name = 'dispatch_lease_token') as has_outbox_lease,
    (SELECT count(*) FROM pragma_table_info('fulfillment_jobs') WHERE name = 'lease_token') as has_job_lease,
    (SELECT count(*) FROM pragma_table_info('fulfillment_jobs') WHERE name = 'artifact_key') as has_artifact_key,
    (SELECT count(*) FROM pragma_table_info('orders') WHERE name = 'payment_code') as has_payment_code,
    (SELECT count(*) FROM pragma_table_info('orders') WHERE name = 'completed_at') as has_order_completed_at
`;

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    try {
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

          const result = await env.DB.prepare(SCHEMA_CHECK_QUERY).first<{
            table_count: number;
            has_outbox_lease: number;
            has_job_lease: number;
            has_artifact_key: number;
            has_payment_code: number;
            has_order_completed_at: number;
          }>();

          if (
            result &&
            result.table_count === REQUIRED_TABLES_COUNT &&
            result.has_outbox_lease === 1 &&
            result.has_job_lease === 1 &&
            result.has_artifact_key === 1 &&
            result.has_payment_code === 1 &&
            result.has_order_completed_at === 1
          ) {
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

      if (request.method === 'POST' && url.pathname === '/webhooks/sepay') {
        return handleSePayWebhook(request, env, ctx);
      }

      if (url.pathname.startsWith('/internal/jobs/')) {
        return handleInternalJobs(request, env, ctx);
      }

      if (url.pathname.startsWith('/internal/catalog-requests')) {
        return handleInternalCatalog(request, env, ctx);
      }

      if (url.pathname.startsWith('/internal/outbox')) {
        return handleInternalOutbox(request, env, ctx);
      }

      if (url.pathname.startsWith('/downloads/')) {
        return handleDownload(request, env, ctx);
      }

      return new Response(JSON.stringify({ error: 'Not Found' }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      });
    } catch (err: any) {
      return new Response(JSON.stringify({ error: err.message, stack: err.stack }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      });
    }
  },

  async scheduled(_event: ScheduledEvent, env: Env, _ctx: ExecutionContext): Promise<void> {
    if (!env.DB) return;
    const jobService = new JobService(env.DB);
    await jobService.finalizeExpiredExhaustedJobs();
    const outboxService = new OutboxService(env.DB, env.FULFILLMENT_QUEUE, env);
    await outboxService.dispatchPendingEvents({ batchSize: 20 });
  },
};

export type { Env };

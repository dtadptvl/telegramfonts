import type { Env } from '../env';
import { CatalogService } from '../services/catalog-service';
import { SessionService } from '../services/session-service';
import { TelegramClient } from '../services/telegram-client';
import { renderStyleSelection } from './telegram-webhook';
import { verifyInternalAuth } from './internal-jobs';
import { emitStructuredLog } from '../utils/logger';
import type { FontCatalog } from '../types/catalog';

export async function handleInternalCatalog(
  request: Request,
  env: Env,
  _ctx: ExecutionContext
): Promise<Response> {
  // 1. Validate internal node auth
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

  if (!env.DB) {
    return new Response(JSON.stringify({ error: 'Database not configured' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const url = new URL(request.url);
  const path = url.pathname;

  const catalogService = new CatalogService(env.DB);
  const sessionService = new SessionService(env.DB);

  // Route: GET /internal/catalog-requests/pending
  if (request.method === 'GET' && path === '/internal/catalog-requests/pending') {
    const pendingRequests = await env.DB
      .prepare(
        `SELECT id, user_id, canonical_key, source_url, status, created_at
         FROM catalog_requests
         WHERE status = 'PENDING'
         ORDER BY created_at ASC
         LIMIT 10`
      )
      .all<{
        id: string;
        user_id: string;
        canonical_key: string;
        source_url: string;
        status: string;
        created_at: number;
      }>();

    return new Response(
      JSON.stringify({
        requests: pendingRequests.results || [],
      }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // Route: POST /internal/catalog-requests/:id/complete OR /internal/catalog-requests/complete
  const completeMatch = path.match(/^\/internal\/catalog-requests\/(?:([a-zA-Z0-9_-]+)\/)?complete$/);
  if (request.method === 'POST' && completeMatch) {
    let body: Record<string, unknown>;
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      return new Response(JSON.stringify({ error: 'Invalid JSON payload' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const canonicalKey = typeof body.canonical_key === 'string' ? body.canonical_key.trim() : '';
    const sourceUrl = typeof body.source_url === 'string' ? body.source_url.trim() : '';
    const familyName = typeof body.family_name === 'string' ? body.family_name.trim() : '';
    const foundry = typeof body.foundry === 'string' ? body.foundry.trim() : undefined;
    const rawStyles = Array.isArray(body.styles) ? body.styles : [];

    if (!canonicalKey || !sourceUrl || !familyName || rawStyles.length === 0) {
      return new Response(
        JSON.stringify({
          error: 'Missing required catalog fields: canonical_key, source_url, family_name, styles',
        }),
        {
          status: 400,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    const styles = rawStyles.map((s: Record<string, unknown>) => ({
      id: String(s.id || '').trim(),
      displayName: String(s.display_name || s.displayName || s.id || '').trim(),
      price: typeof s.price === 'number' ? s.price : 50000,
    })).filter((s) => s.id.length > 0);

    if (styles.length === 0) {
      return new Response(JSON.stringify({ error: 'At least one valid style is required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const catalog: FontCatalog = {
      canonicalKey,
      sourceUrl,
      familyName,
      foundry,
      styles,
    };

    // 1. Persist catalog to D1 (atomically updates catalogs, catalog_styles, and marks catalog_requests as COMPLETED)
    const catalogId = await catalogService.persistCatalogResult(catalog);

    // 2. Find any user sessions waiting for this catalog and advance them to SELECTING_STYLES
    const waitingSessions = await env.DB
      .prepare(
        `SELECT user_id, chat_id, last_message_id, workflow_token, version
         FROM telegram_sessions
         WHERE status = 'AWAITING_CATALOG'`
      )
      .all<{
        user_id: string;
        chat_id: number;
        last_message_id: number | null;
        workflow_token: string;
        version: number;
      }>();

    if (waitingSessions.results && waitingSessions.results.length > 0 && env.TELEGRAM_BOT_TOKEN) {
      const tg = new TelegramClient(env.TELEGRAM_BOT_TOKEN);

      for (const sess of waitingSessions.results) {
        // Update session to SELECTING_STYLES
        await sessionService.updateSessionCatalog(
          sess.user_id,
          catalogId,
          'SELECTING_STYLES'
        );

        // Render and send the interactive style selection message to user
        const { text: msgText, replyMarkup } = renderStyleSelection(
          catalog,
          [],
          sess.workflow_token
        );

        try {
          const sent = await tg.sendMessage({
            chat_id: sess.chat_id,
            text: msgText,
            reply_markup: replyMarkup,
          });

          if (sent.message_id) {
            await sessionService.setStatusUnconditional(sess.user_id, 'SELECTING_STYLES', sent.message_id);
          }
        } catch {
          // Log or tolerate telegram transport hiccups
        }
      }
    }

    return new Response(
      JSON.stringify({
        success: true,
        catalog_id: catalogId,
      }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  return new Response(JSON.stringify({ error: 'Not Found' }), {
    status: 404,
    headers: { 'Content-Type': 'application/json' },
  });
}

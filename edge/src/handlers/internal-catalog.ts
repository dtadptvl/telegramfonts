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

  // Route: POST /internal/catalog-requests/:id/complete
  const completeMatch = path.match(/^\/internal\/catalog-requests\/([a-zA-Z0-9_-]+)\/complete$/);
  if (request.method === 'POST' && completeMatch) {
    const requestId = completeMatch[1];

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

    // 1. Fetch the stored catalog request row
    const reqRow = await env.DB
      .prepare('SELECT id, user_id, canonical_key, source_url, status, catalog_id FROM catalog_requests WHERE id = ?')
      .bind(requestId)
      .first<{
        id: string;
        user_id: string;
        canonical_key: string;
        source_url: string;
        status: string;
        catalog_id: string | null;
      }>();

    if (!reqRow) {
      return new Response(JSON.stringify({ error: 'Catalog request not found' }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    // Idempotency: if already completed, return existing catalog_id without re-mutating
    if (reqRow.status === 'COMPLETED' && reqRow.catalog_id) {
      return new Response(
        JSON.stringify({
          success: true,
          catalog_id: reqRow.catalog_id,
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    // Verify identity against stored request
    if (reqRow.canonical_key !== canonicalKey || reqRow.source_url !== sourceUrl) {
      return new Response(
        JSON.stringify({
          error: 'Payload canonical_key or source_url does not match stored request',
        }),
        {
          status: 400,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    const DEFAULT_STYLE_PRICE_VND = 5000;
    const styles = rawStyles
      .map((s: Record<string, unknown>) => ({
        id: String(s.id || '').trim(),
        displayName: String(s.display_name || s.displayName || s.id || '').trim(),
        price: DEFAULT_STYLE_PRICE_VND,
      }))
      .filter((s) => s.id.length > 0);

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

    // 2. Identify all users waiting on this exact canonical key before persisting
    const pendingMatchingRequests = await env.DB
      .prepare(
        `SELECT id, user_id
         FROM catalog_requests
         WHERE canonical_key = ? AND status = 'PENDING'`
      )
      .bind(canonicalKey)
      .all<{ id: string; user_id: string }>();

    const targetUserIds = new Set<string>();
    targetUserIds.add(reqRow.user_id);
    for (const pendingItem of pendingMatchingRequests.results || []) {
      targetUserIds.add(pendingItem.user_id);
    }

    // 3. Persist catalog to D1 (atomically updates catalogs, catalog_styles, and marks catalog_requests as COMPLETED)
    const catalogId = await catalogService.persistCatalogResult(catalog);

    // 4. Advance every still-relevant waiting session whose request is satisfied by this catalog
    if (env.TELEGRAM_BOT_TOKEN) {
      const tg = new TelegramClient(env.TELEGRAM_BOT_TOKEN);

      for (const targetUserId of targetUserIds) {
        const userSession = await env.DB
          .prepare(
            `SELECT user_id, chat_id, last_message_id, workflow_token, version, status
             FROM telegram_sessions
             WHERE user_id = ? AND status = 'AWAITING_CATALOG'`
          )
          .bind(targetUserId)
          .first<{
            user_id: string;
            chat_id: number;
            last_message_id: number | null;
            workflow_token: string;
            version: number;
            status: string;
          }>();

        if (!userSession) {
          continue;
        }

        // Verify the user is still waiting on this catalog (and did not start a newer different pending request)
        const latestReqForUser = await env.DB
          .prepare(
            `SELECT canonical_key
             FROM catalog_requests
             WHERE user_id = ?
             ORDER BY created_at DESC
             LIMIT 1`
          )
          .bind(targetUserId)
          .first<{ canonical_key: string }>();

        if (latestReqForUser && latestReqForUser.canonical_key !== canonicalKey) {
          // User started a newer request for a different font; do not override with stale resolution!
          continue;
        }

        // Update session to SELECTING_STYLES
        await sessionService.updateSessionCatalog(
          userSession.user_id,
          catalogId,
          'SELECTING_STYLES'
        );

        const updatedSession = await sessionService.getSessionByUserId(userSession.user_id);
        if (!updatedSession) {
          continue;
        }

        // Render and send the interactive style selection message to user with active post-update token
        const { text: msgText, replyMarkup } = renderStyleSelection(
          catalog,
          [],
          updatedSession.workflow_token
        );

        try {
          const sent = await tg.sendMessage({
            chat_id: updatedSession.chat_id,
            text: msgText,
            reply_markup: replyMarkup,
          });

          if (sent.message_id) {
            await sessionService.setStatusUnconditional(updatedSession.user_id, 'SELECTING_STYLES', sent.message_id);
          }
        } catch {
          // Log or tolerate telegram transport hiccups
        }
      }
    }

    emitStructuredLog({
      event: 'catalog_completed',
      request_id: requestId,
      user_id: reqRow.user_id,
      canonical_key: canonicalKey,
      catalog_id: catalogId,
    });

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

  // Route: POST /internal/catalog-requests/:id/fail
  const failMatch = path.match(/^\/internal\/catalog-requests\/([a-zA-Z0-9_-]+)\/fail$/);
  if (request.method === 'POST' && failMatch) {
    const requestId = failMatch[1];

    let body: Record<string, unknown> = {};
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      // Body is optional
    }

    const reason = typeof body.reason === 'string' ? body.reason.trim() : (typeof body.error_code === 'string' ? body.error_code.trim() : 'catalog_acquisition_failed');

    const reqRow = await env.DB
      .prepare('SELECT id, user_id, canonical_key, source_url, status FROM catalog_requests WHERE id = ?')
      .bind(requestId)
      .first<{
        id: string;
        user_id: string;
        canonical_key: string;
        source_url: string;
        status: string;
      }>();

    if (!reqRow) {
      return new Response(JSON.stringify({ error: 'Catalog request not found' }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (reqRow.status === 'FAILED' || reqRow.status === 'COMPLETED') {
      return new Response(
        JSON.stringify({
          success: true,
          status: reqRow.status,
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    await catalogService.failCatalogRequest(requestId);

    // Notify waiting Telegram user and unblock session if this is still the active request
    if (env.TELEGRAM_BOT_TOKEN) {
      const tg = new TelegramClient(env.TELEGRAM_BOT_TOKEN);

      const userSession = await env.DB
        .prepare(
          `SELECT user_id, chat_id, last_message_id, status
           FROM telegram_sessions
           WHERE user_id = ? AND status = 'AWAITING_CATALOG'`
        )
        .bind(reqRow.user_id)
        .first<{
          user_id: string;
          chat_id: number;
          last_message_id: number | null;
          status: string;
        }>();

      if (userSession) {
        // Verify the user is still waiting on this catalog (and did not start a newer different pending request)
        const latestReqForUser = await env.DB
          .prepare(
            `SELECT canonical_key
             FROM catalog_requests
             WHERE user_id = ?
             ORDER BY created_at DESC
             LIMIT 1`
          )
          .bind(reqRow.user_id)
          .first<{ canonical_key: string }>();

        if (!latestReqForUser || latestReqForUser.canonical_key === reqRow.canonical_key) {
          // Reset session back to IDLE
          await sessionService.setStatusUnconditional(userSession.user_id, 'IDLE');

          try {
            await tg.sendMessage({
              chat_id: userSession.chat_id,
              text: '⚠️ Không thể tải thông tin font từ liên kết này. Vui lòng kiểm tra lại liên kết MyFonts hợp lệ hoặc thử lại sau.',
            });
          } catch {
            // Log or tolerate telegram transport hiccups
          }
        }
      }
    }

    emitStructuredLog({
      event: 'catalog_failed',
      request_id: requestId,
      user_id: reqRow.user_id,
      canonical_key: reqRow.canonical_key,
      reason,
    });

    return new Response(
      JSON.stringify({
        success: true,
        status: 'FAILED',
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

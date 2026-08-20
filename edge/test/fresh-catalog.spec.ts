import { describe, it, expect, vi, beforeEach } from 'vitest';
import { env } from 'cloudflare:test';
import worker from '../src/index';

describe('Phase 7: Fresh-Catalog E2E Resolution & Scheduled Cron Delivery', () => {
  const nodeSecret = 'test_internal_node_secret_32bytes_12345';
  const botToken = '123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11';

  beforeEach(() => {
    (env as unknown as Record<string, unknown>).A23_NODE_SECRET = nodeSecret;
    (env as unknown as Record<string, unknown>).TELEGRAM_BOT_TOKEN = botToken;
    (env as unknown as Record<string, unknown>).TELEGRAM_WEBHOOK_SECRET = 'secret_webhook_token_hex';
    (env as unknown as Record<string, unknown>).BASE_URL = 'https://telefont.example.com';
    (env as unknown as Record<string, unknown>).DOWNLOAD_SIGNING_SECRET = 'test_signing_secret_32bytes_123456';
  });

  it('unseen MyFonts URL creates pending catalog request, resolved by A23 without manual seeding', async () => {
    const userId = 8812345;
    const chatId = 8812345;
    const updateId = 998801;

    // 1. Mock fetch for Telegram outbound API
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (info) => {
      const urlStr = typeof info === 'string' ? info : info instanceof Request ? info.url : info.toString();
      if (urlStr.includes('/sendMessage')) {
        return new Response(
          JSON.stringify({
            ok: true,
            result: { message_id: 5544, chat: { id: chatId }, text: 'Mocked Telegram message' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });

    try {
      // 2. User sends fresh, unseen MyFonts URL to Telegram webhook
      const webhookPayload = {
        update_id: updateId,
        message: {
          message_id: 101,
          from: { id: userId, is_bot: false, first_name: 'TestUser' },
          chat: { id: chatId, type: 'private' },
          date: Math.floor(Date.now() / 1000),
          text: 'https://www.myfonts.com/collections/brand-new-font-foundry-abc',
        },
      };

      const webhookReq = new Request('https://worker.local/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': 'secret_webhook_token_hex',
        },
        body: JSON.stringify(webhookPayload),
      });

      const webhookResp = await worker.fetch(webhookReq, env, {} as ExecutionContext);
      expect(webhookResp.status).toBe(200);

      // 3. Verify session is in AWAITING_CATALOG and catalog_requests has a PENDING record
      const pendingReq = await env.DB
        .prepare("SELECT * FROM catalog_requests WHERE user_id = ? AND status = 'PENDING'")
        .bind(String(userId))
        .first<{ id: string; canonical_key: string; source_url: string }>();

      expect(pendingReq).toBeDefined();
      expect(pendingReq?.canonical_key).toContain('brand-new-font');

      // 4. A23 compute worker queries GET /internal/catalog-requests/pending
      const getPendingReq = new Request('https://worker.local/internal/catalog-requests/pending', {
        method: 'GET',
        headers: {
          Authorization: `Bearer ${nodeSecret}`,
        },
      });
      const pendingResp = await worker.fetch(getPendingReq, env, {} as ExecutionContext);
      expect(pendingResp.status).toBe(200);
      const pendingData = (await pendingResp.json()) as { requests: Array<{ id: string; canonical_key: string }> };
      expect(pendingData.requests.length).toBeGreaterThan(0);
      expect(pendingData.requests[0].id).toBe(pendingReq?.id);

      // 5. A23 acquires metadata and posts completion to POST /internal/catalog-requests/:id/complete
      const completePayload = {
        canonical_key: pendingReq?.canonical_key,
        source_url: pendingReq?.source_url,
        family_name: 'Brand New Font',
        foundry: 'Foundry ABC',
        styles: [
          { id: 'regular', display_name: 'Regular', price: 50000 },
          { id: 'bold', display_name: 'Bold', price: 50000 },
          { id: 'italic', display_name: 'Italic', price: 50000 },
        ],
      };

      const postCompleteReq = new Request(
        `https://worker.local/internal/catalog-requests/${pendingReq?.id}/complete`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${nodeSecret}`,
          },
          body: JSON.stringify(completePayload),
        }
      );
      const completeResp = await worker.fetch(postCompleteReq, env, {} as ExecutionContext);
      expect(completeResp.status).toBe(200);

      // 6. Verify D1 state transitions:
      // - Catalog exists
      const catalogRecord = await env.DB
        .prepare('SELECT * FROM catalogs WHERE canonical_key = ?')
        .bind(pendingReq?.canonical_key)
        .first<{ id: string; family_name: string }>();
      expect(catalogRecord).toBeDefined();
      expect(catalogRecord?.family_name).toBe('Brand New Font');

      // - Catalog styles populated
      const stylesRecords = await env.DB
        .prepare('SELECT * FROM catalog_styles WHERE catalog_id = ?')
        .bind(catalogRecord?.id)
        .all<{ id: string }>();
      expect(stylesRecords.results?.length).toBe(3);

      // - Catalog request is marked COMPLETED
      const updatedReq = await env.DB
        .prepare('SELECT * FROM catalog_requests WHERE id = ?')
        .bind(pendingReq?.id)
        .first<{ status: string; catalog_id: string }>();
      expect(updatedReq?.status).toBe('COMPLETED');
      expect(updatedReq?.catalog_id).toBe(catalogRecord?.id);

      // - User session advanced to SELECTING_STYLES with message sent
      const updatedSession = await env.DB
        .prepare('SELECT * FROM telegram_sessions WHERE user_id = ?')
        .bind(String(userId))
        .first<{ status: string; catalog_id: string }>();
      expect(updatedSession?.status).toBe('SELECTING_STYLES');
      expect(updatedSession?.catalog_id).toBe(catalogRecord?.id);
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it('scheduled cron trigger automatically dispatches pending DELIVERY_READY outbox events to Telegram', async () => {
    const orderId = `ord_cron_${crypto.randomUUID().replace(/-/g, '')}`;
    const userId = '771122';
    const now = Date.now();

    // 1. Seed Telegram user and completed order in D1
    await env.DB.prepare(
      `INSERT INTO telegram_users (id, first_name, username, created_at, updated_at)
       VALUES (?, 'CronUser', 'cronuser', ?, ?)`
    )
      .bind(userId, now, now)
      .run();

    await env.DB.prepare(
      `INSERT INTO orders (id, user_id, status, total_amount, currency, created_at, updated_at)
       VALUES (?, ?, 'COMPLETED', 100000, 'VND', ?, ?)`
    )
      .bind(orderId, userId, now, now)
      .run();

    await env.DB.prepare(
      `INSERT INTO fulfillment_receipts (job_id, order_id, artifact_key, artifact_size_bytes, artifact_sha256, completed_at, created_at)
       VALUES (?, ?, ?, 1024, 'abc123', ?, ?)`
    )
      .bind(`job_${orderId}`, orderId, `artifacts/${orderId}/bundle.zip`, now, now)
      .run();

    // 2. Insert pending DELIVERY_READY outbox event
    const outboxId = `outbox_cron_${crypto.randomUUID().replace(/-/g, '')}`;
    await env.DB.prepare(
      `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, dispatch_attempts, created_at)
       VALUES (?, 'DELIVERY_READY', 'ORDER', ?, ?, 'PENDING', 0, ?)`
    )
      .bind(outboxId, orderId, JSON.stringify({ order_id: orderId }), now)
      .run();

    // 3. Mock Telegram sendMessage
    let sentToChat: number | null = null;
    let sentText: string = '';
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (info, init) => {
      const urlStr = typeof info === 'string' ? info : info instanceof Request ? info.url : info.toString();
      if (urlStr.includes('/sendMessage')) {
        const bodyObj = JSON.parse(String(init?.body || '{}')) as { chat_id: number; text: string };
        sentToChat = bodyObj.chat_id;
        sentText = bodyObj.text;
        return new Response(JSON.stringify({ ok: true, result: { message_id: 9988 } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });

    try {
      // 4. Trigger scheduled cron event
      await worker.scheduled({} as ScheduledEvent, env, {} as ExecutionContext);

      // 5. Verify outbox event is marked SENT and message was delivered to Telegram
      const outboxRecord = await env.DB
        .prepare('SELECT status, last_dispatch_error FROM outbox_events WHERE id = ?')
        .bind(outboxId)
        .first<{ status: string; last_dispatch_error: string | null }>();

      expect(outboxRecord?.status).toBe('SENT');
      expect(outboxRecord?.last_dispatch_error).toBeNull();
      expect(String(sentToChat)).toBe('771122');
      expect(sentText).toContain('Your fonts are ready!');
    } finally {
      fetchSpy.mockRestore();
    }
  });
});

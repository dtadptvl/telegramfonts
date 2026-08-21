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

  it('two simultaneous users waiting on different catalogs: completing A changes only A, completing B changes only B', async () => {
    const userA = 'user_aaa_111';
    const chatA = 111111;
    const userB = 'user_bbb_222';
    const chatB = 222222;
    const now = Date.now();

    // 1. Create User A & User B records
    await env.DB.prepare(
      `INSERT INTO telegram_users (id, first_name, username, created_at, updated_at)
       VALUES (?, 'UserA', 'usera', ?, ?), (?, 'UserB', 'userb', ?, ?)`
    )
      .bind(userA, now, now, userB, now, now)
      .run();

    // 2. Create AWAITING_CATALOG sessions
    await env.DB.prepare(
      `INSERT INTO telegram_sessions (user_id, chat_id, status, workflow_token, checkout_token, version, created_at, updated_at)
       VALUES (?, ?, 'AWAITING_CATALOG', 'tok_a', 'chk_a', 1, ?, ?),
              (?, ?, 'AWAITING_CATALOG', 'tok_b', 'chk_b', 1, ?, ?)`
    )
      .bind(userA, chatA, now, now, userB, chatB, now, now)
      .run();

    // 3. Create distinct pending catalog requests for User A and User B
    const reqAId = 'req_user_a_helvetica';
    const reqBId = 'req_user_b_futura';

    await env.DB.prepare(
      `INSERT INTO catalog_requests (id, user_id, canonical_key, source_url, status, created_at, updated_at)
       VALUES (?, ?, 'myfonts:collections/helvetica-now', 'https://www.myfonts.com/collections/helvetica-now', 'PENDING', ?, ?),
              (?, ?, 'myfonts:collections/futura-now', 'https://www.myfonts.com/collections/futura-now', 'PENDING', ?, ?)`
    )
      .bind(reqAId, userA, now, now, reqBId, userB, now, now)
      .run();

    const sentMessages: Array<{ chat_id: number; text: string }> = [];
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (info, init) => {
      const urlStr = typeof info === 'string' ? info : info instanceof Request ? info.url : info.toString();
      if (urlStr.includes('/sendMessage')) {
        const bodyObj = JSON.parse(String(init?.body || '{}')) as { chat_id: number; text: string };
        sentMessages.push(bodyObj);
        return new Response(JSON.stringify({ ok: true, result: { message_id: 8888 } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });

    try {
      // 4. Complete Request A only
      const completeAReq = new Request(
        `https://worker.local/internal/catalog-requests/${reqAId}/complete`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${nodeSecret}`,
          },
          body: JSON.stringify({
            canonical_key: 'myfonts:collections/helvetica-now',
            source_url: 'https://www.myfonts.com/collections/helvetica-now',
            family_name: 'Helvetica Now',
            styles: [{ id: 'regular', display_name: 'Regular', price: 50000 }],
          }),
        }
      );
      const respA = await worker.fetch(completeAReq, env, {} as ExecutionContext);
      expect(respA.status).toBe(200);

      // Verify Session A is SELECTING_STYLES, but Session B is still AWAITING_CATALOG
      const sessionAAfterFirst = await env.DB
        .prepare('SELECT status, catalog_id FROM telegram_sessions WHERE user_id = ?')
        .bind(userA)
        .first<{ status: string; catalog_id: string }>();
      const sessionBAfterFirst = await env.DB
        .prepare('SELECT status, catalog_id FROM telegram_sessions WHERE user_id = ?')
        .bind(userB)
        .first<{ status: string; catalog_id: string | null }>();

      expect(sessionAAfterFirst?.status).toBe('SELECTING_STYLES');
      expect(sessionAAfterFirst?.catalog_id).toBeDefined();
      expect(sessionBAfterFirst?.status).toBe('AWAITING_CATALOG');
      expect(sessionBAfterFirst?.catalog_id).toBeNull();
      expect(sentMessages.length).toBe(1);
      expect(Number(sentMessages[0].chat_id)).toBe(chatA);

      // 5. Complete Request B
      const completeBReq = new Request(
        `https://worker.local/internal/catalog-requests/${reqBId}/complete`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${nodeSecret}`,
          },
          body: JSON.stringify({
            canonical_key: 'myfonts:collections/futura-now',
            source_url: 'https://www.myfonts.com/collections/futura-now',
            family_name: 'Futura Now',
            styles: [{ id: 'bold', display_name: 'Bold', price: 50000 }],
          }),
        }
      );
      const respB = await worker.fetch(completeBReq, env, {} as ExecutionContext);
      expect(respB.status).toBe(200);

      // Verify Session B is now SELECTING_STYLES with its own distinct catalog
      const sessionBAfterSecond = await env.DB
        .prepare('SELECT status, catalog_id FROM telegram_sessions WHERE user_id = ?')
        .bind(userB)
        .first<{ status: string; catalog_id: string }>();

      expect(sessionBAfterSecond?.status).toBe('SELECTING_STYLES');
      expect(sessionBAfterSecond?.catalog_id).not.toBe(sessionAAfterFirst?.catalog_id);
      expect(sentMessages.length).toBe(2);
      expect(Number(sentMessages[1].chat_id)).toBe(chatB);
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it('mismatched/unknown request id or canonical/source payload is rejected with no catalog/session mutation', async () => {
    const userId = 'user_mismatch_333';
    const chatId = 333333;
    const now = Date.now();
    const reqId = 'req_valid_333';

    await env.DB.prepare(
      `INSERT INTO telegram_users (id, first_name, username, created_at, updated_at)
       VALUES (?, 'MismatchUser', 'mismatch', ?, ?)`
    )
      .bind(userId, now, now)
      .run();

    await env.DB.prepare(
      `INSERT INTO telegram_sessions (user_id, chat_id, status, workflow_token, checkout_token, version, created_at, updated_at)
       VALUES (?, ?, 'AWAITING_CATALOG', 'tok_m', 'chk_m', 1, ?, ?)`
    )
      .bind(userId, chatId, now, now)
      .run();

    await env.DB.prepare(
      `INSERT INTO catalog_requests (id, user_id, canonical_key, source_url, status, created_at, updated_at)
       VALUES (?, ?, 'myfonts:collections/real-font', 'https://www.myfonts.com/collections/real-font', 'PENDING', ?, ?)`
    )
      .bind(reqId, userId, now, now)
      .run();

    // 1. Unknown request id returns 404
    const unknownReq = new Request(
      'https://worker.local/internal/catalog-requests/unknown_request_id_999/complete',
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${nodeSecret}`,
        },
        body: JSON.stringify({
          canonical_key: 'myfonts:collections/real-font',
          source_url: 'https://www.myfonts.com/collections/real-font',
          family_name: 'Real Font',
          styles: [{ id: 'regular', display_name: 'Regular', price: 50000 }],
        }),
      }
    );
    const unknownResp = await worker.fetch(unknownReq, env, {} as ExecutionContext);
    expect(unknownResp.status).toBe(404);

    // 2. Mismatched canonical_key / source_url returns 400
    const mismatchReq = new Request(
      `https://worker.local/internal/catalog-requests/${reqId}/complete`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${nodeSecret}`,
        },
        body: JSON.stringify({
          canonical_key: 'myfonts:collections/evil-spoofed-font',
          source_url: 'https://www.myfonts.com/collections/evil-spoofed-font',
          family_name: 'Evil Font',
          styles: [{ id: 'regular', display_name: 'Regular', price: 50000 }],
        }),
      }
    );
    const mismatchResp = await worker.fetch(mismatchReq, env, {} as ExecutionContext);
    expect(mismatchResp.status).toBe(400);

    // 3. Verify database was NOT mutated
    const session = await env.DB
      .prepare('SELECT status, catalog_id FROM telegram_sessions WHERE user_id = ?')
      .bind(userId)
      .first<{ status: string; catalog_id: string | null }>();
    expect(session?.status).toBe('AWAITING_CATALOG');
    expect(session?.catalog_id).toBeNull();

    const requestRecord = await env.DB
      .prepare('SELECT status FROM catalog_requests WHERE id = ?')
      .bind(reqId)
      .first<{ status: string }>();
    expect(requestRecord?.status).toBe('PENDING');
  });

  it('two users request the same unseen catalog concurrently: one catalog resolution completes both applicable requests/sessions with the same catalog; neither is stranded', async () => {
    const user1 = 'concurrent_user_1';
    const chat1 = 100001;
    const user2 = 'concurrent_user_2';
    const chat2 = 100002;
    const now = Date.now();
    const canonicalKey = 'myfonts:collections/shared-popular-font';
    const sourceUrl = 'https://www.myfonts.com/collections/shared-popular-font';

    // 1. Create two user accounts
    await env.DB.prepare(
      `INSERT INTO telegram_users (id, first_name, username, created_at, updated_at)
       VALUES (?, 'User1', 'u1', ?, ?), (?, 'User2', 'u2', ?, ?)`
    )
      .bind(user1, now, now, user2, now, now)
      .run();

    // 2. Both users send the same unseen URL -> both sessions are in AWAITING_CATALOG
    await env.DB.prepare(
      `INSERT INTO telegram_sessions (user_id, chat_id, status, workflow_token, checkout_token, version, created_at, updated_at)
       VALUES (?, ?, 'AWAITING_CATALOG', 'tok_u1', 'chk_u1', 1, ?, ?),
              (?, ?, 'AWAITING_CATALOG', 'tok_u2', 'chk_u2', 1, ?, ?)`
    )
      .bind(user1, chat1, now, now, user2, chat2, now, now)
      .run();

    // 3. Both users have pending catalog_requests for the SAME canonical_key
    const req1Id = 'req_shared_user1';
    const req2Id = 'req_shared_user2';
    await env.DB.prepare(
      `INSERT INTO catalog_requests (id, user_id, canonical_key, source_url, status, created_at, updated_at)
       VALUES (?, ?, ?, ?, 'PENDING', ?, ?),
              (?, ?, ?, ?, 'PENDING', ?, ?)`
    )
      .bind(req1Id, user1, canonicalKey, sourceUrl, now, now, req2Id, user2, canonicalKey, sourceUrl, now + 1, now + 1)
      .run();

    const deliveredChats: number[] = [];
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (info, init) => {
      const urlStr = typeof info === 'string' ? info : info instanceof Request ? info.url : info.toString();
      if (urlStr.includes('/sendMessage')) {
        const bodyObj = JSON.parse(String(init?.body || '{}')) as { chat_id: number };
        deliveredChats.push(Number(bodyObj.chat_id));
        return new Response(JSON.stringify({ ok: true, result: { message_id: 7777 } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });

    try {
      // 4. A23 resolves and completes Request 1
      const completeReq = new Request(
        `https://worker.local/internal/catalog-requests/${req1Id}/complete`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${nodeSecret}`,
          },
          body: JSON.stringify({
            canonical_key: canonicalKey,
            source_url: sourceUrl,
            family_name: 'Shared Popular Font',
            foundry: 'Top Foundry',
            styles: [{ id: 'regular', display_name: 'Regular', price: 50000 }],
          }),
        }
      );
      const resp = await worker.fetch(completeReq, env, {} as ExecutionContext);
      expect(resp.status).toBe(200);

      // 5. Verify BOTH User 1 and User 2 transitioned out of AWAITING_CATALOG to SELECTING_STYLES
      const sess1 = await env.DB
        .prepare('SELECT status, catalog_id FROM telegram_sessions WHERE user_id = ?')
        .bind(user1)
        .first<{ status: string; catalog_id: string }>();
      const sess2 = await env.DB
        .prepare('SELECT status, catalog_id FROM telegram_sessions WHERE user_id = ?')
        .bind(user2)
        .first<{ status: string; catalog_id: string }>();

      expect(sess1?.status).toBe('SELECTING_STYLES');
      expect(sess2?.status).toBe('SELECTING_STYLES');
      expect(sess1?.catalog_id).toBe(sess2?.catalog_id);

      // Verify BOTH requests are marked COMPLETED
      const req1 = await env.DB.prepare('SELECT status, catalog_id FROM catalog_requests WHERE id = ?').bind(req1Id).first<{ status: string; catalog_id: string }>();
      const req2 = await env.DB.prepare('SELECT status, catalog_id FROM catalog_requests WHERE id = ?').bind(req2Id).first<{ status: string; catalog_id: string }>();
      expect(req1?.status).toBe('COMPLETED');
      expect(req2?.status).toBe('COMPLETED');
      expect(req1?.catalog_id).toBe(req2?.catalog_id);

      // Verify BOTH users received interactive Telegram style keyboards
      expect(deliveredChats).toContain(chat1);
      expect(deliveredChats).toContain(chat2);
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it('a stale older request for a user does not override a newer different pending request', async () => {
    const userId = 'user_multi_req_99';
    const chatId = 999901;
    const now = Date.now();

    const oldKey = 'myfonts:collections/old-abandoned-font';
    const oldUrl = 'https://www.myfonts.com/collections/old-abandoned-font';
    const newKey = 'myfonts:collections/new-wanted-font';
    const newUrl = 'https://www.myfonts.com/collections/new-wanted-font';

    // 1. Create user and session
    await env.DB.prepare(
      `INSERT INTO telegram_users (id, first_name, username, created_at, updated_at)
       VALUES (?, 'MultiUser', 'multi', ?, ?)`
    )
      .bind(userId, now, now)
      .run();

    await env.DB.prepare(
      `INSERT INTO telegram_sessions (user_id, chat_id, status, workflow_token, checkout_token, version, created_at, updated_at)
       VALUES (?, ?, 'AWAITING_CATALOG', 'tok_new', 'chk_new', 2, ?, ?)`
    )
      .bind(userId, chatId, now, now)
      .run();

    // 2. Insert older request (created at now - 1000) and newer request (created at now)
    const oldReqId = 'req_old_abandoned';
    const newReqId = 'req_new_wanted';

    await env.DB.prepare(
      `INSERT INTO catalog_requests (id, user_id, canonical_key, source_url, status, created_at, updated_at)
       VALUES (?, ?, ?, ?, 'PENDING', ?, ?),
              (?, ?, ?, ?, 'PENDING', ?, ?)`
    )
      .bind(oldReqId, userId, oldKey, oldUrl, now - 1000, now - 1000, newReqId, userId, newKey, newUrl, now, now)
      .run();

    const deliveredChats: number[] = [];
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (info, init) => {
      const urlStr = typeof info === 'string' ? info : info instanceof Request ? info.url : info.toString();
      if (urlStr.includes('/sendMessage')) {
        const bodyObj = JSON.parse(String(init?.body || '{}')) as { chat_id: number };
        deliveredChats.push(Number(bodyObj.chat_id));
        return new Response(JSON.stringify({ ok: true, result: { message_id: 5555 } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });

    try {
      // 3. Late arriving completion for the OLD request arrives
      const completeOldReq = new Request(
        `https://worker.local/internal/catalog-requests/${oldReqId}/complete`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${nodeSecret}`,
          },
          body: JSON.stringify({
            canonical_key: oldKey,
            source_url: oldUrl,
            family_name: 'Old Abandoned Font',
            styles: [{ id: 'regular', display_name: 'Regular', price: 50000 }],
          }),
        }
      );
      const oldResp = await worker.fetch(completeOldReq, env, {} as ExecutionContext);
      expect(oldResp.status).toBe(200);

      // Verify session did NOT switch to the old catalog (remains AWAITING_CATALOG for the newer request)
      const currentSession = await env.DB
        .prepare('SELECT status, catalog_id FROM telegram_sessions WHERE user_id = ?')
        .bind(userId)
        .first<{ status: string; catalog_id: string | null }>();

      expect(currentSession?.status).toBe('AWAITING_CATALOG');
      expect(currentSession?.catalog_id).toBeNull();
      expect(deliveredChats.length).toBe(0);
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it('external JSON-LD price such as 45 does not create a 45 VND order; order pricing remains the app-defined VND amount', async () => {
    const userId = 'user_pricing_test';
    const chatId = 665544;
    const now = Date.now();
    const reqId = 'req_pricing_45';
    const canonicalKey = 'myfonts:collections/external-usd-priced-font';
    const sourceUrl = 'https://www.myfonts.com/collections/external-usd-priced-font';

    await env.DB.prepare(
      `INSERT INTO telegram_users (id, first_name, username, created_at, updated_at)
       VALUES (?, 'PriceUser', 'priceuser', ?, ?)`
    )
      .bind(userId, now, now)
      .run();

    await env.DB.prepare(
      `INSERT INTO telegram_sessions (user_id, chat_id, status, workflow_token, checkout_token, version, created_at, updated_at)
       VALUES (?, ?, 'AWAITING_CATALOG', 'tok_p', 'chk_p', 1, ?, ?)`
    )
      .bind(userId, chatId, now, now)
      .run();

    await env.DB.prepare(
      `INSERT INTO catalog_requests (id, user_id, canonical_key, source_url, status, created_at, updated_at)
       VALUES (?, ?, ?, ?, 'PENDING', ?, ?)`
    )
      .bind(reqId, userId, canonicalKey, sourceUrl, now, now)
      .run();

    // 1. Post completion where provider styles contain a raw dollar price (e.g. 45 or 0)
    const completeReq = new Request(
      `https://worker.local/internal/catalog-requests/${reqId}/complete`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${nodeSecret}`,
        },
        body: JSON.stringify({
          canonical_key: canonicalKey,
          source_url: sourceUrl,
          family_name: 'External USD Priced Font',
          styles: [
            { id: 'regular', display_name: 'Regular', price: 45 },
            { id: 'bold', display_name: 'Bold', price: 0 },
          ],
        }),
      }
    );
    const resp = await worker.fetch(completeReq, env, {} as ExecutionContext);
    expect(resp.status).toBe(200);

    // 2. Verify styles stored in D1 are normalized to authoritative VND pricing (5,000 VND)
    const savedStyles = await env.DB
      .prepare(
        `SELECT style_id, display_name, price
         FROM catalog_styles
         WHERE catalog_id = (SELECT id FROM catalogs WHERE canonical_key = ?)`
      )
      .bind(canonicalKey)
      .all<{ style_id: string; display_name: string; price: number }>();

    expect(savedStyles.results?.length).toBe(2);
    for (const st of savedStyles.results || []) {
      // Must be 5,000 VND, NEVER 45 VND or 0 VND
      expect(st.price).toBe(5000);
    }
  });

  it('calculates order total based on 5,000 VND per selected style: 1 style => 5,000 VND, 4 styles => 20,000 VND', async () => {
    const userId = 'user_order_pricing';
    const chatId = 778899;
    const now = Date.now();
    const catalogId = 'cat_pricing_calc_test';
    const canonicalKey = 'myfonts:collections/pricing-calc-font';

    // 1. Seed user, catalog, and 4 styles
    await env.DB.prepare(
      `INSERT INTO telegram_users (id, first_name, username, created_at, updated_at)
       VALUES (?, 'OrderPriceUser', 'opuser', ?, ?)`
    )
      .bind(userId, now, now)
      .run();

    await env.DB.prepare(
      `INSERT INTO catalogs (id, canonical_key, source_url, family_name, foundry, created_at, updated_at)
       VALUES (?, ?, 'https://www.myfonts.com/collections/pricing-calc-font', 'Pricing Calc Font', 'Foundry X', ?, ?)`
    )
      .bind(catalogId, canonicalKey, now, now)
      .run();

    const styleIds = ['s_light', 's_reg', 's_bold', 's_black'];
    for (const sId of styleIds) {
      await env.DB.prepare(
        `INSERT INTO catalog_styles (id, catalog_id, style_id, display_name, price, created_at)
         VALUES (?, ?, ?, ?, 5000, ?)`
      )
        .bind(`style_${catalogId}_${sId}`, catalogId, sId, sId.toUpperCase(), now)
        .run();
    }

    // 2. Test 1 selected style => 5,000 VND
    const { OrderService } = await import('../src/services/order-service');
    const { CatalogService } = await import('../src/services/catalog-service');
    const orderService = new OrderService(env.DB);
    const catalogService = new CatalogService(env.DB);

    const catalog = await catalogService.getCatalogById(catalogId);
    expect(catalog).toBeDefined();

    // Session for 1 style
    const session1 = {
      id: 'sess_price_1',
      user_id: userId,
      chat_id: String(chatId),
      status: 'CONFIRMING' as const,
      catalog_id: catalogId,
      selected_styles: JSON.stringify(['s_reg']),
      selected_formats: JSON.stringify(['TTF', 'OTF', 'WOFF2']),
      workflow_token: 'w_tok_1',
      checkout_token: 'c_tok_1',
      version: 1,
      last_message_id: null,
      active_order_id: null,
      created_at: now,
      updated_at: now,
    };
    await env.DB.prepare(
      `INSERT INTO telegram_sessions (id, user_id, chat_id, status, catalog_id, selected_styles, selected_formats, workflow_token, checkout_token, version, created_at, updated_at)
       VALUES (?, ?, ?, 'CONFIRMING', ?, ?, ?, 'w_tok_1', 'c_tok_1', 1, ?, ?)`
    )
      .bind('sess_price_1', userId, chatId, catalogId, session1.selected_styles, session1.selected_formats, now, now)
      .run();

    const order1 = await orderService.createOrderFromSession(
      session1,
      catalog!
    );
    expect(order1.totalAmount).toBe(5000);

    // 3. Test 4 selected styles => 20,000 VND
    const userId4 = 'user_order_pricing_4';
    await env.DB.prepare(
      `INSERT INTO telegram_users (id, first_name, username, created_at, updated_at)
       VALUES (?, 'OrderPriceUser4', 'opuser4', ?, ?)`
    )
      .bind(userId4, now, now)
      .run();

    const session4 = {
      id: 'sess_price_4',
      user_id: userId4,
      chat_id: String(chatId),
      status: 'CONFIRMING' as const,
      catalog_id: catalogId,
      selected_styles: JSON.stringify(styleIds),
      selected_formats: JSON.stringify(['TTF', 'OTF', 'WOFF2']),
      workflow_token: 'w_tok_4',
      checkout_token: 'c_tok_4',
      version: 1,
      last_message_id: null,
      active_order_id: null,
      created_at: now,
      updated_at: now,
    };
    await env.DB.prepare(
      `INSERT INTO telegram_sessions (id, user_id, chat_id, status, catalog_id, selected_styles, selected_formats, workflow_token, checkout_token, version, created_at, updated_at)
       VALUES (?, ?, ?, 'CONFIRMING', ?, ?, ?, 'w_tok_4', 'c_tok_4', 1, ?, ?)`
    )
      .bind('sess_price_4', userId4, chatId, catalogId, session4.selected_styles, session4.selected_formats, now, now)
      .run();

    const order4 = await orderService.createOrderFromSession(
      session4,
      catalog!
    );
    expect(order4.totalAmount).toBe(20000);
  });

  it('handles POST /internal/catalog-requests/:id/fail: marks request FAILED, resets session to IDLE, and notifies user', async () => {
    const userId = 'user_fail_test_1';
    const chatId = 888123;
    const reqId = 'req_fail_101';
    const now = Date.now();

    await env.DB.prepare(
      `INSERT INTO telegram_users (id, first_name, username, created_at, updated_at)
       VALUES (?, 'FailTester', 'failuser', ?, ?)`
    )
      .bind(userId, now, now)
      .run();

    await env.DB.prepare(
      `INSERT INTO telegram_sessions (id, user_id, chat_id, status, workflow_token, checkout_token, version, created_at, updated_at)
       VALUES (?, ?, ?, 'AWAITING_CATALOG', 'w_fail_tok', 'c_fail_tok', 1, ?, ?)`
    )
      .bind('sess_fail_1', userId, chatId, now, now)
      .run();

    await env.DB.prepare(
      `INSERT INTO catalog_requests (id, user_id, canonical_key, source_url, status, created_at, updated_at)
       VALUES (?, ?, 'myfonts:collections/unsupported-font', 'https://www.myfonts.com/collections/unsupported-font', 'PENDING', ?, ?)`
    )
      .bind(reqId, userId, now, now)
      .run();

    let sentMessageText = '';
    let sentChatId = 0;
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (info, init) => {
      const urlStr = typeof info === 'string' ? info : info instanceof Request ? info.url : info.toString();
      if (urlStr.includes('/sendMessage')) {
        const bodyObj = JSON.parse(String(init?.body || '{}')) as { chat_id: number; text: string };
        sentChatId = Number(bodyObj.chat_id);
        sentMessageText = bodyObj.text;
        return new Response(JSON.stringify({ ok: true, result: { message_id: 8888 } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });

    try {
      // 1. Unauthorized fail request rejected
      const unauthReq = new Request(
        `https://worker.local/internal/catalog-requests/${reqId}/fail`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason: 'NO_CATALOG_STYLES_FOUND' }),
        }
      );
      const unauthResp = await worker.fetch(unauthReq, env, {} as ExecutionContext);
      expect(unauthResp.status).toBe(401);

      // 2. Authorized fail request processed
      const failReq = new Request(
        `https://worker.local/internal/catalog-requests/${reqId}/fail`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${nodeSecret}`,
          },
          body: JSON.stringify({ reason: 'NO_CATALOG_STYLES_FOUND' }),
        }
      );
      const failResp = await worker.fetch(failReq, env, {} as ExecutionContext);
      expect(failResp.status).toBe(200);
      const failData = (await failResp.json()) as { success: boolean; status: string };
      expect(failData.success).toBe(true);
      expect(failData.status).toBe('FAILED');

      // 3. Verify D1 catalog_requests row transitioned to FAILED
      const reqRow = await env.DB.prepare('SELECT status FROM catalog_requests WHERE id = ?').bind(reqId).first<{ status: string }>();
      expect(reqRow?.status).toBe('FAILED');

      // 4. Verify telegram_sessions row reset to IDLE
      const sessRow = await env.DB.prepare('SELECT status FROM telegram_sessions WHERE user_id = ?').bind(userId).first<{ status: string }>();
      expect(sessRow?.status).toBe('IDLE');

      // 5. Verify user received notification on Telegram
      expect(sentChatId).toBe(chatId);
      expect(sentMessageText).toContain('Không thể tải thông tin font');
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it('a stale older failure does not reset a newer different pending request session', async () => {
    const userId = 'user_stale_fail_99';
    const chatId = 999902;
    const now = Date.now();

    const oldKey = 'myfonts:collections/old-broken-font';
    const oldUrl = 'https://www.myfonts.com/collections/old-broken-font';
    const newKey = 'myfonts:collections/newer-active-font';
    const newUrl = 'https://www.myfonts.com/collections/newer-active-font';

    await env.DB.prepare(
      `INSERT INTO telegram_users (id, first_name, username, created_at, updated_at)
       VALUES (?, 'StaleFailUser', 'sfuser', ?, ?)`
    )
      .bind(userId, now, now)
      .run();

    await env.DB.prepare(
      `INSERT INTO telegram_sessions (id, user_id, chat_id, status, workflow_token, checkout_token, version, created_at, updated_at)
       VALUES (?, ?, ?, 'AWAITING_CATALOG', 'tok_new_f', 'chk_new_f', 2, ?, ?)`
    )
      .bind('sess_stale_f', userId, chatId, now, now)
      .run();

    const oldReqId = 'req_old_broken';
    const newReqId = 'req_newer_active';

    await env.DB.prepare(
      `INSERT INTO catalog_requests (id, user_id, canonical_key, source_url, status, created_at, updated_at)
       VALUES (?, ?, ?, ?, 'PENDING', ?, ?),
              (?, ?, ?, ?, 'PENDING', ?, ?)`
    )
      .bind(oldReqId, userId, oldKey, oldUrl, now - 1000, now - 1000, newReqId, userId, newKey, newUrl, now, now)
      .run();

    const deliveredChats: number[] = [];
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (info, init) => {
      const urlStr = typeof info === 'string' ? info : info instanceof Request ? info.url : info.toString();
      if (urlStr.includes('/sendMessage')) {
        const bodyObj = JSON.parse(String(init?.body || '{}')) as { chat_id: number };
        deliveredChats.push(Number(bodyObj.chat_id));
        return new Response(JSON.stringify({ ok: true, result: { message_id: 5556 } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });

    try {
      // Late arriving failure for the OLD request arrives
      const failOldReq = new Request(
        `https://worker.local/internal/catalog-requests/${oldReqId}/fail`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${nodeSecret}`,
          },
          body: JSON.stringify({ reason: 'NO_CATALOG_STYLES_FOUND' }),
        }
      );
      const oldResp = await worker.fetch(failOldReq, env, {} as ExecutionContext);
      expect(oldResp.status).toBe(200);

      // Verify the old request is marked FAILED
      const oldReqRow = await env.DB.prepare('SELECT status FROM catalog_requests WHERE id = ?').bind(oldReqId).first<{ status: string }>();
      expect(oldReqRow?.status).toBe('FAILED');

      // Verify session did NOT get reset to IDLE because user has a newer active request
      const currentSession = await env.DB
        .prepare('SELECT status FROM telegram_sessions WHERE user_id = ?')
        .bind(userId)
        .first<{ status: string }>();
      expect(currentSession?.status).toBe('AWAITING_CATALOG');

      // Verify no failure message was sent to disrupt the newer ongoing request
      expect(deliveredChats).not.toContain(chatId);
    } finally {
      fetchSpy.mockRestore();
    }
  });
});


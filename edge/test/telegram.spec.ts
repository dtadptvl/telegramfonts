import { describe, it, expect, beforeEach } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker, { type Env } from '../src/index';
import { CatalogService } from '../src/services/catalog-service';
import type { FontCatalog } from '../src/types/catalog';
import type { TelegramUpdate } from '../src/types/telegram';

const BOT_TOKEN = 'mock_bot_token_12345';
const WEBHOOK_SECRET = 'super_secret_webhook_token_987';

const testEnv: Env = {
  ...(env as unknown as Env),
  TELEGRAM_BOT_TOKEN: BOT_TOKEN,
  TELEGRAM_WEBHOOK_SECRET: WEBHOOK_SECRET,
};

const sampleCatalog: FontCatalog = {
  sourceUrl: 'https://www.myfonts.com/collections/helvetica-now-font-monotype-imaging',
  canonicalKey: 'myfonts:collections/helvetica-now-font-monotype-imaging',
  familyName: 'Helvetica Now',
  foundry: 'Monotype Imaging',
  styles: [
    { id: 'hn_regular', displayName: 'Helvetica Now Regular', price: 50000 },
    { id: 'hn_bold', displayName: 'Helvetica Now Bold', price: 50000 },
    { id: 'hn_italic', displayName: 'Helvetica Now Italic', price: 50000 },
  ],
};

describe('Telegram Webhook & UX Flow', () => {
  beforeEach(async () => {
    // Intercept outbound Telegram Bot API requests
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const urlStr = typeof input === 'string' ? input : input.toString();
      if (urlStr.includes('api.telegram.org')) {
        return new Response(JSON.stringify({ ok: true, result: { message_id: 999 } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('Not found', { status: 404 });
    };
  });

  describe('Webhook Security & Update Parsing', () => {
    it('returns 503 when bot token or webhook secret are missing in env', async () => {
      const mockEnv: Env = {
        ...(env as unknown as Env),
        TELEGRAM_BOT_TOKEN: undefined,
        TELEGRAM_WEBHOOK_SECRET: undefined,
      };

      const req = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': WEBHOOK_SECRET,
        },
        body: JSON.stringify({ update_id: 1 }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, mockEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(503);
    });

    it('returns 401 when X-Telegram-Bot-Api-Secret-Token is missing or incorrect', async () => {
      const reqWrong = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': 'wrong-secret',
        },
        body: JSON.stringify({ update_id: 1 }),
      });

      const ctx = createExecutionContext();
      const resWrong = await worker.fetch(reqWrong, testEnv, ctx);
      await waitOnExecutionContext(ctx);
      expect(resWrong.status).toBe(401);

      const reqMissing = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ update_id: 1 }),
      });
      const resMissing = await worker.fetch(reqMissing, testEnv, ctx);
      await waitOnExecutionContext(ctx);
      expect(resMissing.status).toBe(401);
    });

    it('handles malformed / non-json payload gracefully without throwing', async () => {
      const req = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': WEBHOOK_SECRET,
        },
        body: 'invalid-json{{{',
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
      const data = await res.json();
      expect(data).toEqual({ status: 'ignored_invalid_payload' });
    });

    it('handles unsupported updates gracefully (e.g. channel_post)', async () => {
      const req = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': WEBHOOK_SECRET,
        },
        body: JSON.stringify({ update_id: 100, channel_post: { message_id: 5 } }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
    });
  });

  describe('User Onboarding & URL Ingestion', () => {
    it('handles /start by resetting user session in D1', async () => {
      const update: TelegramUpdate = {
        update_id: 1,
        message: {
          message_id: 10,
          from: { id: 11111, is_bot: false, first_name: 'Alice', username: 'alice123' },
          chat: { id: 11111, type: 'private', first_name: 'Alice' },
          date: Date.now(),
          text: '/start',
        },
      };

      const req = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': WEBHOOK_SECRET,
        },
        body: JSON.stringify(update),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);

      const user = await env.DB.prepare('SELECT * FROM telegram_users WHERE id = ?')
        .bind('11111')
        .first<{ id: string; username: string }>();
      expect(user).not.toBeNull();
      expect(user?.username).toBe('alice123');

      const session = await env.DB.prepare('SELECT * FROM telegram_sessions WHERE user_id = ?')
        .bind('11111')
        .first<{ status: string }>();
      expect(session?.status).toBe('IDLE');
    });

    it('creates and deduplicates catalog request when catalog is pending', async () => {
      const update: TelegramUpdate = {
        update_id: 2,
        message: {
          message_id: 11,
          from: { id: 22222, is_bot: false, first_name: 'Bob' },
          chat: { id: 22222, type: 'private', first_name: 'Bob' },
          date: Date.now(),
          text: 'https://www.myfonts.com/collections/futura-font-linotype',
        },
      };

      const req = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': WEBHOOK_SECRET,
        },
        body: JSON.stringify(update),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);

      const request = await env.DB.prepare(
        'SELECT * FROM catalog_requests WHERE user_id = ? AND canonical_key = ?'
      )
        .bind('22222', 'myfonts:collections/futura-font-linotype')
        .first<{ status: string }>();
      expect(request).not.toBeNull();
      expect(request?.status).toBe('PENDING');

      const session = await env.DB.prepare('SELECT * FROM telegram_sessions WHERE user_id = ?')
        .bind('22222')
        .first<{ status: string }>();
      expect(session?.status).toBe('AWAITING_CATALOG');
    });

    it('immediately starts style selection when catalog is already persisted in D1', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);
      expect(catalogId).toBeDefined();

      const update: TelegramUpdate = {
        update_id: 3,
        message: {
          message_id: 12,
          from: { id: 33333, is_bot: false, first_name: 'Charlie' },
          chat: { id: 33333, type: 'private', first_name: 'Charlie' },
          date: Date.now(),
          text: sampleCatalog.sourceUrl,
        },
      };

      const req = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': WEBHOOK_SECRET,
        },
        body: JSON.stringify(update),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);

      const session = await env.DB.prepare('SELECT * FROM telegram_sessions WHERE user_id = ?')
        .bind('33333')
        .first<{ status: string; catalog_id: string }>();
      expect(session?.status).toBe('SELECTING_STYLES');
      expect(session?.catalog_id).toBe(catalogId);
    });
  });

  describe('Interactive UX Callbacks, Ownership & Invariants', () => {
    it('rejects callback queries from unauthorized users (ownership check)', async () => {
      // User 44444 has an active session
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);

      await env.DB.prepare(
        `INSERT INTO telegram_users (id, first_name, created_at, updated_at) VALUES (?, ?, ?, ?)`
      )
        .bind('44444', 'Dave', Date.now(), Date.now())
        .run();

      await env.DB.prepare(
        `INSERT INTO telegram_sessions (id, user_id, chat_id, catalog_id, selected_styles, selected_formats, status, created_at, updated_at)
         VALUES (?, ?, ?, ?, '[]', '["TTF"]', 'SELECTING_STYLES', ?, ?)`
      )
        .bind('sess_dave', '44444', '44444', catalogId, Date.now(), Date.now())
        .run();

      // Another user (99999) attempts to click Dave's inline button
      const attackerUpdate: TelegramUpdate = {
        update_id: 4,
        callback_query: {
          id: 'cb_attack_1',
          from: { id: 99999, is_bot: false, first_name: 'Attacker' },
          data: 'st:t:hn_regular',
        },
      };

      const req = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': WEBHOOK_SECRET,
        },
        body: JSON.stringify(attackerUpdate),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);

      // Dave's session should remain completely unchanged
      const daveSession = await env.DB.prepare(
        'SELECT * FROM telegram_sessions WHERE user_id = ?'
      )
        .bind('44444')
        .first<{ selected_styles: string }>();
      expect(daveSession?.selected_styles).toBe('[]');
    });

    it('toggles styles and respects callback data byte limit (< 64 bytes)', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);

      await env.DB.prepare(
        `INSERT INTO telegram_users (id, first_name, created_at, updated_at) VALUES (?, ?, ?, ?)`
      )
        .bind('55555', 'Eve', Date.now(), Date.now())
        .run();

      await env.DB.prepare(
        `INSERT INTO telegram_sessions (id, user_id, chat_id, catalog_id, selected_styles, selected_formats, status, created_at, updated_at)
         VALUES (?, ?, ?, ?, '[]', '["TTF"]', 'SELECTING_STYLES', ?, ?)`
      )
        .bind('sess_eve', '55555', '55555', catalogId, Date.now(), Date.now())
        .run();

      const callbackData = 'st:t:hn_regular';
      expect(new TextEncoder().encode(callbackData).length).toBeLessThan(64);

      const update: TelegramUpdate = {
        update_id: 5,
        callback_query: {
          id: 'cb_eve_1',
          from: { id: 55555, is_bot: false, first_name: 'Eve' },
          data: callbackData,
        },
      };

      const req = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': WEBHOOK_SECRET,
        },
        body: JSON.stringify(update),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);

      const eveSession = await env.DB.prepare(
        'SELECT * FROM telegram_sessions WHERE user_id = ?'
      )
        .bind('55555')
        .first<{ selected_styles: string }>();
      expect(JSON.parse(eveSession?.selected_styles || '[]')).toEqual(['hn_regular']);
    });

    it('rejects transitioning to formats with empty style selection', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);

      await env.DB.prepare(
        `INSERT INTO telegram_users (id, first_name, created_at, updated_at) VALUES (?, ?, ?, ?)`
      )
        .bind('66666', 'Frank', Date.now(), Date.now())
        .run();

      await env.DB.prepare(
        `INSERT INTO telegram_sessions (id, user_id, chat_id, catalog_id, selected_styles, selected_formats, status, created_at, updated_at)
         VALUES (?, ?, ?, ?, '[]', '["TTF"]', 'SELECTING_STYLES', ?, ?)`
      )
        .bind('sess_frank', '66666', '66666', catalogId, Date.now(), Date.now())
        .run();

      const update: TelegramUpdate = {
        update_id: 6,
        callback_query: {
          id: 'cb_frank_next',
          from: { id: 66666, is_bot: false, first_name: 'Frank' },
          data: 'st:next',
        },
      };

      const req = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': WEBHOOK_SECRET,
        },
        body: JSON.stringify(update),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);

      // Session status must remain SELECTING_STYLES
      const session = await env.DB.prepare('SELECT * FROM telegram_sessions WHERE user_id = ?')
        .bind('66666')
        .first<{ status: string }>();
      expect(session?.status).toBe('SELECTING_STYLES');
    });

    it('creates order with AWAITING_PAYMENT status and ensures idempotency on replay', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);

      await env.DB.prepare(
        `INSERT INTO telegram_users (id, first_name, created_at, updated_at) VALUES (?, ?, ?, ?)`
      )
        .bind('77777', 'Grace', Date.now(), Date.now())
        .run();

      await env.DB.prepare(
        `INSERT INTO telegram_sessions (id, user_id, chat_id, catalog_id, selected_styles, selected_formats, status, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, '["TTF","WOFF2"]', 'CONFIRMING', ?, ?)`
      )
        .bind(
          'sess_grace',
          '77777',
          '77777',
          catalogId,
          JSON.stringify(['hn_regular', 'hn_bold']),
          Date.now(),
          Date.now()
        )
        .run();

      const update: TelegramUpdate = {
        update_id: 7,
        callback_query: {
          id: 'cb_grace_confirm',
          from: { id: 77777, is_bot: false, first_name: 'Grace' },
          data: 'ord:confirm',
        },
      };

      const req = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': WEBHOOK_SECRET,
        },
        body: JSON.stringify(update),
      });

      const ctx1 = createExecutionContext();
      const res1 = await worker.fetch(req, testEnv, ctx1);
      await waitOnExecutionContext(ctx1);

      expect(res1.status).toBe(200);

      // Verify order creation
      const orders = await env.DB.prepare('SELECT * FROM orders WHERE user_id = ?')
        .bind('77777')
        .all<{ id: string; status: string; total_amount: number }>();
      expect(orders.results.length).toBe(1);
      const createdOrder = orders.results[0];
      expect(createdOrder.status).toBe('AWAITING_PAYMENT');
      expect(createdOrder.total_amount).toBe(100000); // 2 styles * 50000

      // Verify order_items
      const items = await env.DB.prepare('SELECT * FROM order_items WHERE order_id = ?')
        .bind(createdOrder.id)
        .all<{ font_id: string; font_name: string }>();
      expect(items.results.length).toBe(2);
      expect(items.results.map((i) => i.font_id).sort()).toEqual(['hn_bold', 'hn_regular']);

      // Replay confirm action -> must not create duplicate order (Idempotency)
      const ctx2 = createExecutionContext();
      const res2 = await worker.fetch(req, testEnv, ctx2);
      await waitOnExecutionContext(ctx2);

      expect(res2.status).toBe(200);

      const ordersAfterReplay = await env.DB.prepare(
        'SELECT * FROM orders WHERE user_id = ?'
      )
        .bind('77777')
        .all();
      expect(ordersAfterReplay.results.length).toBe(1);
    });
  });
});

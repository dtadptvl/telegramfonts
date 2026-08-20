import { describe, it, expect, beforeEach } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker, { type Env } from '../src/index';
import { CatalogService } from '../src/services/catalog-service';
import { SessionService, SessionConflictError } from '../src/services/session-service';
import { OrderService } from '../src/services/order-service';
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
    globalThis.fetch = async (input: RequestInfo | URL) => {
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

  describe('Webhook Security, Update Parsing & Retry Ledger (BLOCK A & BLOCK 3)', () => {
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

    it('recovers from transient processing failure: first attempt returns 500, retry succeeds, subsequent duplicate ignored', async () => {
      let failFirstAttempt = true;

      globalThis.fetch = async (input: RequestInfo | URL) => {
        const urlStr = typeof input === 'string' ? input : input.toString();
        if (urlStr.includes('api.telegram.org')) {
          if (failFirstAttempt) {
            failFirstAttempt = false;
            return new Response(JSON.stringify({ ok: false, error_code: 500, description: 'Transient Gateway Error' }), {
              status: 500,
            });
          }
          return new Response(JSON.stringify({ ok: true, result: { message_id: 1234 } }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response('Not found', { status: 404 });
      };

      const update: TelegramUpdate = {
        update_id: 9988,
        message: {
          message_id: 60,
          from: { id: 88882, is_bot: false, first_name: 'RetryUser' },
          chat: { id: 88882, type: 'private', first_name: 'RetryUser' },
          date: Date.now(),
          text: '/start',
        },
      };

      // Attempt 1: Fails downstream, returns 500
      const req1 = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': WEBHOOK_SECRET,
        },
        body: JSON.stringify(update),
      });

      const ctx1 = createExecutionContext();
      const res1 = await worker.fetch(req1, testEnv, ctx1);
      await waitOnExecutionContext(ctx1);
      expect(res1.status).toBe(500);

      // Verify ledger left in PROCESSING state
      const updateRow = await env.DB.prepare('SELECT * FROM telegram_updates WHERE update_id = ?')
        .bind(9988)
        .first<{ status: string }>();
      expect(updateRow?.status).toBe('PROCESSING');

      // Attempt 2: Telegram retries the same update_id -> succeeds
      const req2 = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': WEBHOOK_SECRET,
        },
        body: JSON.stringify(update),
      });

      const ctx2 = createExecutionContext();
      const res2 = await worker.fetch(req2, testEnv, ctx2);
      await waitOnExecutionContext(ctx2);
      expect(res2.status).toBe(200);

      // Verify ledger updated to COMPLETED
      const updateRowCompleted = await env.DB.prepare('SELECT * FROM telegram_updates WHERE update_id = ?')
        .bind(9988)
        .first<{ status: string }>();
      expect(updateRowCompleted?.status).toBe('COMPLETED');

      // Attempt 3: Duplicate retry after COMPLETED -> safely ignored without re-executing
      const req3 = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': WEBHOOK_SECRET,
        },
        body: JSON.stringify(update),
      });

      const ctx3 = createExecutionContext();
      const res3 = await worker.fetch(req3, testEnv, ctx3);
      await waitOnExecutionContext(ctx3);
      expect(res3.status).toBe(200);
      const data3 = await res3.json();
      expect(data3).toEqual({ status: 'ignored_duplicate_update' });
    });
  });

  describe('User Onboarding & URL Ingestion', () => {
    it('handles /start by resetting user session in D1', async () => {
      const update: TelegramUpdate = {
        update_id: 1001,
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
        update_id: 1002,
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
        update_id: 1003,
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

  describe('Interactive UX Callbacks, State/Token Binding & Safety (BLOCK B & BLOCK 2)', () => {
    it('rejects stale callback tokens from outdated menus without mutating state', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);
      const sessionService = new SessionService(env.DB);

      await sessionService.upsertTelegramUser({ id: 44444, is_bot: false, first_name: 'Dave' });
      await sessionService.getOrCreateSession('44444', '44444');
      await sessionService.updateSessionCatalog('44444', catalogId, 'SELECTING_STYLES');

      const update: TelegramUpdate = {
        update_id: 1004,
        callback_query: {
          id: 'cb_stale_token',
          from: { id: 44444, is_bot: false, first_name: 'Dave' },
          data: 'st:t:STALETOK:0',
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

      // Session styles remain untouched
      const session = await sessionService.getSessionByUserId('44444');
      expect(session?.selected_styles).toBe('[]');
    });

    it('rejects actions sent in the wrong workflow state', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);
      const sessionService = new SessionService(env.DB);

      await sessionService.upsertTelegramUser({ id: 55551, is_bot: false, first_name: 'WrongStateUser' });
      await sessionService.getOrCreateSession('55551', '55551');
      await sessionService.updateSessionCatalog('55551', catalogId, 'SELECTING_STYLES');

      const activeSession = await sessionService.getSessionByUserId('55551');
      const token = activeSession!.workflow_token;

      // Try sending ord:cnf while in SELECTING_STYLES state
      const update: TelegramUpdate = {
        update_id: 1005,
        callback_query: {
          id: 'cb_wrong_state',
          from: { id: 55551, is_bot: false, first_name: 'WrongStateUser' },
          data: `ord:cnf:${token}`,
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

      // No order created
      const orders = await env.DB.prepare('SELECT * FROM orders WHERE user_id = ?')
        .bind('55551')
        .all();
      expect(orders.results.length).toBe(0);
    });

    it('guarantees callback data <= 64 bytes even with very long external style IDs', async () => {
      const catalogWithLongStyles: FontCatalog = {
        sourceUrl: 'https://www.myfonts.com/collections/super-long-font-collection-name',
        canonicalKey: 'myfonts:collections/super-long-font-collection-name',
        familyName: 'Super Long Font Name With Many Words',
        styles: [
          {
            id: 'super_extra_ultra_long_style_identifier_exceeding_standard_limits_1234567890_abcdefghijklmnopqrstuvwxyz',
            displayName: 'Ultra Extended Black Italic Pro',
            price: 60000,
          },
        ],
      };

      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(catalogWithLongStyles);
      const sessionService = new SessionService(env.DB);

      await sessionService.upsertTelegramUser({ id: 55552, is_bot: false, first_name: 'LongStyleUser' });
      await sessionService.getOrCreateSession('55552', '55552');
      await sessionService.updateSessionCatalog('55552', catalogId, 'SELECTING_STYLES');

      const session = await sessionService.getSessionByUserId('55552');
      const token = session!.workflow_token;

      // Index-based callback format: st:t:<token>:<index>
      const callbackData = `st:t:${token}:0`;
      const byteLength = new TextEncoder().encode(callbackData).length;

      // Strictly below Telegram 64-byte limit
      expect(byteLength).toBeLessThan(30);

      const update: TelegramUpdate = {
        update_id: 1006,
        callback_query: {
          id: 'cb_long_style',
          from: { id: 55552, is_bot: false, first_name: 'LongStyleUser' },
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

      const updatedSession = await sessionService.getSessionByUserId('55552');
      expect(JSON.parse(updatedSession!.selected_styles)).toEqual([
        'super_extra_ultra_long_style_identifier_exceeding_standard_limits_1234567890_abcdefghijklmnopqrstuvwxyz',
      ]);
    });

    it('detects concurrent CAS conflicts and prevents state corruption (BLOCK B)', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);
      const sessionService = new SessionService(env.DB);

      await sessionService.upsertTelegramUser({ id: 55553, is_bot: false, first_name: 'CasUser' });
      await sessionService.getOrCreateSession('55553', '55553');
      await sessionService.updateSessionCatalog('55553', catalogId, 'SELECTING_STYLES');

      const session = await sessionService.getSessionByUserId('55553');
      const token = session!.workflow_token;
      const initialVersion = session!.version;

      // Mutate once to increment version
      await sessionService.toggleStyleSelection('55553', token, 'hn_regular', initialVersion);

      // Attempting to mutate with the now-stale initialVersion must throw SessionConflictError
      await expect(
        sessionService.toggleStyleSelection('55553', token, 'hn_bold', initialVersion)
      ).rejects.toThrow(SessionConflictError);
    });
  });

  describe('Atomic Checkout & Concurrency Idempotency (BLOCK 1, BLOCK B & BLOCK C)', () => {
    it('creates order + order_items atomically with AWAITING_PAYMENT status', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);
      const sessionService = new SessionService(env.DB);
      const orderService = new OrderService(env.DB);

      await sessionService.upsertTelegramUser({ id: 77771, is_bot: false, first_name: 'Grace' });
      await sessionService.getOrCreateSession('77771', '77771');
      await sessionService.updateSessionCatalog('77771', catalogId, 'SELECTING_STYLES');
      
      const sess1 = await sessionService.getSessionByUserId('77771');
      await sessionService.setAllStyles('77771', sess1!.workflow_token, ['hn_regular', 'hn_bold'], sess1!.version);
      
      const sess2 = await sessionService.getSessionByUserId('77771');
      await sessionService.transitionStatus('77771', sess2!.workflow_token, 'SELECTING_STYLES', 'SELECTING_FORMATS', sess2!.version);

      const sess3 = await sessionService.getSessionByUserId('77771');
      await sessionService.toggleFormatSelection('77771', sess3!.workflow_token, 'WOFF2', sess3!.version); // ['TTF', 'WOFF2']

      const sess4 = await sessionService.getSessionByUserId('77771');
      await sessionService.transitionStatus('77771', sess4!.workflow_token, 'SELECTING_FORMATS', 'CONFIRMING', sess4!.version);

      const session = await sessionService.getSessionByUserId('77771');
      const catalog = await catalogService.getCatalogById(catalogId);

      const result = await orderService.createOrderFromSession(session!, catalog!);

      expect(result.isExisting).toBe(false);
      expect(result.orderId).toBeDefined();
      expect(result.totalAmount).toBe(100000);
      expect(result.itemsCount).toBe(2);

      // Verify DB truth
      const order = await env.DB.prepare('SELECT * FROM orders WHERE id = ?')
        .bind(result.orderId)
        .first<{ status: string; total_amount: number; checkout_token: string }>();

      expect(order?.status).toBe('AWAITING_PAYMENT');
      expect(order?.total_amount).toBe(100000);
      expect(order?.checkout_token).toBe(session!.checkout_token);

      const items = await env.DB.prepare('SELECT * FROM order_items WHERE order_id = ?')
        .bind(result.orderId)
        .all<{ font_id: string }>();

      expect(items.results.length).toBe(2);

      // Concurrent/replay execution returns the exact same order
      const replayResult = await orderService.createOrderFromSession(session!, catalog!);
      expect(replayResult.isExisting).toBe(true);
      expect(replayResult.orderId).toBe(result.orderId);
      expect(replayResult.totalAmount).toBe(100000);
    });

    it('handles concurrent first catalog completion idempotently without unique constraint errors (BLOCK C)', async () => {
      const catalogService = new CatalogService(env.DB);

      const catalogA: FontCatalog = {
        sourceUrl: 'https://www.myfonts.com/collections/concurrent-catalog-family',
        canonicalKey: 'myfonts:collections/concurrent-catalog-family',
        familyName: 'Concurrent Family',
        styles: [
          { id: 'style_c1', displayName: 'Concurrent Style 1', price: 50000 },
          { id: 'style_c2', displayName: 'Concurrent Style 2', price: 50000 },
        ],
      };

      const catalogB: FontCatalog = { ...catalogA };

      // Simulate simultaneous first completion calls
      const [id1, id2] = await Promise.all([
        catalogService.persistCatalogResult(catalogA),
        catalogService.persistCatalogResult(catalogB),
      ]);

      expect(id1).toBe(id2);

      const storedStyles = await env.DB.prepare(
        'SELECT style_id FROM catalog_styles WHERE catalog_id = ?'
      )
        .bind(id1)
        .all();

      expect(storedStyles.results.length).toBe(2);
    });

    it('atomic catalog persistence purges stale styles on update (BLOCK 4)', async () => {
      const catalogService = new CatalogService(env.DB);

      const initialCatalog: FontCatalog = {
        sourceUrl: 'https://www.myfonts.com/collections/stale-test-family',
        canonicalKey: 'myfonts:collections/stale-test-family',
        familyName: 'Stale Test Family',
        styles: [
          { id: 'style_v1_old', displayName: 'Old Style V1', price: 40000 },
          { id: 'style_v1_common', displayName: 'Common Style', price: 50000 },
        ],
      };

      const catalogId = await catalogService.persistCatalogResult(initialCatalog);

      const updatedCatalog: FontCatalog = {
        sourceUrl: 'https://www.myfonts.com/collections/stale-test-family',
        canonicalKey: 'myfonts:collections/stale-test-family',
        familyName: 'Stale Test Family (Updated)',
        styles: [
          { id: 'style_v1_common', displayName: 'Common Style', price: 50000 },
          { id: 'style_v2_new', displayName: 'New Style V2', price: 60000 },
        ],
      };

      await catalogService.persistCatalogResult(updatedCatalog);

      const storedStyles = await env.DB.prepare(
        'SELECT style_id FROM catalog_styles WHERE catalog_id = ? ORDER BY style_id ASC'
      )
        .bind(catalogId)
        .all<{ style_id: string }>();

      const styleIds = storedStyles.results.map((s) => s.style_id);
      expect(styleIds).toEqual(['style_v1_common', 'style_v2_new']);
      expect(styleIds).not.toContain('style_v1_old');
    });
  });
});

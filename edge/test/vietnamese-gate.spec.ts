import { describe, it, expect, beforeEach } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker, { type Env } from '../src/index';
import { CatalogService } from '../src/services/catalog-service';
import { SessionService } from '../src/services/session-service';
import {
  OrderService,
  VIETNAMESE_ORDERING_TEMPORARILY_DISABLED,
} from '../src/services/order-service';
import type { FontCatalog } from '../src/types/catalog';
import type { TelegramUpdate } from '../src/types/telegram';

/**
 * T-FAST-ATLAS-A23-ORIGINAL-RELEASE-01 (O1 & O2 acceptance):
 * Proves the mode-safe pre-payment Vietnamese gate:
 * (a) VIETNAMESE selection while disabled -> clear bilingual unavailable reply,
 *     NO session mode set, NO order created, NO payment code / VietQR generated;
 * (b) ORIGINAL selection while disabled -> normal flow reaches order creation & pricing;
 * (c) flag 'true' -> VIETNAMESE flow proceeds normally;
 * (d) OrderService defense-in-depth guard rejects VIETNAMESE order creation while disabled.
 */

const BOT_TOKEN = 'mock_bot_token_gate_123';
const WEBHOOK_SECRET = 'mock_webhook_secret_gate_456';

const sampleCatalog: FontCatalog = {
  sourceUrl: 'https://www.myfonts.com/collections/helvetica-now-font-monotype-imaging',
  canonicalKey: 'myfonts:collections/helvetica-now-font-monotype-imaging',
  familyName: 'Helvetica Now',
  foundry: 'Monotype Imaging',
  styles: [
    { id: 'hn_regular', displayName: 'Helvetica Now Regular', price: 5000 },
    { id: 'hn_bold', displayName: 'Helvetica Now Bold', price: 5000 },
  ],
};

let userSeq = 880000;

describe('T-FAST-ATLAS-A23-ORIGINAL-RELEASE-01 O2: VIETNAMESE Pre-Payment Gate', () => {
  let outboundMessages: Array<{ url: string; body: Record<string, unknown> }>;

  beforeEach(() => {
    outboundMessages = [];
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const urlStr = typeof input === 'string' ? input : input.toString();
      let body: Record<string, unknown> = {};
      if (init?.body && typeof init.body === 'string') {
        try {
          body = JSON.parse(init.body) as Record<string, unknown>;
        } catch {
          body = {};
        }
      }
      outboundMessages.push({ url: urlStr, body });

      if (urlStr.includes('api.telegram.org')) {
        return new Response(JSON.stringify({ ok: true, result: { message_id: 1001 } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('Not found', { status: 404 });
    };
  });

  it('(a) VIETNAMESE selection while disabled: returns unavailable reply, NO session mode set, NO order, NO payment', async () => {
    const userId = String(userSeq++);
    const sessionService = new SessionService(env.DB);
    await sessionService.upsertTelegramUser({
      id: Number(userId),
      is_bot: false,
      first_name: 'GateUserVN',
    });
    const s0 = await sessionService.getOrCreateSession(userId, userId);
    expect(s0.mode).toBeNull();

    const gateDisabledEnv: Env = {
      ...(env as unknown as Env),
      TELEGRAM_BOT_TOKEN: BOT_TOKEN,
      TELEGRAM_WEBHOOK_SECRET: WEBHOOK_SECRET,
      VIETNAMESE_ORDERING_ENABLED: 'false',
    };

    const update: TelegramUpdate = {
      update_id: 2001,
      callback_query: {
        id: 'cb_gate_vn_disabled',
        from: { id: Number(userId), is_bot: false, first_name: 'GateUserVN' },
        data: `mode:set:VIETNAMESE:${s0.workflow_token}`,
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
    const res = await worker.fetch(req, gateDisabledEnv, ctx);
    await waitOnExecutionContext(ctx);

    expect(res.status).toBe(200);

    // 1. Session mode was NOT set to VIETNAMESE
    const sessionAfter = await sessionService.getSessionByUserId(userId);
    expect(sessionAfter?.mode).toBeNull();

    // 2. Zero orders exist for this user
    const orderCount = await env.DB.prepare('SELECT count(*) AS count FROM orders WHERE user_id = ?')
      .bind(userId)
      .first<{ count: number }>();
    expect(orderCount?.count).toBe(0);

    // 3. Outbound Telegram messages contained clear unavailable notice
    const sendMsg = outboundMessages.find((m) => m.url.includes('/sendMessage'));
    expect(sendMsg).toBeDefined();
    const msgText = String(sendMsg?.body.text || '');
    expect(msgText).toContain('Tính năng đang tạm bảo trì');
    expect(msgText).toContain('VIETNAMESE mode ordering is temporarily paused');

    // 4. Callback was answered with alert text
    const answerCb = outboundMessages.find((m) => m.url.includes('/answerCallbackQuery'));
    expect(answerCb).toBeDefined();
    expect(String(answerCb?.body.text || '')).toContain('Tính năng VIETNAMESE tạm thời chưa khả dụng');
  });

  it('(b) ORIGINAL selection while disabled: normal flow proceeds to mode selection & order creation', async () => {
    const userId = String(userSeq++);
    const sessionService = new SessionService(env.DB);
    const catalogService = new CatalogService(env.DB);
    const catalogId = await catalogService.persistCatalogResult(sampleCatalog);

    await sessionService.upsertTelegramUser({
      id: Number(userId),
      is_bot: false,
      first_name: 'GateUserOrig',
    });
    const s0 = await sessionService.getOrCreateSession(userId, userId);
    expect(s0.mode).toBeNull();

    const gateDisabledEnv: Env = {
      ...(env as unknown as Env),
      TELEGRAM_BOT_TOKEN: BOT_TOKEN,
      TELEGRAM_WEBHOOK_SECRET: WEBHOOK_SECRET,
      VIETNAMESE_ORDERING_ENABLED: 'false',
    };

    // User selects ORIGINAL mode
    const update: TelegramUpdate = {
      update_id: 2002,
      callback_query: {
        id: 'cb_gate_orig',
        from: { id: Number(userId), is_bot: false, first_name: 'GateUserOrig' },
        data: `mode:set:ORIGINAL:${s0.workflow_token}`,
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
    const res = await worker.fetch(req, gateDisabledEnv, ctx);
    await waitOnExecutionContext(ctx);

    expect(res.status).toBe(200);

    // 1. Session mode is properly set to ORIGINAL
    const sessionAfter = await sessionService.getSessionByUserId(userId);
    expect(sessionAfter?.mode).toBe('ORIGINAL');

    // 2. Advance to confirmation and create order
    await sessionService.updateSessionCatalog(userId, catalogId, 'SELECTING_STYLES');
    const s1 = await sessionService.getSessionByUserId(userId);
    await sessionService.setAllStyles(
      userId,
      s1!.workflow_token,
      sampleCatalog.styles.map((s) => s.id),
      s1!.version
    );
    const s2 = await sessionService.getSessionByUserId(userId);
    await sessionService.transitionStatus(
      userId,
      s2!.workflow_token,
      'SELECTING_STYLES',
      'CONFIRMING',
      s2!.version
    );
    const sConfirming = await sessionService.getSessionByUserId(userId);

    const orderService = new OrderService(
      env.DB,
      gateDisabledEnv.VIETNAMESE_ORDERING_ENABLED === 'true'
    );
    const orderResult = await orderService.createOrderFromSession(
      sConfirming!,
      sampleCatalog
    );

    expect(orderResult.isExisting).toBe(false);
    expect(orderResult.orderId).toBeDefined();
    expect(orderResult.totalAmount).toBe(10000); // 2 styles * 5,000 VND

    const orderRow = await env.DB.prepare('SELECT mode, status, total_amount FROM orders WHERE id = ?')
      .bind(orderResult.orderId)
      .first<{ mode: string; status: string; total_amount: number }>();

    expect(orderRow?.mode).toBe('ORIGINAL');
    expect(orderRow?.status).toBe('AWAITING_PAYMENT');
    expect(orderRow?.total_amount).toBe(10000);
  });

  it('(c) flag "true": VIETNAMESE mode flow proceeds normally', async () => {
    const userId = String(userSeq++);
    const sessionService = new SessionService(env.DB);
    const catalogService = new CatalogService(env.DB);
    const catalogId = await catalogService.persistCatalogResult(sampleCatalog);

    await sessionService.upsertTelegramUser({
      id: Number(userId),
      is_bot: false,
      first_name: 'GateUserVNEnabled',
    });
    const s0 = await sessionService.getOrCreateSession(userId, userId);

    const gateEnabledEnv: Env = {
      ...(env as unknown as Env),
      TELEGRAM_BOT_TOKEN: BOT_TOKEN,
      TELEGRAM_WEBHOOK_SECRET: WEBHOOK_SECRET,
      VIETNAMESE_ORDERING_ENABLED: 'true',
    };

    const update: TelegramUpdate = {
      update_id: 2003,
      callback_query: {
        id: 'cb_gate_vn_enabled',
        from: { id: Number(userId), is_bot: false, first_name: 'GateUserVNEnabled' },
        data: `mode:set:VIETNAMESE:${s0.workflow_token}`,
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
    const res = await worker.fetch(req, gateEnabledEnv, ctx);
    await waitOnExecutionContext(ctx);

    expect(res.status).toBe(200);

    // 1. Session mode is set to VIETNAMESE
    const sessionAfter = await sessionService.getSessionByUserId(userId);
    expect(sessionAfter?.mode).toBe('VIETNAMESE');

    // 2. OrderService with enabled flag creates the VIETNAMESE order at 8,000 VND/style
    await sessionService.updateSessionCatalog(userId, catalogId, 'SELECTING_STYLES');
    const s1 = await sessionService.getSessionByUserId(userId);
    await sessionService.setAllStyles(
      userId,
      s1!.workflow_token,
      sampleCatalog.styles.map((s) => s.id),
      s1!.version
    );
    const s2 = await sessionService.getSessionByUserId(userId);
    await sessionService.transitionStatus(
      userId,
      s2!.workflow_token,
      'SELECTING_STYLES',
      'CONFIRMING',
      s2!.version
    );
    const sConfirming = await sessionService.getSessionByUserId(userId);

    const orderService = new OrderService(
      env.DB,
      gateEnabledEnv.VIETNAMESE_ORDERING_ENABLED === 'true'
    );
    const orderResult = await orderService.createOrderFromSession(
      sConfirming!,
      sampleCatalog
    );

    expect(orderResult.isExisting).toBe(false);
    expect(orderResult.totalAmount).toBe(16000); // 2 styles * 8,000 VND

    const orderRow = await env.DB.prepare('SELECT mode, status, total_amount FROM orders WHERE id = ?')
      .bind(orderResult.orderId)
      .first<{ mode: string; status: string; total_amount: number }>();

    expect(orderRow?.mode).toBe('VIETNAMESE');
    expect(orderRow?.status).toBe('AWAITING_PAYMENT');
    expect(orderRow?.total_amount).toBe(16000);
  });

  it('(d) order-service defense-in-depth: rejects VIETNAMESE order creation while disabled', async () => {
    const userId = String(userSeq++);
    const sessionService = new SessionService(env.DB);
    const catalogService = new CatalogService(env.DB);
    const catalogId = await catalogService.persistCatalogResult(sampleCatalog);

    await sessionService.upsertTelegramUser({
      id: Number(userId),
      is_bot: false,
      first_name: 'GateUserDefense',
    });
    const s0 = await sessionService.getOrCreateSession(userId, userId);

    // Force session to VIETNAMESE mode (simulating direct DB mutation or bypassed handler)
    await sessionService.selectMode(userId, s0.workflow_token, 'VIETNAMESE', s0.version);
    await sessionService.updateSessionCatalog(userId, catalogId, 'SELECTING_STYLES');
    const s1 = await sessionService.getSessionByUserId(userId);
    await sessionService.setAllStyles(
      userId,
      s1!.workflow_token,
      sampleCatalog.styles.map((s) => s.id),
      s1!.version
    );
    const s2 = await sessionService.getSessionByUserId(userId);
    await sessionService.transitionStatus(
      userId,
      s2!.workflow_token,
      'SELECTING_STYLES',
      'CONFIRMING',
      s2!.version
    );
    const sConfirming = await sessionService.getSessionByUserId(userId);
    expect(sConfirming?.mode).toBe('VIETNAMESE');

    // OrderService initialized with disabled flag (default false)
    const orderServiceDisabled = new OrderService(env.DB, false);

    await expect(
      orderServiceDisabled.createOrderFromSession(sConfirming!, sampleCatalog)
    ).rejects.toThrow(VIETNAMESE_ORDERING_TEMPORARILY_DISABLED);

    // Verify ZERO orders were created
    const count = await env.DB.prepare('SELECT count(*) AS count FROM orders WHERE user_id = ?')
      .bind(userId)
      .first<{ count: number }>();
    expect(count?.count).toBe(0);
  });
});

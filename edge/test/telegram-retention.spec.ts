import { afterEach, describe, expect, it } from 'vitest';
import { env } from 'cloudflare:test';
import type { Env } from '../src/env';
import type { OrderRecord } from '../src/services/order-service';
import {
  TelegramClient,
  type TelegramClientOptions,
} from '../src/services/telegram-client';
import {
  EPHEMERAL_TELEGRAM_MESSAGE_TTL_MS,
  TelegramMessageRetentionService,
} from '../src/services/telegram-message-retention';
import { getOrderPaymentQrUrl, renderOrderCreatedMessage } from '../src/handlers/telegram-webhook';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe('Telegram QR delivery and ephemeral message retention', () => {
  it('sends VietQR through Telegram sendPhoto without putting the URL in the user text', async () => {
    const order: OrderRecord = {
      id: 'ord_qr_review',
      user_id: 'qr-review-user',
      status: 'AWAITING_PAYMENT',
      total_amount: 50000,
      currency: 'VND',
      metadata: null,
      checkout_token: 'checkout_qr_review',
      payment_code: 'TF8X9K2M',
      created_at: 1,
      updated_at: 1,
    };
    const testEnv = {
      ...(env as unknown as Env),
      BANK_ID: 'MB',
      BANK_ACCOUNT_NUMBER: '123456789',
      BANK_ACCOUNT_NAME: 'TELEFONT STORE',
      VIETQR_TEMPLATE: 'compact2',
    } as Env;
    const rendered = renderOrderCreatedMessage(order, testEnv);
    const qrUrl = getOrderPaymentQrUrl(order, testEnv);
    const calls: Array<{ path: string; body: Record<string, unknown> }> = [];
    const scheduled: Array<{ chatId: string | number; messageId: number }> = [];

    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      calls.push({
        path: new URL(url).pathname.split('/').pop() || '',
        body: JSON.parse(String(init?.body || '{}')) as Record<string, unknown>,
      });
      return new Response(JSON.stringify({ ok: true, result: { message_id: 701 } }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const options: TelegramClientOptions = {
      onEphemeralMessageSent: async (chatId, messageId) => {
        scheduled.push({ chatId, messageId });
      },
    };
    const tg = new TelegramClient('qr-review-token', options);
    await tg.sendPhoto({
      chat_id: 'qr-review-chat',
      photo: qrUrl!,
      caption: 'Quét mã QR để thanh toán',
    });

    expect(rendered.text).not.toContain('img.vietqr.io');
    expect(rendered.text).toContain('Mã đơn:');
    expect(rendered.text).toContain('Mã thanh toán:');
    expect(rendered.text).toContain('Số tiền:');
    expect(rendered.text).toContain('Chờ thanh toán');
    expect(rendered.text).toContain('ảnh bên dưới');
    expect(rendered.text).not.toContain('Thông tin chuyển khoản');
    expect(rendered.text).not.toContain('Số tài khoản');
    expect(rendered.text).not.toContain('Tên tài khoản');
    expect(rendered.text).not.toContain('Nội dung / mã chuyển khoản');
    expect(calls).toEqual([
      {
        path: 'sendPhoto',
        body: {
          chat_id: 'qr-review-chat',
          photo: qrUrl,
          caption: 'Quét mã QR để thanh toán',
          parse_mode: 'HTML',
        },
      },
    ]);
    expect(scheduled).toEqual([{ chatId: 'qr-review-chat', messageId: 701 }]);
  });

  it('keeps allowlisted messages and schedules other bot messages for cleanup', async () => {
    const scheduled: number[] = [];
    globalThis.fetch = async () =>
      new Response(JSON.stringify({ ok: true, result: { message_id: scheduled.length + 1 } }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });

    const tg = new TelegramClient('retention-review-token', {
      onEphemeralMessageSent: async (_chatId, messageId) => {
        scheduled.push(messageId);
      },
    });
    await tg.sendMessage({ chat_id: 'retention-review-chat', text: 'ephemeral' });
    await tg.sendMessage(
      { chat_id: 'retention-review-chat', text: 'persistent' },
      { retention: 'persistent' }
    );
    await tg.sendDocument({
      chat_id: 'retention-review-chat',
      document: new Uint8Array([1, 2, 3]),
      filename: 'delivered.zip',
    });

    expect(scheduled).toEqual([1]);
  });

  it('cleans a bounded expired batch, removes already-gone messages, and retries failures', async () => {
    const service = new TelegramMessageRetentionService(env.DB);
    const chatId = `retention-review-${crypto.randomUUID()}`;
    const now = 1_000_000;
    await service.scheduleEphemeralMessage(chatId, 1, now - EPHEMERAL_TELEGRAM_MESSAGE_TTL_MS);
    await service.scheduleEphemeralMessage(chatId, 2, now - EPHEMERAL_TELEGRAM_MESSAGE_TTL_MS);

    let failMessageTwo = true;
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(typeof input === 'string' ? input : input.toString()).pathname.split('/').pop();
      if (path !== 'deleteMessage') return new Response('{}', { status: 404 });
      const body = JSON.parse(String(init?.body || '{}')) as { message_id: number };
      if (body.message_id === 2 && failMessageTwo) {
        return new Response(JSON.stringify({ ok: false, description: 'temporary Telegram failure' }), {
          status: 500,
        });
      }
      if (body.message_id === 1) {
        return new Response(JSON.stringify({ ok: false, description: 'message to delete not found' }), {
          status: 400,
        });
      }
      return new Response(JSON.stringify({ ok: true, result: true }), { status: 200 });
    };

    const tg = new TelegramClient('retention-cleanup-token');
    const first = await service.deleteExpiredMessages(tg, now, 2);
    expect(first).toEqual({ attempted: 2, removed: 1, pending: 1 });

    const remainingAfterFirst = await env.DB
      .prepare('SELECT message_id FROM telegram_message_retention WHERE chat_id = ?')
      .bind(chatId)
      .all<{ message_id: number }>();
    expect(remainingAfterFirst.results).toEqual([{ message_id: 2 }]);

    failMessageTwo = false;
    const second = await service.deleteExpiredMessages(tg, now, 100);
    expect(second).toEqual({ attempted: 1, removed: 1, pending: 0 });

    const remainingAfterSecond = await env.DB
      .prepare('SELECT message_id FROM telegram_message_retention WHERE chat_id = ?')
      .bind(chatId)
      .all();
    expect(remainingAfterSecond.results).toEqual([]);
  });
});

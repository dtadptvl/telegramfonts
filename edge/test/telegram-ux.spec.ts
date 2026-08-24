import { afterEach, describe, expect, it } from 'vitest';
import {
  configureCustomerMenu,
  retireInteractiveMessage,
  TelegramClient,
} from '../src/services/telegram-client';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe('Telegram UX client contract', () => {
  it('registers exactly the Vietnamese customer commands and native commands menu', async () => {
    const calls: Array<{ path: string; body: Record<string, unknown> }> = [];
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      calls.push({
        path: new URL(url).pathname.split('/').pop() || '',
        body: JSON.parse(String(init?.body || '{}')) as Record<string, unknown>,
      });
      return new Response(JSON.stringify({ ok: true, result: true }), { status: 200 });
    };

    await configureCustomerMenu(new TelegramClient('test-token'));

    expect(calls).toEqual([
      {
        path: 'setMyCommands',
        body: {
          commands: [
            { command: 'trogiup', description: 'Trợ giúp' },
            { command: 'muahang', description: 'Mua hàng' },
          ],
        },
      },
      {
        path: 'setChatMenuButton',
        body: { menu_button: { type: 'commands' } },
      },
    ]);
  });

  it('falls back to removing inline controls when bot-message deletion is rejected', async () => {
    const paths: string[] = [];
    let markupBody: Record<string, unknown> | undefined;
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const path = new URL(url).pathname.split('/').pop() || '';
      paths.push(path);
      if (path === 'deleteMessage') {
        return new Response(JSON.stringify({ ok: false, description: 'message cannot be deleted' }), {
          status: 400,
        });
      }
      markupBody = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>;
      return new Response(JSON.stringify({ ok: true, result: true }), { status: 200 });
    };

    await retireInteractiveMessage(new TelegramClient('test-token'), 'chat-1', 42);

    expect(paths).toEqual(['deleteMessage', 'editMessageReplyMarkup']);
    expect(markupBody).toEqual({
      chat_id: 'chat-1',
      message_id: 42,
      reply_markup: { inline_keyboard: [] },
    });
  });
});

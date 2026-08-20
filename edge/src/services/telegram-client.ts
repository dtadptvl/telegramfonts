import type {
  SendMessageParams,
  EditMessageTextParams,
  AnswerCallbackQueryParams,
} from '../types/telegram';

export class TelegramApiError extends Error {
  constructor(public readonly statusCode: number, message: string) {
    super(message);
    this.name = 'TelegramApiError';
  }
}

export class TelegramClient {
  private readonly baseUrl: string;

  constructor(token: string) {
    this.baseUrl = `https://api.telegram.org/bot${token}`;
  }

  async sendMessage(params: SendMessageParams): Promise<{ message_id?: number }> {
    const payload = {
      ...params,
      parse_mode: 'HTML',
    };

    const res = await fetch(`${this.baseUrl}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      throw new TelegramApiError(
        res.status,
        `Telegram sendMessage failed with HTTP status ${res.status}`
      );
    }

    const data = (await res.json()) as { ok: boolean; result?: { message_id?: number } };
    return data.result || {};
  }

  async editMessageText(params: EditMessageTextParams): Promise<{ message_id?: number }> {
    const payload = {
      ...params,
      parse_mode: 'HTML',
    };

    const res = await fetch(`${this.baseUrl}/editMessageText`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      throw new TelegramApiError(
        res.status,
        `Telegram editMessageText failed with HTTP status ${res.status}`
      );
    }

    const data = (await res.json()) as { ok: boolean; result?: { message_id?: number } };
    return data.result || {};
  }

  async answerCallbackQuery(params: AnswerCallbackQueryParams): Promise<boolean> {
    const res = await fetch(`${this.baseUrl}/answerCallbackQuery`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });

    if (!res.ok) {
      throw new TelegramApiError(
        res.status,
        `Telegram answerCallbackQuery failed with HTTP status ${res.status}`
      );
    }

    const data = (await res.json()) as { ok: boolean; result?: boolean };
    return Boolean(data.result ?? data.ok);
  }
}

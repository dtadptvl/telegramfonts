import type {
  SendMessageParams,
  EditMessageTextParams,
  AnswerCallbackQueryParams,
  SendDocumentParams,
} from '../types/telegram';

export class TelegramApiError extends Error {
  constructor(
    public readonly statusCode: number,
    message: string,
    public readonly description?: string
  ) {
    super(message);
    this.name = 'TelegramApiError';
  }

  get isMessageNotModified(): boolean {
    return (
      this.statusCode === 400 &&
      typeof this.description === 'string' &&
      this.description.toLowerCase().includes('message is not modified')
    );
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
      let description: string | undefined;
      try {
        const errorBody = (await res.json()) as { description?: string };
        description = errorBody?.description;
      } catch {
        // Ignore JSON parse errors on non-2xx
      }

      throw new TelegramApiError(
        res.status,
        `Telegram sendMessage failed with HTTP status ${res.status}`,
        description
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
      let description: string | undefined;
      try {
        const errorBody = (await res.json()) as { description?: string };
        description = errorBody?.description;
      } catch {
        // Ignore JSON parse errors on non-2xx
      }

      // Treat Telegram confirmed no-op edit as success
      if (
        res.status === 400 &&
        description &&
        description.toLowerCase().includes('message is not modified')
      ) {
        return {};
      }

      throw new TelegramApiError(
        res.status,
        `Telegram editMessageText failed with HTTP status ${res.status}`,
        description
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
      let description: string | undefined;
      try {
        const errorBody = (await res.json()) as { description?: string };
        description = errorBody?.description;
      } catch {
        // Ignore JSON parse errors on non-2xx
      }

      throw new TelegramApiError(
        res.status,
        `Telegram answerCallbackQuery failed with HTTP status ${res.status}`,
        description
      );
    }

    const data = (await res.json()) as { ok: boolean; result?: boolean };
    return Boolean(data.result ?? data.ok);
  }

  async sendDocument(params: SendDocumentParams): Promise<{ message_id?: number }> {
    const formData = new FormData();
    formData.append('chat_id', params.chat_id.toString());
    const blob =
      params.document instanceof Blob
        ? params.document
        : new Blob([params.document], { type: 'application/zip' });
    formData.append('document', blob, params.filename);
    if (params.caption) {
      formData.append('caption', params.caption);
      formData.append('parse_mode', params.parse_mode || 'HTML');
    }

    const res = await fetch(`${this.baseUrl}/sendDocument`, {
      method: 'POST',
      body: formData,
    });

    if (!res.ok) {
      let description: string | undefined;
      try {
        const errorBody = (await res.json()) as { description?: string };
        description = errorBody?.description;
      } catch {
        // Ignore JSON parse errors on non-2xx
      }

      throw new TelegramApiError(
        res.status,
        `Telegram sendDocument failed with HTTP status ${res.status}`,
        description
      );
    }

    const data = (await res.json()) as { ok: boolean; result?: { message_id?: number } };
    return data.result || {};
  }
}


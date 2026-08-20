import type {
  SendMessageParams,
  EditMessageTextParams,
  AnswerCallbackQueryParams,
} from '../types/telegram';

export class TelegramClient {
  private readonly baseUrl: string;

  constructor(token: string) {
    this.baseUrl = `https://api.telegram.org/bot${token}`;
  }

  async sendMessage(params: SendMessageParams): Promise<Response> {
    const payload = {
      ...params,
      parse_mode: 'HTML',
    };

    return fetch(`${this.baseUrl}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  }

  async editMessageText(params: EditMessageTextParams): Promise<Response> {
    const payload = {
      ...params,
      parse_mode: 'HTML',
    };

    return fetch(`${this.baseUrl}/editMessageText`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  }

  async answerCallbackQuery(params: AnswerCallbackQueryParams): Promise<Response> {
    return fetch(`${this.baseUrl}/answerCallbackQuery`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });
  }
}

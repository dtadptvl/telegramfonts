import type { TelegramSessionRecord, SessionStatus, FontFormat } from '../types/session';
import type { TelegramUser } from '../types/telegram';

function generateShortToken(): string {
  return crypto.randomUUID().slice(0, 8);
}

function generateCheckoutToken(): string {
  return `chk_${crypto.randomUUID().replace(/-/g, '')}`;
}

export class SessionService {
  constructor(private readonly db: D1Database) {}

  async upsertTelegramUser(user: TelegramUser): Promise<void> {
    const now = Date.now();
    const userId = String(user.id);
    await this.db
      .prepare(
        `INSERT INTO telegram_users (id, username, first_name, last_name, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?)
         ON CONFLICT(id) DO UPDATE SET
           username = excluded.username,
           first_name = excluded.first_name,
           last_name = excluded.last_name,
           updated_at = excluded.updated_at`
      )
      .bind(
        userId,
        user.username || null,
        user.first_name,
        user.last_name || null,
        now,
        now
      )
      .run();
  }

  async getOrCreateSession(userId: string, chatId: string): Promise<TelegramSessionRecord> {
    const existing = await this.db
      .prepare('SELECT * FROM telegram_sessions WHERE user_id = ?')
      .bind(userId)
      .first<TelegramSessionRecord>();

    if (existing) {
      if (existing.chat_id !== chatId) {
        await this.db
          .prepare('UPDATE telegram_sessions SET chat_id = ?, updated_at = ? WHERE user_id = ?')
          .bind(chatId, Date.now(), userId)
          .run();
        existing.chat_id = chatId;
      }
      return existing;
    }

    const now = Date.now();
    const sessionId = crypto.randomUUID();
    const workflowToken = generateShortToken();
    const checkoutToken = generateCheckoutToken();
    const defaultFormats = JSON.stringify(['TTF']);

    await this.db
      .prepare(
        `INSERT INTO telegram_sessions (id, user_id, chat_id, workflow_token, checkout_token, catalog_id, selected_styles, selected_formats, last_message_id, status, active_order_id, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, NULL, '[]', ?, NULL, 'IDLE', NULL, ?, ?)`
      )
      .bind(sessionId, userId, chatId, workflowToken, checkoutToken, defaultFormats, now, now)
      .run();

    return {
      id: sessionId,
      user_id: userId,
      chat_id: chatId,
      workflow_token: workflowToken,
      checkout_token: checkoutToken,
      catalog_id: null,
      selected_styles: '[]',
      selected_formats: defaultFormats,
      last_message_id: null,
      status: 'IDLE',
      active_order_id: null,
      created_at: now,
      updated_at: now,
    };
  }

  async getSessionByUserId(userId: string): Promise<TelegramSessionRecord | null> {
    return this.db
      .prepare('SELECT * FROM telegram_sessions WHERE user_id = ?')
      .bind(userId)
      .first<TelegramSessionRecord>();
  }

  async resetSession(userId: string, chatId: string): Promise<TelegramSessionRecord> {
    const now = Date.now();
    const workflowToken = generateShortToken();
    const checkoutToken = generateCheckoutToken();
    const defaultFormats = JSON.stringify(['TTF']);

    await this.db
      .prepare(
        `UPDATE telegram_sessions SET
           chat_id = ?,
           workflow_token = ?,
           checkout_token = ?,
           catalog_id = NULL,
           selected_styles = '[]',
           selected_formats = ?,
           last_message_id = NULL,
           status = 'IDLE',
           active_order_id = NULL,
           updated_at = ?
         WHERE user_id = ?`
      )
      .bind(chatId, workflowToken, checkoutToken, defaultFormats, now, userId)
      .run();

    return this.getOrCreateSession(userId, chatId);
  }

  async updateSessionCatalog(
    userId: string,
    catalogId: string,
    initialStatus: SessionStatus = 'SELECTING_STYLES'
  ): Promise<void> {
    const now = Date.now();
    const workflowToken = generateShortToken();
    const checkoutToken = generateCheckoutToken();

    await this.db
      .prepare(
        `UPDATE telegram_sessions SET
           workflow_token = ?,
           checkout_token = ?,
           catalog_id = ?,
           selected_styles = '[]',
           selected_formats = '["TTF"]',
           status = ?,
           active_order_id = NULL,
           updated_at = ?
         WHERE user_id = ?`
      )
      .bind(workflowToken, checkoutToken, catalogId, initialStatus, now, userId)
      .run();
  }

  async toggleStyleSelection(userId: string, styleId: string): Promise<string[]> {
    const session = await this.getSessionByUserId(userId);
    if (!session) return [];

    let current: string[] = [];
    try {
      current = JSON.parse(session.selected_styles);
    } catch {
      current = [];
    }

    if (current.includes(styleId)) {
      current = current.filter((id) => id !== styleId);
    } else {
      current.push(styleId);
    }

    const now = Date.now();
    await this.db
      .prepare(
        'UPDATE telegram_sessions SET selected_styles = ?, updated_at = ? WHERE user_id = ?'
      )
      .bind(JSON.stringify(current), now, userId)
      .run();

    return current;
  }

  async setAllStyles(userId: string, allStyleIds: string[]): Promise<void> {
    const now = Date.now();
    await this.db
      .prepare(
        'UPDATE telegram_sessions SET selected_styles = ?, updated_at = ? WHERE user_id = ?'
      )
      .bind(JSON.stringify(allStyleIds), now, userId)
      .run();
  }

  async clearStyles(userId: string): Promise<void> {
    const now = Date.now();
    await this.db
      .prepare(
        'UPDATE telegram_sessions SET selected_styles = "[]", updated_at = ? WHERE user_id = ?'
      )
      .bind(now, userId)
      .run();
  }

  async toggleFormatSelection(userId: string, format: FontFormat): Promise<FontFormat[]> {
    const session = await this.getSessionByUserId(userId);
    if (!session) return ['TTF'];

    let current: FontFormat[] = [];
    try {
      current = JSON.parse(session.selected_formats);
    } catch {
      current = ['TTF'];
    }

    if (current.includes(format)) {
      current = current.filter((f) => f !== format);
    } else {
      current.push(format);
    }

    const now = Date.now();
    await this.db
      .prepare(
        'UPDATE telegram_sessions SET selected_formats = ?, updated_at = ? WHERE user_id = ?'
      )
      .bind(JSON.stringify(current), now, userId)
      .run();

    return current;
  }

  async setStatus(
    userId: string,
    status: SessionStatus,
    lastMessageId?: number
  ): Promise<void> {
    const now = Date.now();
    if (lastMessageId !== undefined) {
      await this.db
        .prepare(
          'UPDATE telegram_sessions SET status = ?, last_message_id = ?, updated_at = ? WHERE user_id = ?'
        )
        .bind(status, lastMessageId, now, userId)
        .run();
    } else {
      await this.db
        .prepare(
          'UPDATE telegram_sessions SET status = ?, updated_at = ? WHERE user_id = ?'
        )
        .bind(status, now, userId)
        .run();
    }
  }
}

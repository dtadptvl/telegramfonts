import type { TelegramSessionRecord, SessionStatus, FontFormat } from '../types/session';
import type { TelegramUser } from '../types/telegram';

export class SessionConflictError extends Error {
  constructor(message = 'Session state changed concurrently or is invalid') {
    super(message);
    this.name = 'SessionConflictError';
  }
}

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
        `INSERT INTO telegram_sessions (id, user_id, chat_id, workflow_token, checkout_token, version, catalog_id, selected_styles, selected_formats, last_message_id, status, active_order_id, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, 1, NULL, '[]', ?, NULL, 'IDLE', NULL, ?, ?)`
      )
      .bind(sessionId, userId, chatId, workflowToken, checkoutToken, defaultFormats, now, now)
      .run();

    return {
      id: sessionId,
      user_id: userId,
      chat_id: chatId,
      workflow_token: workflowToken,
      checkout_token: checkoutToken,
      version: 1,
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

  async resetSession(
    userId: string,
    chatId: string,
    updateId?: number
  ): Promise<TelegramSessionRecord> {
    const now = Date.now();
    const workflowToken = generateShortToken();
    const checkoutToken = generateCheckoutToken();
    const defaultFormats = JSON.stringify(['TTF']);

    const updateSessionStmt = this.db
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
           version = version + 1,
           updated_at = ?
         WHERE user_id = ?`
      )
      .bind(chatId, workflowToken, checkoutToken, defaultFormats, now, userId);

    if (updateId !== undefined) {
      const updateLedgerStmt = this.db
        .prepare(
          `UPDATE telegram_updates SET status = 'APPLIED', updated_at = ? WHERE update_id = ?`
        )
        .bind(now, updateId);

      await this.db.batch([updateSessionStmt, updateLedgerStmt]);
    } else {
      await updateSessionStmt.run();
    }

    return this.getOrCreateSession(userId, chatId);
  }

  async updateSessionCatalog(
    userId: string,
    catalogId: string,
    initialStatus: SessionStatus = 'SELECTING_STYLES',
    updateId?: number
  ): Promise<void> {
    const now = Date.now();
    const workflowToken = generateShortToken();
    const checkoutToken = generateCheckoutToken();

    const updateSessionStmt = this.db
      .prepare(
        `UPDATE telegram_sessions SET
           workflow_token = ?,
           checkout_token = ?,
           catalog_id = ?,
           selected_styles = '[]',
           selected_formats = '["TTF"]',
           status = ?,
           active_order_id = NULL,
           version = version + 1,
           updated_at = ?
         WHERE user_id = ?`
      )
      .bind(workflowToken, checkoutToken, catalogId, initialStatus, now, userId);

    if (updateId !== undefined) {
      const updateLedgerStmt = this.db
        .prepare(
          `UPDATE telegram_updates SET status = 'APPLIED', updated_at = ? WHERE update_id = ?`
        )
        .bind(now, updateId);

      await this.db.batch([updateSessionStmt, updateLedgerStmt]);
    } else {
      await updateSessionStmt.run();
    }
  }

  async toggleStyleSelection(
    userId: string,
    workflowToken: string,
    styleId: string,
    expectedVersion: number,
    updateId?: number
  ): Promise<string[]> {
    const session = await this.getSessionByUserId(userId);
    if (!session || session.workflow_token !== workflowToken || session.status !== 'SELECTING_STYLES') {
      throw new SessionConflictError('Invalid session state or token');
    }

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
    const updateSessionStmt = this.db
      .prepare(
        `UPDATE telegram_sessions SET
           selected_styles = ?,
           version = version + 1,
           updated_at = ?
         WHERE user_id = ? AND workflow_token = ? AND status = 'SELECTING_STYLES' AND version = ?`
      )
      .bind(JSON.stringify(current), now, userId, workflowToken, expectedVersion);

    if (updateId !== undefined) {
      const updateLedgerStmt = this.db
        .prepare(
          `UPDATE telegram_updates SET status = 'APPLIED', updated_at = ? WHERE update_id = ?`
        )
        .bind(now, updateId);

      const results = await this.db.batch([updateSessionStmt, updateLedgerStmt]);
      const sessionResult = results[0];
      if (!sessionResult.meta.changes || sessionResult.meta.changes === 0) {
        throw new SessionConflictError('Concurrent mutation conflict on style selection');
      }
    } else {
      const res = await updateSessionStmt.run();
      if (!res.meta.changes || res.meta.changes === 0) {
        throw new SessionConflictError('Concurrent mutation conflict on style selection');
      }
    }

    return current;
  }

  async setAllStyles(
    userId: string,
    workflowToken: string,
    allStyleIds: string[],
    expectedVersion: number,
    updateId?: number
  ): Promise<void> {
    const now = Date.now();
    const updateSessionStmt = this.db
      .prepare(
        `UPDATE telegram_sessions SET
           selected_styles = ?,
           version = version + 1,
           updated_at = ?
         WHERE user_id = ? AND workflow_token = ? AND status = 'SELECTING_STYLES' AND version = ?`
      )
      .bind(JSON.stringify(allStyleIds), now, userId, workflowToken, expectedVersion);

    if (updateId !== undefined) {
      const updateLedgerStmt = this.db
        .prepare(
          `UPDATE telegram_updates SET status = 'APPLIED', updated_at = ? WHERE update_id = ?`
        )
        .bind(now, updateId);

      const results = await this.db.batch([updateSessionStmt, updateLedgerStmt]);
      const sessionResult = results[0];
      if (!sessionResult.meta.changes || sessionResult.meta.changes === 0) {
        throw new SessionConflictError('Concurrent mutation conflict');
      }
    } else {
      const res = await updateSessionStmt.run();
      if (!res.meta.changes || res.meta.changes === 0) {
        throw new SessionConflictError('Concurrent mutation conflict');
      }
    }
  }

  async clearStyles(
    userId: string,
    workflowToken: string,
    expectedVersion: number,
    updateId?: number
  ): Promise<void> {
    const now = Date.now();
    const updateSessionStmt = this.db
      .prepare(
        `UPDATE telegram_sessions SET
           selected_styles = '[]',
           version = version + 1,
           updated_at = ?
         WHERE user_id = ? AND workflow_token = ? AND status = 'SELECTING_STYLES' AND version = ?`
      )
      .bind(now, userId, workflowToken, expectedVersion);

    if (updateId !== undefined) {
      const updateLedgerStmt = this.db
        .prepare(
          `UPDATE telegram_updates SET status = 'APPLIED', updated_at = ? WHERE update_id = ?`
        )
        .bind(now, updateId);

      const results = await this.db.batch([updateSessionStmt, updateLedgerStmt]);
      const sessionResult = results[0];
      if (!sessionResult.meta.changes || sessionResult.meta.changes === 0) {
        throw new SessionConflictError('Concurrent mutation conflict');
      }
    } else {
      const res = await updateSessionStmt.run();
      if (!res.meta.changes || res.meta.changes === 0) {
        throw new SessionConflictError('Concurrent mutation conflict');
      }
    }
  }

  async toggleFormatSelection(
    userId: string,
    workflowToken: string,
    format: FontFormat,
    expectedVersion: number,
    updateId?: number
  ): Promise<FontFormat[]> {
    const session = await this.getSessionByUserId(userId);
    if (!session || session.workflow_token !== workflowToken || session.status !== 'SELECTING_FORMATS') {
      throw new SessionConflictError('Invalid session state or token');
    }

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
    const updateSessionStmt = this.db
      .prepare(
        `UPDATE telegram_sessions SET
           selected_formats = ?,
           version = version + 1,
           updated_at = ?
         WHERE user_id = ? AND workflow_token = ? AND status = 'SELECTING_FORMATS' AND version = ?`
      )
      .bind(JSON.stringify(current), now, userId, workflowToken, expectedVersion);

    if (updateId !== undefined) {
      const updateLedgerStmt = this.db
        .prepare(
          `UPDATE telegram_updates SET status = 'APPLIED', updated_at = ? WHERE update_id = ?`
        )
        .bind(now, updateId);

      const results = await this.db.batch([updateSessionStmt, updateLedgerStmt]);
      const sessionResult = results[0];
      if (!sessionResult.meta.changes || sessionResult.meta.changes === 0) {
        throw new SessionConflictError('Concurrent mutation conflict on format selection');
      }
    } else {
      const res = await updateSessionStmt.run();
      if (!res.meta.changes || res.meta.changes === 0) {
        throw new SessionConflictError('Concurrent mutation conflict on format selection');
      }
    }

    return current;
  }

  async transitionStatus(
    userId: string,
    workflowToken: string,
    fromStatus: SessionStatus,
    toStatus: SessionStatus,
    expectedVersion: number,
    lastMessageId?: number,
    updateId?: number
  ): Promise<void> {
    const now = Date.now();
    const query =
      lastMessageId !== undefined
        ? `UPDATE telegram_sessions SET status = ?, last_message_id = ?, version = version + 1, updated_at = ?
           WHERE user_id = ? AND workflow_token = ? AND status = ? AND version = ?`
        : `UPDATE telegram_sessions SET status = ?, version = version + 1, updated_at = ?
           WHERE user_id = ? AND workflow_token = ? AND status = ? AND version = ?`;

    const updateSessionStmt =
      lastMessageId !== undefined
        ? this.db
            .prepare(query)
            .bind(toStatus, lastMessageId, now, userId, workflowToken, fromStatus, expectedVersion)
        : this.db
            .prepare(query)
            .bind(toStatus, now, userId, workflowToken, fromStatus, expectedVersion);

    if (updateId !== undefined) {
      const updateLedgerStmt = this.db
        .prepare(
          `UPDATE telegram_updates SET status = 'APPLIED', updated_at = ? WHERE update_id = ?`
        )
        .bind(now, updateId);

      const results = await this.db.batch([updateSessionStmt, updateLedgerStmt]);
      const sessionResult = results[0];
      if (!sessionResult.meta.changes || sessionResult.meta.changes === 0) {
        throw new SessionConflictError(`Cannot transition from ${fromStatus} to ${toStatus}: state conflict`);
      }
    } else {
      const res = await updateSessionStmt.run();
      if (!res.meta.changes || res.meta.changes === 0) {
        throw new SessionConflictError(`Cannot transition from ${fromStatus} to ${toStatus}: state conflict`);
      }
    }
  }

  async cancelSession(
    userId: string,
    workflowToken: string,
    expectedVersion: number,
    updateId?: number
  ): Promise<void> {
    const now = Date.now();
    const newWorkflowToken = generateShortToken();
    const newCheckoutToken = generateCheckoutToken();

    const updateSessionStmt = this.db
      .prepare(
        `UPDATE telegram_sessions SET
           workflow_token = ?,
           checkout_token = ?,
           catalog_id = NULL,
           selected_styles = '[]',
           selected_formats = '["TTF"]',
           status = 'IDLE',
           active_order_id = NULL,
           version = version + 1,
           updated_at = ?
         WHERE user_id = ? AND workflow_token = ? AND status IN ('SELECTING_STYLES', 'SELECTING_FORMATS', 'CONFIRMING') AND version = ?`
      )
      .bind(newWorkflowToken, newCheckoutToken, now, userId, workflowToken, expectedVersion);

    if (updateId !== undefined) {
      const updateLedgerStmt = this.db
        .prepare(
          `UPDATE telegram_updates SET status = 'APPLIED', updated_at = ? WHERE update_id = ?`
        )
        .bind(now, updateId);

      const results = await this.db.batch([updateSessionStmt, updateLedgerStmt]);
      const sessionResult = results[0];
      if (!sessionResult.meta.changes || sessionResult.meta.changes === 0) {
        throw new SessionConflictError('Cannot cancel inactive or completed session');
      }
    } else {
      const res = await updateSessionStmt.run();
      if (!res.meta.changes || res.meta.changes === 0) {
        throw new SessionConflictError('Cannot cancel inactive or completed session');
      }
    }
  }

  async setStatusUnconditional(
    userId: string,
    status: SessionStatus,
    lastMessageId?: number
  ): Promise<void> {
    const now = Date.now();
    if (lastMessageId !== undefined) {
      await this.db
        .prepare(
          'UPDATE telegram_sessions SET status = ?, last_message_id = ?, version = version + 1, updated_at = ? WHERE user_id = ?'
        )
        .bind(status, lastMessageId, now, userId)
        .run();
    } else {
      await this.db
        .prepare(
          'UPDATE telegram_sessions SET status = ?, version = version + 1, updated_at = ? WHERE user_id = ?'
        )
        .bind(status, now, userId)
        .run();
    }
  }

  async setLastMessageId(userId: string, lastMessageId: number | null): Promise<void> {
    await this.db
      .prepare('UPDATE telegram_sessions SET last_message_id = ?, updated_at = ? WHERE user_id = ?')
      .bind(lastMessageId, Date.now(), userId)
      .run();
  }
}

import { TelegramApiError, TelegramClient } from './telegram-client';

export const EPHEMERAL_TELEGRAM_MESSAGE_TTL_MS = 10 * 60 * 1000;
export const TELEGRAM_MESSAGE_CLEANUP_BATCH_SIZE = 100;

interface RetainedTelegramMessage {
  chat_id: string;
  message_id: number;
}

export interface TelegramMessageCleanupResult {
  attempted: number;
  removed: number;
  pending: number;
}

export class TelegramMessageRetentionService {
  constructor(private readonly db: D1Database) {}

  async scheduleEphemeralMessage(
    chatId: number | string,
    messageId: number,
    now = Date.now()
  ): Promise<void> {
    await this.db
      .prepare(
        `INSERT OR IGNORE INTO telegram_message_retention (chat_id, message_id, delete_at, created_at)
         VALUES (?, ?, ?, ?)`
      )
      .bind(String(chatId), messageId, now + EPHEMERAL_TELEGRAM_MESSAGE_TTL_MS, now)
      .run();
  }

  async deleteExpiredMessages(
    tg: TelegramClient,
    now = Date.now(),
    batchSize = TELEGRAM_MESSAGE_CLEANUP_BATCH_SIZE
  ): Promise<TelegramMessageCleanupResult> {
    const limit = Math.max(1, Math.min(TELEGRAM_MESSAGE_CLEANUP_BATCH_SIZE, Math.floor(batchSize)));
    const expired = await this.db
      .prepare(
        `SELECT chat_id, message_id
         FROM telegram_message_retention
         WHERE delete_at <= ?
         ORDER BY delete_at ASC, chat_id ASC, message_id ASC
         LIMIT ?`
      )
      .bind(now, limit)
      .all<RetainedTelegramMessage>();

    let removed = 0;
    let pending = 0;

    for (const message of expired.results || []) {
      let shouldRemove = false;
      try {
        shouldRemove = await tg.deleteMessage({
          chat_id: message.chat_id,
          message_id: message.message_id,
        });
      } catch (error: unknown) {
        // A message already gone is an idempotent cleanup success. Other
        // Telegram failures stay queued for the next minute tick.
        shouldRemove = isMessageAlreadyGone(error);
      }

      if (!shouldRemove) {
        pending++;
        continue;
      }

      await this.db
        .prepare('DELETE FROM telegram_message_retention WHERE chat_id = ? AND message_id = ?')
        .bind(message.chat_id, message.message_id)
        .run();
      removed++;
    }

    return {
      attempted: expired.results?.length || 0,
      removed,
      pending,
    };
  }
}

export function createRetentionAwareTelegramClient(
  token: string,
  db: D1Database
): TelegramClient {
  const retentionService = new TelegramMessageRetentionService(db);
  return new TelegramClient(token, {
    onEphemeralMessageSent: (chatId, messageId) =>
      retentionService.scheduleEphemeralMessage(chatId, messageId),
  });
}

function isMessageAlreadyGone(error: unknown): boolean {
  if (!(error instanceof TelegramApiError) || error.statusCode !== 400) return false;
  const description = (error.description || error.message).toLowerCase();
  return description.includes('message to delete not found') || description.includes('message is not found');
}

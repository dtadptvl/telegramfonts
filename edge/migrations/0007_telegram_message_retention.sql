-- Migration: 0007_telegram_message_retention.sql
-- Durable bounded cleanup queue for ephemeral bot-authored Telegram messages.

CREATE TABLE IF NOT EXISTS telegram_message_retention (
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    delete_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_telegram_message_retention_delete_at
    ON telegram_message_retention(delete_at);

-- Migration: 0002_telegram_sessions_and_catalog.sql
-- D1 Migration for Telegram users, updates dedupe, sessions, and font catalog metadata

-- Telegram updates deduplication table (crash & replay safety)
CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id INTEGER PRIMARY KEY,
    user_id TEXT,
    created_at INTEGER NOT NULL
);

-- Telegram users table
CREATE TABLE IF NOT EXISTS telegram_users (
    id TEXT PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Catalogs table
CREATE TABLE IF NOT EXISTS catalogs (
    id TEXT PRIMARY KEY,
    source_url TEXT NOT NULL UNIQUE,
    canonical_key TEXT NOT NULL UNIQUE,
    family_name TEXT NOT NULL,
    foundry TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_catalogs_canonical_key ON catalogs(canonical_key);

-- Catalog styles table
CREATE TABLE IF NOT EXISTS catalog_styles (
    id TEXT PRIMARY KEY,
    catalog_id TEXT NOT NULL REFERENCES catalogs(id) ON DELETE CASCADE,
    style_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    price INTEGER NOT NULL DEFAULT 50000,
    created_at INTEGER NOT NULL,
    UNIQUE(catalog_id, style_id)
);

CREATE INDEX IF NOT EXISTS idx_catalog_styles_catalog_id ON catalog_styles(catalog_id);

-- Catalog requests table (with UNIQUE user_id + canonical_key constraint for concurrent dedupe)
CREATE TABLE IF NOT EXISTS catalog_requests (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES telegram_users(id) ON DELETE CASCADE,
    canonical_key TEXT NOT NULL,
    source_url TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'COMPLETED', 'FAILED')),
    catalog_id TEXT REFERENCES catalogs(id) ON DELETE SET NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(user_id, canonical_key)
);

CREATE INDEX IF NOT EXISTS idx_catalog_requests_canonical_key ON catalog_requests(canonical_key);
CREATE INDEX IF NOT EXISTS idx_catalog_requests_user_id ON catalog_requests(user_id);

-- Telegram interactive sessions table (with workflow_token and checkout_token for callback & checkout safety)
CREATE TABLE IF NOT EXISTS telegram_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE REFERENCES telegram_users(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    workflow_token TEXT NOT NULL,
    checkout_token TEXT NOT NULL,
    catalog_id TEXT REFERENCES catalogs(id) ON DELETE SET NULL,
    selected_styles TEXT NOT NULL DEFAULT '[]',
    selected_formats TEXT NOT NULL DEFAULT '["TTF"]',
    last_message_id INTEGER,
    status TEXT NOT NULL CHECK(status IN ('IDLE', 'AWAITING_CATALOG', 'SELECTING_STYLES', 'SELECTING_FORMATS', 'CONFIRMING', 'ORDER_CREATED')),
    active_order_id TEXT REFERENCES orders(id) ON DELETE SET NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_telegram_sessions_user_id ON telegram_sessions(user_id);

-- Add checkout_token column to orders table for atomic checkout idempotency
ALTER TABLE orders ADD COLUMN checkout_token TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_checkout_token ON orders(checkout_token);

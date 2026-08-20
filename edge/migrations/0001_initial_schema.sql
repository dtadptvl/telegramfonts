-- Migration: 0001_initial_schema.sql
-- D1 Migration for telegramfonts control-plane

-- Orders table
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AWAITING_PAYMENT', 'PAID', 'PROCESSING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    total_amount INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'VND',
    metadata TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);

-- Order items table
CREATE TABLE IF NOT EXISTS order_items (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    font_id TEXT NOT NULL,
    font_name TEXT,
    price INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id);

-- Payments table (data-minimized: normalized audit & idempotency fields only)
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    provider TEXT NOT NULL,
    transaction_id TEXT NOT NULL UNIQUE,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'VND',
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id);
CREATE INDEX IF NOT EXISTS idx_payments_transaction_id ON payments(transaction_id);

-- Fulfillment jobs table (enforces 1:1 durable job per order + lease & retry tracking)
CREATE TABLE IF NOT EXISTS fulfillment_jobs (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE REFERENCES orders(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'PROCESSING', 'RETRY', 'COMPLETED', 'FAILED')),
    leased_at INTEGER,
    lease_expires_at INTEGER,
    lease_owner TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_retry_at INTEGER,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fulfillment_jobs_claim ON fulfillment_jobs(status, next_retry_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_fulfillment_jobs_order_id ON fulfillment_jobs(order_id);

-- Outbox events table
CREATE TABLE IF NOT EXISTS outbox_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'SENT', 'FAILED')),
    dispatched_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbox_events_dispatch ON outbox_events(status, created_at);

-- Artifacts table
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    job_id TEXT REFERENCES fulfillment_jobs(id) ON DELETE SET NULL,
    storage_key TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    file_size INTEGER,
    mime_type TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_order_id ON artifacts(order_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_job_id ON artifacts(job_id);

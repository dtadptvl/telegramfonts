-- Migration: 0005_r2_artifacts_and_delivery.sql
-- D1 Migration for Phase 6: Private R2 Artifacts, Fenced Completion Receipts, and Delivery

-- 1. Fulfillment receipts table: atomic completion guard and durable completion record
CREATE TABLE IF NOT EXISTS fulfillment_receipts (
    job_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE REFERENCES orders(id) ON DELETE RESTRICT,
    artifact_key TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    artifact_size_bytes INTEGER NOT NULL,
    worker_id TEXT NOT NULL,
    completed_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fulfillment_receipts_order_id ON fulfillment_receipts(order_id);

-- 2. Add artifact and completion columns to fulfillment_jobs
ALTER TABLE fulfillment_jobs ADD COLUMN artifact_key TEXT;
ALTER TABLE fulfillment_jobs ADD COLUMN artifact_sha256 TEXT;
ALTER TABLE fulfillment_jobs ADD COLUMN artifact_size_bytes INTEGER;
ALTER TABLE fulfillment_jobs ADD COLUMN completed_at INTEGER;

-- 3. Add completed_at column to orders
ALTER TABLE orders ADD COLUMN completed_at INTEGER;

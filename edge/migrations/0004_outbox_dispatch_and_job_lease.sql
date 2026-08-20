-- Migration: 0004_outbox_dispatch_and_job_lease.sql
-- D1 Migration for Outbox lease/dispatch tracking and fulfillment job fencing token

-- Add dispatch tracking columns to outbox_events
ALTER TABLE outbox_events ADD COLUMN dispatch_lease_token TEXT;
ALTER TABLE outbox_events ADD COLUMN dispatch_leased_at INTEGER;
ALTER TABLE outbox_events ADD COLUMN dispatch_lease_expires_at INTEGER;
ALTER TABLE outbox_events ADD COLUMN dispatch_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outbox_events ADD COLUMN next_dispatch_at INTEGER;
ALTER TABLE outbox_events ADD COLUMN last_dispatch_error TEXT;

CREATE INDEX IF NOT EXISTS idx_outbox_dispatch_claim ON outbox_events(status, next_dispatch_at, dispatch_lease_expires_at);

-- Add fencing lease token to fulfillment_jobs
ALTER TABLE fulfillment_jobs ADD COLUMN lease_token TEXT;

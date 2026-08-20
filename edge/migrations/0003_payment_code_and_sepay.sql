-- Migration: 0003_payment_code_and_sepay.sql
-- D1 Migration for durable payment code on orders and outbox deduplication

-- Add payment_code column to orders table
ALTER TABLE orders ADD COLUMN payment_code TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_code ON orders(payment_code);

-- Deduplication index on outbox_events for at-most-once event emission per aggregate & event type
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_events_dedupe ON outbox_events(aggregate_type, aggregate_id, event_type);

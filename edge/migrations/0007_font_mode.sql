-- Migration: 0007_font_mode.sql
-- D1 migration for the ORIGINAL/VIETNAMESE font mode product contract (T-PRICE-01).
--
-- mode is nullable by design: ABSENT mode = legacy order => fail closed at every
-- executable route (no ORIGINAL default, no automatic backfill, no silent inference).
-- Forward-safe & idempotent against the live schema (orders/telegram_sessions have no
-- mode column in migrations 0001-0006): ADD COLUMN with NULL-allowing CHECK constraints.

-- Mode selection column on interactive sessions (validated values: 'ORIGINAL'|'VIETNAMESE')
ALTER TABLE telegram_sessions ADD COLUMN mode TEXT CHECK (mode IS NULL OR mode IN ('ORIGINAL', 'VIETNAMESE'));

-- Transient storage for a MyFonts URL that arrived before mode selection (consumed on selection)
ALTER TABLE telegram_sessions ADD COLUMN pending_source_url TEXT;

-- Durable mode identity on orders (NULL = legacy order => fail-closed on every executable route)
ALTER TABLE orders ADD COLUMN mode TEXT CHECK (mode IS NULL OR mode IN ('ORIGINAL', 'VIETNAMESE'));
import { describe, it, expect, beforeAll } from 'vitest';
import { env } from 'cloudflare:test';

describe('D1 Database Schema & Constraints', () => {
  beforeAll(async () => {
    // Ensure foreign key constraints are active in SQLite session
    await env.DB.exec('PRAGMA foreign_keys = ON;');
  });

  describe('Migrations Tracker', () => {
    it('records 0001_initial_schema in d1_migrations table', async () => {
      const migration = await env.DB.prepare(
        'SELECT * FROM d1_migrations WHERE name = ?'
      )
        .bind('0001_initial_schema.sql')
        .first<{ id: number; name: string }>();

      expect(migration).not.toBeNull();
      expect(migration?.name).toBe('0001_initial_schema.sql');
    });
  });

  describe('Orders Table', () => {
    it('allows inserting orders with valid canonical states', async () => {
      const canonicalStates = [
        'AWAITING_PAYMENT',
        'PAID',
        'PROCESSING',
        'COMPLETED',
        'FAILED',
        'CANCELLED',
      ];

      const now = Date.now();
      for (const [index, status] of canonicalStates.entries()) {
        const orderId = `ord_test_${index}_${status.toLowerCase()}`;
        await env.DB.prepare(
          `INSERT INTO orders (id, user_id, status, total_amount, currency, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
        )
          .bind(orderId, `tg_user_${index}`, status, 50000, 'VND', JSON.stringify({ chat_id: 12345 }), now, now)
          .run();

        const inserted = await env.DB.prepare('SELECT * FROM orders WHERE id = ?')
          .bind(orderId)
          .first<{ id: string; status: string; total_amount: number }>();

        expect(inserted).not.toBeNull();
        expect(inserted?.id).toBe(orderId);
        expect(inserted?.status).toBe(status);
        expect(inserted?.total_amount).toBe(50000);
      }
    });

    it('rejects order with invalid state', async () => {
      const now = Date.now();
      await expect(
        env.DB.prepare(
          `INSERT INTO orders (id, user_id, status, total_amount, currency, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
        )
          .bind('ord_invalid_state', 'tg_user_bad', 'INVALID_STATE', 50000, 'VND', null, now, now)
          .run()
      ).rejects.toThrow();
    });
  });

  describe('Order Items Table & Foreign Key Cascade', () => {
    it('inserts order items and cascades on order deletion', async () => {
      const now = Date.now();
      const orderId = 'ord_cascade_test';
      await env.DB.prepare(
        `INSERT INTO orders (id, user_id, status, total_amount, currency, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      )
        .bind(orderId, 'tg_user_cascade', 'AWAITING_PAYMENT', 100000, 'VND', now, now)
        .run();

      const itemId = 'item_1';
      await env.DB.prepare(
        `INSERT INTO order_items (id, order_id, font_id, font_name, price, created_at)
         VALUES (?, ?, ?, ?, ?, ?)`
      )
        .bind(itemId, orderId, 'font_helvetica_viet', 'Helvetica Viet', 100000, now)
        .run();

      const itemBefore = await env.DB.prepare('SELECT * FROM order_items WHERE id = ?')
        .bind(itemId)
        .first();
      expect(itemBefore).not.toBeNull();

      // Delete the parent order
      await env.DB.prepare('DELETE FROM orders WHERE id = ?').bind(orderId).run();

      // Child order item should be deleted due to ON DELETE CASCADE
      const itemAfter = await env.DB.prepare('SELECT * FROM order_items WHERE id = ?')
        .bind(itemId)
        .first();
      expect(itemAfter).toBeNull();
    });
  });

  describe('Payments Table & Idempotency (Data Minimized)', () => {
    it('enforces unique transaction_id for payment idempotency without raw payload', async () => {
      const now = Date.now();
      const orderId = 'ord_payment_test';
      await env.DB.prepare(
        `INSERT INTO orders (id, user_id, status, total_amount, currency, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      )
        .bind(orderId, 'tg_user_pay', 'AWAITING_PAYMENT', 75000, 'VND', now, now)
        .run();

      const txnId = 'sepay_txn_12345678';
      await env.DB.prepare(
        `INSERT INTO payments (id, order_id, provider, transaction_id, amount, currency, status, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
      )
        .bind('pay_1', orderId, 'SEPAY', txnId, 75000, 'VND', 'SUCCESS', now, now)
        .run();

      const payment = await env.DB.prepare('SELECT * FROM payments WHERE transaction_id = ?')
        .bind(txnId)
        .first<{ id: string; provider: string; transaction_id: string; amount: number }>();

      expect(payment).not.toBeNull();
      expect(payment?.provider).toBe('SEPAY');
      expect(payment?.amount).toBe(75000);

      // Attempt duplicate transaction_id
      await expect(
        env.DB.prepare(
          `INSERT INTO payments (id, order_id, provider, transaction_id, amount, currency, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
        )
          .bind('pay_2', orderId, 'SEPAY', txnId, 75000, 'VND', 'SUCCESS', now, now)
          .run()
      ).rejects.toThrow();
    });
  });

  describe('Fulfillment Jobs Table, Lease/Retry & Uniqueness', () => {
    it('allows valid job states across distinct orders and queries claim/retry eligibility', async () => {
      const now = Date.now();
      const validJobStates = ['PENDING', 'PROCESSING', 'RETRY', 'COMPLETED', 'FAILED'];

      for (const [idx, state] of validJobStates.entries()) {
        const orderId = `ord_job_state_${idx}`;
        await env.DB.prepare(
          `INSERT INTO orders (id, user_id, status, total_amount, currency, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)`
        )
          .bind(orderId, `tg_user_job_${idx}`, 'PAID', 50000, 'VND', now, now)
          .run();

        const jobId = `job_${idx}_${state.toLowerCase()}`;
        const leasedAt = state === 'PROCESSING' ? now - 1000 : null;
        const leaseExpiresAt = state === 'PROCESSING' ? now + 60000 : null;
        const nextRetryAt = state === 'RETRY' ? now - 1000 : null;

        await env.DB.prepare(
          `INSERT INTO fulfillment_jobs (id, order_id, status, leased_at, lease_expires_at, next_retry_at, attempt_count, max_attempts, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
        )
          .bind(jobId, orderId, state, leasedAt, leaseExpiresAt, nextRetryAt, 0, 3, now, now)
          .run();
      }

      // Query jobs available for claim or retry:
      // 1. PENDING jobs
      // 2. RETRY jobs where next_retry_at <= now
      // 3. PROCESSING jobs where lease_expires_at < now (expired lease recovery)
      const claimableJobs = await env.DB.prepare(
        `SELECT * FROM fulfillment_jobs
         WHERE status = 'PENDING'
            OR (status = 'RETRY' AND next_retry_at <= ?)
            OR (status = 'PROCESSING' AND lease_expires_at < ?)`
      )
        .bind(now, now)
        .all();

      expect(claimableJobs.results.length).toBeGreaterThanOrEqual(2); // PENDING + RETRY
    });

    it('rejects duplicate fulfillment job for the same order (1:1 constraint)', async () => {
      const now = Date.now();
      const orderId = 'ord_single_job_test';
      await env.DB.prepare(
        `INSERT INTO orders (id, user_id, status, total_amount, currency, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      )
        .bind(orderId, 'tg_user_single', 'PAID', 50000, 'VND', now, now)
        .run();

      await env.DB.prepare(
        `INSERT INTO fulfillment_jobs (id, order_id, status, attempt_count, max_attempts, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      )
        .bind('job_first', orderId, 'PENDING', 0, 3, now, now)
        .run();

      // Second job for same order must fail with UNIQUE constraint error
      await expect(
        env.DB.prepare(
          `INSERT INTO fulfillment_jobs (id, order_id, status, attempt_count, max_attempts, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)`
        )
          .bind('job_second', orderId, 'PENDING', 0, 3, now, now)
          .run()
      ).rejects.toThrow();
    });

    it('rejects invalid job status', async () => {
      const now = Date.now();
      const orderId = 'ord_bad_status';
      await env.DB.prepare(
        `INSERT INTO orders (id, user_id, status, total_amount, currency, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      )
        .bind(orderId, 'tg_user_bad_job', 'PAID', 50000, 'VND', now, now)
        .run();

      await expect(
        env.DB.prepare(
          `INSERT INTO fulfillment_jobs (id, order_id, status, attempt_count, max_attempts, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)`
        )
          .bind('job_invalid', orderId, 'INVALID_JOB_STATE', 0, 3, now, now)
          .run()
      ).rejects.toThrow();
    });
  });

  describe('Outbox Events Table', () => {
    it('allows inserting pending outbox events and supports dispatch indexing', async () => {
      const now = Date.now();
      const eventId = 'evt_test_1';
      await env.DB.prepare(
        `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      )
        .bind(eventId, 'ORDER_PAID', 'ORDER', 'ord_123', JSON.stringify({ total: 50000 }), 'PENDING', now)
        .run();

      const pendingEvents = await env.DB.prepare(
        `SELECT * FROM outbox_events WHERE status = 'PENDING' ORDER BY created_at ASC LIMIT 10`
      ).all();

      expect(pendingEvents.results.some((e: Record<string, unknown>) => e.id === eventId)).toBe(true);
    });
  });

  describe('Artifacts Table', () => {
    it('stores artifact metadata and enforces unique storage_key', async () => {
      const now = Date.now();
      const orderId = 'ord_artifact_test';
      await env.DB.prepare(
        `INSERT INTO orders (id, user_id, status, total_amount, currency, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      )
        .bind(orderId, 'tg_user_art', 'PROCESSING', 50000, 'VND', now, now)
        .run();

      const key = 'artifacts/ord_artifact_test/font.zip';
      await env.DB.prepare(
        `INSERT INTO artifacts (id, order_id, storage_key, file_name, file_size, mime_type, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      )
        .bind('art_1', orderId, key, 'font.zip', 102400, 'application/zip', now)
        .run();

      // Duplicate storage_key must fail
      await expect(
        env.DB.prepare(
          `INSERT INTO artifacts (id, order_id, storage_key, file_name, file_size, mime_type, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)`
        )
          .bind('art_2', orderId, key, 'duplicate.zip', 204800, 'application/zip', now)
          .run()
      ).rejects.toThrow();
    });
  });
});

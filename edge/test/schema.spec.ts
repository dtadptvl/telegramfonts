import { describe, it, expect, beforeAll } from 'vitest';
import { env } from 'cloudflare:test';

describe('D1 Database Schema & Constraints', () => {
  beforeAll(async () => {
    // Ensure foreign key constraints are active in SQLite session
    await env.DB.exec('PRAGMA foreign_keys = ON;');
  });

  describe('Migrations Tracker', () => {
    it('records migrations in d1_migrations table', async () => {
      const migrations = await env.DB.prepare(
        'SELECT name FROM d1_migrations ORDER BY name ASC'
      ).all<{ name: string }>();

      expect(migrations.results.some((m) => m.name === '0001_initial_schema.sql')).toBe(true);
      expect(
        migrations.results.some((m) => m.name === '0002_telegram_sessions_and_catalog.sql')
      ).toBe(true);
      expect(
        migrations.results.some((m) => m.name === '0003_payment_code_and_sepay.sql')
      ).toBe(true);
      expect(
        migrations.results.some((m) => m.name === '0004_outbox_dispatch_and_job_lease.sql')
      ).toBe(true);
    });
  });

  describe('Orders Table & Checkout Idempotency', () => {
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
          `INSERT INTO orders (id, user_id, status, total_amount, currency, metadata, checkout_token, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
        )
          .bind(
            orderId,
            `tg_user_${index}`,
            status,
            50000,
            'VND',
            JSON.stringify({ chat_id: 12345 }),
            `chk_test_${index}`,
            now,
            now
          )
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

    it('enforces unique checkout_token on orders for idempotency', async () => {
      const now = Date.now();
      const chkToken = 'chk_unique_test_123';

      await env.DB.prepare(
        `INSERT INTO orders (id, user_id, status, total_amount, currency, checkout_token, created_at, updated_at)
         VALUES (?, ?, 'AWAITING_PAYMENT', 50000, 'VND', ?, ?, ?)`
      )
        .bind('ord_chk_1', 'tg_user_chk', chkToken, now, now)
        .run();

      await expect(
        env.DB.prepare(
          `INSERT INTO orders (id, user_id, status, total_amount, currency, checkout_token, created_at, updated_at)
           VALUES (?, ?, 'AWAITING_PAYMENT', 50000, 'VND', ?, ?, ?)`
        )
          .bind('ord_chk_2', 'tg_user_chk', chkToken, now, now)
          .run()
      ).rejects.toThrow();
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

      const claimableJobs = await env.DB.prepare(
        `SELECT * FROM fulfillment_jobs
         WHERE status = 'PENDING'
            OR (status = 'RETRY' AND next_retry_at <= ?)
            OR (status = 'PROCESSING' AND lease_expires_at < ?)`
      )
        .bind(now, now)
        .all();

      expect(claimableJobs.results.length).toBeGreaterThanOrEqual(2);
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

      await expect(
        env.DB.prepare(
          `INSERT INTO fulfillment_jobs (id, order_id, status, attempt_count, max_attempts, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)`
        )
          .bind('job_second', orderId, 'PENDING', 0, 3, now, now)
          .run()
      ).rejects.toThrow();
    });
  });

  describe('Telegram Users & Sessions Schema (Migration 0002)', () => {
    it('stores telegram users and enforces unique session per user', async () => {
      const now = Date.now();
      const userId = 'tg_user_100';

      await env.DB.prepare(
        `INSERT INTO telegram_users (id, username, first_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)`
      )
        .bind(userId, 'tester1', 'Test', now, now)
        .run();

      await env.DB.prepare(
        `INSERT INTO telegram_sessions (id, user_id, chat_id, workflow_token, checkout_token, status, created_at, updated_at)
         VALUES (?, ?, ?, 'tok1', 'chk1', 'IDLE', ?, ?)`
      )
        .bind('sess_1', userId, 'chat_100', now, now)
        .run();

      // Duplicate session for same user must fail
      await expect(
        env.DB.prepare(
          `INSERT INTO telegram_sessions (id, user_id, chat_id, workflow_token, checkout_token, status, created_at, updated_at)
           VALUES (?, ?, ?, 'tok2', 'chk2', 'IDLE', ?, ?)`
        )
          .bind('sess_2', userId, 'chat_100', now, now)
          .run()
      ).rejects.toThrow();
    });

    it('enforces unique (user_id, canonical_key) on catalog_requests', async () => {
      const now = Date.now();
      const userId = 'tg_user_req_100';

      await env.DB.prepare(
        `INSERT INTO telegram_users (id, username, first_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)`
      )
        .bind(userId, 'tester_req', 'TestReq', now, now)
        .run();

      await env.DB.prepare(
        `INSERT INTO catalog_requests (id, user_id, canonical_key, source_url, status, created_at, updated_at)
         VALUES (?, ?, ?, ?, 'PENDING', ?, ?)`
      )
        .bind(
          'req_1',
          userId,
          'myfonts:collections/test-req-font',
          'https://www.myfonts.com/collections/test-req-font',
          now,
          now
        )
        .run();

      // Duplicate request for same user and canonical key must fail
      await expect(
        env.DB.prepare(
          `INSERT INTO catalog_requests (id, user_id, canonical_key, source_url, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'PENDING', ?, ?)`
        )
          .bind(
            'req_2',
            userId,
            'myfonts:collections/test-req-font',
            'https://www.myfonts.com/collections/test-req-font',
            now,
            now
          )
          .run()
      ).rejects.toThrow();
    });

    it('stores catalogs and cascades styles deletion', async () => {
      const now = Date.now();
      const catId = 'cat_test_cascade';

      await env.DB.prepare(
        `INSERT INTO catalogs (id, source_url, canonical_key, family_name, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?)`
      )
        .bind(
          catId,
          'https://www.myfonts.com/collections/test-font',
          'myfonts:collections/test-font',
          'Test Font',
          now,
          now
        )
        .run();

      await env.DB.prepare(
        `INSERT INTO catalog_styles (id, catalog_id, style_id, display_name, price, created_at)
         VALUES (?, ?, ?, ?, ?, ?)`
      )
        .bind('cstyle_1', catId, 'regular', 'Regular', 50000, now)
        .run();

      // Delete catalog -> style should cascade
      await env.DB.prepare('DELETE FROM catalogs WHERE id = ?').bind(catId).run();

      const style = await env.DB.prepare('SELECT * FROM catalog_styles WHERE id = ?')
        .bind('cstyle_1')
        .first();
      expect(style).toBeNull();
    });
  });
});

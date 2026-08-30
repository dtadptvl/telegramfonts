import { describe, it, expect } from 'vitest';
import { env } from 'cloudflare:test';
import { SessionService, SessionConflictError } from '../src/services/session-service';
import { OrderService } from '../src/services/order-service';
import {
  JobService,
  MODE_ABSENT_LEGACY_ORDER_REASON,
  MODE_IDENTITY_MISMATCH_REASON,
} from '../src/services/job-service';
import { MODE_PRICE_PER_STYLE_VND, stylePriceForMode } from '../src/utils/pricing';
import type { FontCatalog } from '../src/types/catalog';
import type { FontMode } from '../src/types/session';

/**
 * T-PRICE-01 (contract E-00012-D / ADR-0002): ORIGINAL vs VIETNAMESE font mode.
 * Mode is durable order identity, bound end-to-end:
 * session selectMode -> orders row/metadata -> claim payload -> delivery evidence.
 * Pricing: ORIGINAL 5,000 VND/style; VIETNAMESE 8,000 VND/style and never the
 * 5,000 catalog default. Absent or divergent mode fails closed everywhere.
 */

// Styles without an explicit catalog price take the mode rate; rt_bold carries
// a 5,000 VND catalog price that VIETNAMESE must never fall back to.
const testCatalog: FontCatalog = {
  sourceUrl: 'https://www.myfonts.com/collections/roboto-test',
  canonicalKey: 'collections/roboto-test',
  familyName: 'Roboto Test',
  foundry: 'Test Foundry',
  styles: [
    { id: 'rt_regular', displayName: 'Regular' },
    { id: 'rt_bold', displayName: 'Bold', price: 5000 },
    { id: 'rt_italic', displayName: 'Italic' },
  ],
};

let userSeq = 710000;

async function prepareCheckoutSession(mode: FontMode | null) {
  const userId = String(userSeq++);
  const sessionService = new SessionService(env.DB);
  await sessionService.upsertTelegramUser({ id: Number(userId), is_bot: false, first_name: 'ModeUser' });
  const created = await sessionService.getOrCreateSession(userId, userId);
  expect(created.mode).toBeNull();

  if (mode !== null) {
    await sessionService.selectMode(userId, created.workflow_token, mode, created.version);
  }

  await sessionService.setStatusUnconditional(userId, 'SELECTING_STYLES');
  let session = await sessionService.getSessionByUserId(userId);
  expect(session).not.toBeNull();
  await sessionService.setAllStyles(
    userId,
    session!.workflow_token,
    testCatalog.styles.map((style) => style.id),
    session!.version
  );
  await sessionService.setStatusUnconditional(userId, 'CONFIRMING');
  session = await sessionService.getSessionByUserId(userId);
  return { userId, sessionService, session: session! };
}

async function setupModeJob(opts: { orderMode: string | null; metadataMode?: string }) {
  const orderId = `ord_mode_${crypto.randomUUID().replace(/-/g, '')}`;
  const jobId = `job_mode_${crypto.randomUUID().replace(/-/g, '')}`;
  const now = Date.now();

  const metadata: Record<string, unknown> = {
    source_url: 'https://www.myfonts.com/collections/roboto-test',
    family_name: 'Roboto Test',
    foundry: 'Test Foundry',
    selected_formats: ['TTF', 'OTF'],
  };
  if (opts.metadataMode !== undefined) {
    metadata.mode = opts.metadataMode;
  }

  await env.DB.prepare(
    `INSERT INTO orders (id, user_id, status, total_amount, currency, metadata, mode, created_at, updated_at)
     VALUES (?, ?, 'PAID', 15000, 'VND', ?, ?, ?, ?)`
  )
    .bind(orderId, 'user_mode_test', JSON.stringify(metadata), opts.orderMode, now, now)
    .run();

  await env.DB.prepare(
    `INSERT INTO order_items (id, order_id, font_id, font_name, price, created_at)
     VALUES (?, ?, 'rt_regular', 'Roboto Test Regular', 5000, ?)`
  )
    .bind(`item_mode_${crypto.randomUUID().replace(/-/g, '')}`, orderId, now)
    .run();

  await env.DB.prepare(
    `INSERT INTO fulfillment_jobs (id, order_id, status, attempt_count, max_attempts, created_at, updated_at)
     VALUES (?, ?, 'PENDING', 0, 3, ?, ?)`
  )
    .bind(jobId, orderId, now, now)
    .run();

  return { orderId, jobId };
}

describe('T-PRICE-01: mode selection and durable persistence (session -> order)', () => {
  it('selectMode persists the explicit mode on the session and consumes pending_source_url atomically', async () => {
    const userId = String(userSeq++);
    const sessionService = new SessionService(env.DB);
    await sessionService.upsertTelegramUser({ id: Number(userId), is_bot: false, first_name: 'ModeSelect' });
    const s0 = await sessionService.getOrCreateSession(userId, userId);
    expect(s0.mode).toBeNull();
    expect(s0.pending_source_url).toBeNull();

    await sessionService.setPendingSourceUrl(userId, 'https://www.myfonts.com/collections/roboto-test');
    const withPending = await sessionService.getSessionByUserId(userId);
    expect(withPending?.pending_source_url).toBe('https://www.myfonts.com/collections/roboto-test');

    await sessionService.selectMode(userId, s0.workflow_token, 'VIETNAMESE', s0.version);

    const after = await sessionService.getSessionByUserId(userId);
    expect(after?.mode).toBe('VIETNAMESE');
    expect(after?.pending_source_url).toBeNull();
    expect(after?.version).toBe(s0.version + 1);
  });

  it('selectMode fails closed on stale token/version and cannot overwrite an existing mode', async () => {
    const userId = String(userSeq++);
    const sessionService = new SessionService(env.DB);
    await sessionService.upsertTelegramUser({ id: Number(userId), is_bot: false, first_name: 'ModeConflict' });
    const s0 = await sessionService.getOrCreateSession(userId, userId);

    await expect(
      sessionService.selectMode(userId, 'wrong_token', 'ORIGINAL', s0.version)
    ).rejects.toThrow(SessionConflictError);
    await expect(
      sessionService.selectMode(userId, s0.workflow_token, 'ORIGINAL', s0.version + 99)
    ).rejects.toThrow(SessionConflictError);

    await sessionService.selectMode(userId, s0.workflow_token, 'ORIGINAL', s0.version);

    // Mode is a one-shot durable selection per flow: re-selection fails closed.
    const s1 = await sessionService.getSessionByUserId(userId);
    await expect(
      sessionService.selectMode(userId, s1!.workflow_token, 'VIETNAMESE', s1!.version)
    ).rejects.toThrow(SessionConflictError);

    const s2 = await sessionService.getSessionByUserId(userId);
    expect(s2?.mode).toBe('ORIGINAL');
  });

  it('createOrderFromSession carries the selected mode into orders.mode column and metadata for ORIGINAL and VIETNAMESE', async () => {
    for (const mode of ['ORIGINAL', 'VIETNAMESE'] as FontMode[]) {
      const { session } = await prepareCheckoutSession(mode);
      const orderService = new OrderService(env.DB);

      const result = await orderService.createOrderFromSession(session, testCatalog);
      expect(result.isExisting).toBe(false);
      expect(result.currency).toBe('VND');

      const row = await env.DB.prepare(
        'SELECT mode, metadata, status, total_amount FROM orders WHERE id = ?'
      )
        .bind(result.orderId)
        .first<{ mode: string | null; metadata: string; status: string; total_amount: number }>();

      expect(row?.status).toBe('AWAITING_PAYMENT');
      expect(row?.mode).toBe(mode);
      const meta = JSON.parse(row?.metadata || '{}') as Record<string, unknown>;
      expect(meta.mode).toBe(mode);
      expect(row?.total_amount).toBe(result.totalAmount);
    }
  });

  it('createOrderFromSession fails closed when session mode is absent: no ORIGINAL default, no order created', async () => {
    const { userId, session } = await prepareCheckoutSession(null);
    const orderService = new OrderService(env.DB);

    await expect(orderService.createOrderFromSession(session, testCatalog)).rejects.toThrow(
      SessionConflictError
    );

    const count = await env.DB.prepare('SELECT count(*) AS count FROM orders WHERE user_id = ?')
      .bind(userId)
      .first<{ count: number }>();
    expect(count?.count).toBe(0);
  });
});

describe('T-PRICE-01: mode-bound pricing (pricing.ts semantics)', () => {
  it('unit rates: ORIGINAL 5,000 VND/style, VIETNAMESE 8,000 VND/style; VIETNAMESE never falls back to 5,000', () => {
    expect(MODE_PRICE_PER_STYLE_VND.ORIGINAL).toBe(5000);
    expect(MODE_PRICE_PER_STYLE_VND.VIETNAMESE).toBe(8000);

    // ORIGINAL honors the 5,000 catalog default and explicit overrides.
    expect(stylePriceForMode(undefined, 'ORIGINAL')).toBe(5000);
    expect(stylePriceForMode(null, 'ORIGINAL')).toBe(5000);
    expect(stylePriceForMode(12000, 'ORIGINAL')).toBe(12000);

    // VIETNAMESE never falls back to the 5,000 catalog/default rate.
    expect(stylePriceForMode(undefined, 'VIETNAMESE')).toBe(8000);
    expect(stylePriceForMode(null, 'VIETNAMESE')).toBe(8000);
    expect(stylePriceForMode(5000, 'VIETNAMESE')).toBe(8000);
    // Explicit catalog overrides exceeding the VIETNAMESE rate are preserved.
    expect(stylePriceForMode(12000, 'VIETNAMESE')).toBe(12000);
  });

  it('multi-style order totals: ORIGINAL 3 styles = 15,000 VND vs VIETNAMESE 3 styles = 24,000 VND, per-item prices mode-bound', async () => {
    const orderService = new OrderService(env.DB);

    const original = await prepareCheckoutSession('ORIGINAL');
    const originalResult = await orderService.createOrderFromSession(original.session, testCatalog);
    expect(originalResult.totalAmount).toBe(15000);
    expect(originalResult.itemsCount).toBe(3);

    const vietnamese = await prepareCheckoutSession('VIETNAMESE');
    const vietnameseResult = await orderService.createOrderFromSession(vietnamese.session, testCatalog);
    expect(vietnameseResult.totalAmount).toBe(24000);
    expect(vietnameseResult.itemsCount).toBe(3);

    const originalItems = await env.DB.prepare(
      'SELECT price FROM order_items WHERE order_id = ? ORDER BY font_id ASC'
    )
      .bind(originalResult.orderId)
      .all<{ price: number }>();
    expect(originalItems.results.map((i) => i.price)).toEqual([5000, 5000, 5000]);

    // rt_bold carries a 5,000 catalog price: VIETNAMESE must bill 8,000, never 5,000.
    const vietnameseItems = await env.DB.prepare(
      'SELECT price FROM order_items WHERE order_id = ? ORDER BY font_id ASC'
    )
      .bind(vietnameseResult.orderId)
      .all<{ price: number }>();
    expect(vietnameseItems.results.map((i) => i.price)).toEqual([8000, 8000, 8000]);

    const totals = await env.DB.prepare(
      'SELECT id, total_amount, currency FROM orders WHERE id IN (?, ?)'
    )
      .bind(originalResult.orderId, vietnameseResult.orderId)
      .all<{ id: string; total_amount: number; currency: string }>();
    const byId = new Map(totals.results.map((r) => [r.id, r]));
    expect(byId.get(originalResult.orderId)?.total_amount).toBe(15000);
    expect(byId.get(vietnameseResult.orderId)?.total_amount).toBe(24000);
    expect(byId.get(originalResult.orderId)?.currency).toBe('VND');
    expect(byId.get(vietnameseResult.orderId)?.currency).toBe('VND');
  });
});

describe('T-PRICE-01: claim payload carries mode; identity violations fail closed', () => {
  it('claimJob payload carries ORIGINAL for ORIGINAL orders', async () => {
    const { jobId } = await setupModeJob({ orderMode: 'ORIGINAL', metadataMode: 'ORIGINAL' });
    const jobService = new JobService(env.DB);

    const claim = await jobService.claimJob(jobId, 'worker-mode-1', 300);
    expect(claim.status).toBe('CLAIMED');
    expect(claim.payload).toBeDefined();
    expect(claim.payload!.job_id).toBe(jobId);
    expect(claim.payload!.mode).toBe('ORIGINAL');
    expect(claim.payload!.formats).toEqual(['TTF', 'OTF']);
    expect(claim.payload!.source_url).toBe('https://www.myfonts.com/collections/roboto-test');
  });

  it('claimJob payload carries VIETNAMESE for VIETNAMESE orders', async () => {
    const { jobId, orderId } = await setupModeJob({ orderMode: 'VIETNAMESE', metadataMode: 'VIETNAMESE' });
    const jobService = new JobService(env.DB);

    const claim = await jobService.claimJob(jobId, 'worker-mode-2', 300);
    expect(claim.status).toBe('CLAIMED');
    expect(claim.payload!.mode).toBe('VIETNAMESE');

    const order = await env.DB.prepare('SELECT status, mode FROM orders WHERE id = ?')
      .bind(orderId)
      .first<{ status: string; mode: string | null }>();
    expect(order?.status).toBe('PROCESSING');
    expect(order?.mode).toBe('VIETNAMESE');
  });

  it('fails closed with MODE_IDENTITY_MISMATCH when the order mode column and metadata mirror diverge', async () => {
    const { jobId, orderId } = await setupModeJob({ orderMode: 'VIETNAMESE', metadataMode: 'ORIGINAL' });
    const jobService = new JobService(env.DB);

    const claim = await jobService.claimJob(jobId, 'worker-mode-3', 300);
    expect(claim.status).toBe('TERMINAL');
    expect(claim.queue_action).toBe('ack');
    expect(claim.reason).toBe(MODE_IDENTITY_MISMATCH_REASON);
    expect(claim.payload).toBeUndefined();

    const job = await env.DB.prepare('SELECT status, last_error FROM fulfillment_jobs WHERE id = ?')
      .bind(jobId)
      .first<{ status: string; last_error: string | null }>();
    expect(job?.status).toBe('FAILED');
    expect(job?.last_error).toBe(MODE_IDENTITY_MISMATCH_REASON);

    const order = await env.DB.prepare('SELECT status, mode FROM orders WHERE id = ?')
      .bind(orderId)
      .first<{ status: string; mode: string | null }>();
    expect(order?.status).toBe('FAILED');
    // Divergence is terminal: it is never silently resolved or rewritten.
    expect(order?.mode).toBe('VIETNAMESE');
  });

  it('absent-mode legacy order fails closed on claim: TERMINAL ack with MODE_ABSENT_LEGACY_ORDER, FAILED job+order, no defaulting, no backfill', async () => {
    const { jobId, orderId } = await setupModeJob({ orderMode: null });
    const jobService = new JobService(env.DB);

    const claim = await jobService.claimJob(jobId, 'worker-mode-4', 300);
    expect(claim.status).toBe('TERMINAL');
    expect(claim.queue_action).toBe('ack');
    expect(claim.reason).toBe(MODE_ABSENT_LEGACY_ORDER_REASON);
    expect(claim.payload).toBeUndefined();

    const job = await env.DB.prepare('SELECT status, last_error FROM fulfillment_jobs WHERE id = ?')
      .bind(jobId)
      .first<{ status: string; last_error: string | null }>();
    expect(job?.status).toBe('FAILED');
    expect(job?.last_error).toBe(MODE_ABSENT_LEGACY_ORDER_REASON);

    const order = await env.DB.prepare('SELECT status, mode, metadata FROM orders WHERE id = ?')
      .bind(orderId)
      .first<{ status: string; mode: string | null; metadata: string }>();
    expect(order?.status).toBe('FAILED');
    // No ORIGINAL defaulting and no automatic mode backfill on legacy rows.
    expect(order?.mode).toBeNull();
    const meta = JSON.parse(order?.metadata || '{}') as Record<string, unknown>;
    expect(meta.mode).toBeUndefined();
  });
});
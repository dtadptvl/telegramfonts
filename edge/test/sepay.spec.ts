import { describe, it, expect } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker, { type Env } from '../src/index';
import { CatalogService } from '../src/services/catalog-service';
import { SessionService } from '../src/services/session-service';
import { OrderService } from '../src/services/order-service';
import { PaymentService } from '../src/services/payment-service';
import {
  generatePaymentCode,
  validatePaymentCodePrefix,
  generateVietQrUrl,
} from '../src/utils/vietqr';
import type { FontCatalog } from '../src/types/catalog';
import type { TelegramUpdate } from '../src/types/telegram';

const BOT_TOKEN = 'mock_bot_token_sepay';
const TELEGRAM_SECRET = 'super_secret_telegram_token';
const SEPAY_SECRET = 'sepay_webhook_secret_key_123';
const BANK_ACCOUNT = '0000123456789';
const BANK_ID = 'MB';

const testEnv: Env = {
  ...(env as unknown as Env),
  TELEGRAM_BOT_TOKEN: BOT_TOKEN,
  TELEGRAM_WEBHOOK_SECRET: TELEGRAM_SECRET,
  SEPAY_WEBHOOK_SECRET: SEPAY_SECRET,
  BANK_ID: BANK_ID,
  BANK_ACCOUNT_NUMBER: BANK_ACCOUNT,
  BANK_ACCOUNT_NAME: 'TELEFONT STORE',
  PAYMENT_CODE_PREFIX: 'TF',
};

const sampleCatalog: FontCatalog = {
  sourceUrl: 'https://www.myfonts.com/collections/roboto-flex',
  canonicalKey: 'myfonts:collections/roboto-flex',
  familyName: 'Roboto Flex',
  styles: [
    { id: 'rf_regular', displayName: 'Roboto Flex Regular', price: 50000 },
    { id: 'rf_bold', displayName: 'Roboto Flex Bold', price: 50000 },
  ],
};

async function generateSePaySignature(
  secret: string,
  timestamp: string | number,
  body: string
): Promise<string> {
  const encoder = new TextEncoder();
  const dataToSign = encoder.encode(`${timestamp}.${body}`);
  const keyData = encoder.encode(secret);

  const cryptoKey = await crypto.subtle.importKey(
    'raw',
    keyData,
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );

  const signatureBuffer = await crypto.subtle.sign('HMAC', cryptoKey, dataToSign);
  const signatureArray = Array.from(new Uint8Array(signatureBuffer));
  return signatureArray.map((b) => b.toString(16).padStart(2, '0')).join('');
}

describe('Phase 3: SePay Verified Payment & Transactional Outbox', () => {
  describe('VietQR & Payment Code Utilities (Gap 2)', () => {
    it('defaults to TF prefix when unset or empty', () => {
      expect(validatePaymentCodePrefix(undefined as any)).toBe('TF');
      expect(validatePaymentCodePrefix(null as any)).toBe('TF');
      expect(validatePaymentCodePrefix('')).toBe('TF');

      const code = generatePaymentCode();
      expect(code).toMatch(/^TF[2-9A-Z]{6}$/);
    });

    it('accepts valid 2-5 character uppercase/alphanumeric prefixes', () => {
      expect(validatePaymentCodePrefix('tf')).toBe('TF');
      expect(validatePaymentCodePrefix('TELE')).toBe('TELE');
      expect(validatePaymentCodePrefix('A12B')).toBe('A12B');
      expect(validatePaymentCodePrefix('TF123')).toBe('TF123');

      const codeCustom = generatePaymentCode('TELE');
      expect(codeCustom).toMatch(/^TELE[2-9A-Z]{6}$/);
    });

    it('strictly rejects invalid payment code prefixes (too short, too long, invalid charset)', () => {
      // Too short (< 2 chars)
      expect(() => validatePaymentCodePrefix('A')).toThrow(/Invalid payment code prefix/);

      // Too long (> 5 chars)
      expect(() => validatePaymentCodePrefix('TOOLONG')).toThrow(/Invalid payment code prefix/);

      // Invalid characters
      expect(() => validatePaymentCodePrefix('TF!')).toThrow(/Invalid payment code prefix/);
      expect(() => validatePaymentCodePrefix('TF @')).toThrow(/Invalid payment code prefix/);
      expect(() => validatePaymentCodePrefix('TF-1')).toThrow(/Invalid payment code prefix/);
    });

    it('generates unique uppercase alphanumeric payment codes with configured prefix', () => {
      const code1 = generatePaymentCode('TF');
      const code2 = generatePaymentCode('TF');

      expect(code1).toMatch(/^TF[2-9A-Z]{6}$/);
      expect(code2).toMatch(/^TF[2-9A-Z]{6}$/);
      expect(code1).not.toBe(code2);
    });

    it('generates deterministic VietQR URL with exact amount, code, and bank details', () => {
      const url = generateVietQrUrl({
        bankId: 'MB',
        accountNumber: '123456789',
        amount: 100000,
        paymentCode: 'TF8X9K2M',
        accountName: 'TELEFONT STORE',
        template: 'compact2',
      });

      expect(url).toContain('https://img.vietqr.io/image/MB-123456789-compact2.png');
      expect(url).toContain('amount=100000');
      expect(url).toContain('addInfo=TF8X9K2M');
      expect(url).toContain('accountName=TELEFONT%20STORE');
    });
  });

  describe('SePay Webhook Authentication & Security (BLOCK 2 & BLOCK 3)', () => {
    it('returns 503 when SEPAY_WEBHOOK_SECRET is missing in env', async () => {
      const mockEnv: Env = {
        ...(env as unknown as Env),
        SEPAY_WEBHOOK_SECRET: undefined,
      };

      const req = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: 1 }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, mockEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(503);
    });

    it('returns 503 when recipient BANK_ACCOUNT_NUMBER is missing in env (fail-closed)', async () => {
      const mockEnv: Env = {
        ...(env as unknown as Env),
        SEPAY_WEBHOOK_SECRET: SEPAY_SECRET,
        BANK_ACCOUNT_NUMBER: undefined,
      };

      const req = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: 1 }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, mockEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(503);
    });

    it('returns 401 on missing signature or timestamp headers', async () => {
      const req = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: 1 }),
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(401);
    });

    it('rejects bare hex or malformed signature format without sha256= prefix', async () => {
      const timestamp = Math.floor(Date.now() / 1000);
      const body = JSON.stringify({ id: 1 });
      const rawHex = await generateSePaySignature(SEPAY_SECRET, timestamp, body);

      const reqBare = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': rawHex, // missing sha256= prefix
          'X-SePay-Timestamp': String(timestamp),
        },
        body,
      });

      const ctxBare = createExecutionContext();
      const resBare = await worker.fetch(reqBare, testEnv, ctxBare);
      await waitOnExecutionContext(ctxBare);
      expect(resBare.status).toBe(401);

      const reqShort = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': 'sha256=tooshort',
          'X-SePay-Timestamp': String(timestamp),
        },
        body,
      });

      const ctxShort = createExecutionContext();
      const resShort = await worker.fetch(reqShort, testEnv, ctxShort);
      await waitOnExecutionContext(ctxShort);
      expect(resShort.status).toBe(401);
    });

    it('returns 401 on stale timestamp (> 300 seconds drift)', async () => {
      const staleTimestamp = Math.floor(Date.now() / 1000) - 400;
      const body = JSON.stringify({ id: 1 });
      const signature = await generateSePaySignature(SEPAY_SECRET, staleTimestamp, body);

      const req = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${signature}`,
          'X-SePay-Timestamp': String(staleTimestamp),
        },
        body,
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(401);
    });

    it('returns 401 when body is tampered after signature generation', async () => {
      const timestamp = Math.floor(Date.now() / 1000);
      const originalBody = JSON.stringify({ id: 1, transferAmount: 50000 });
      const signature = await generateSePaySignature(SEPAY_SECRET, timestamp, originalBody);
      const tamperedBody = JSON.stringify({ id: 1, transferAmount: 10000 });

      const req = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${signature}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body: tamperedBody,
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(401);
    });
  });

  describe('Payload Validation & Runtime Safety (Gap 1 & BLOCK 2)', () => {
    it('acknowledges malformed json or non-object payload without mutating DB', async () => {
      const timestamp = Math.floor(Date.now() / 1000);
      const body = 'invalid-json{{{';
      const signature = await generateSePaySignature(SEPAY_SECRET, timestamp, body);

      const req = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${signature}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body,
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
      const json = await res.json();
      expect(json).toEqual({ success: true, status: 'ignored_invalid_json' });
    });

    it('handles authenticated wrong-typed payloads gracefully with HTTP 200 and zero mutation (Gap 1)', async () => {
      const timestamp = Math.floor(Date.now() / 1000);

      // Payload with wrong types for id, transferType, transferAmount, accountNumber, code
      const wrongTypedPayloads = [
        { id: {}, transferType: 123, transferAmount: 'abc', accountNumber: [], code: false },
        { id: -5, transferType: 'in', transferAmount: 50000, accountNumber: BANK_ACCOUNT, code: 'TF1234' },
        { id: 101, transferType: true, transferAmount: 50000, accountNumber: BANK_ACCOUNT, code: 'TF1234' },
        { id: 102, transferType: 'in', transferAmount: '50000', accountNumber: BANK_ACCOUNT, code: 'TF1234' },
        { id: 103, transferType: 'in', transferAmount: -1000, accountNumber: BANK_ACCOUNT, code: 'TF1234' },
        { id: 104, transferType: 'in', transferAmount: 50000, accountNumber: 123456, code: 'TF1234' },
        { id: 105, transferType: 'in', transferAmount: 50000, accountNumber: BANK_ACCOUNT, code: { complex: true } },
      ];

      for (const payload of wrongTypedPayloads) {
        const body = JSON.stringify(payload);
        const signature = await generateSePaySignature(SEPAY_SECRET, timestamp, body);

        const req = new Request('http://example.com/webhooks/sepay', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-SePay-Signature': `sha256=${signature}`,
            'X-SePay-Timestamp': String(timestamp),
          },
          body,
        });

        const ctx = createExecutionContext();
        const res = await worker.fetch(req, testEnv, ctx);
        await waitOnExecutionContext(ctx);

        expect(res.status).toBe(200);
        const json = (await res.json()) as { success: boolean; status: string };
        expect(json.success).toBe(true);
        expect(json.status).toMatch(/^ignored_/);
      }
    });

    it('ignores missing transferType or outbound transfer (transferType: out) without mutating DB', async () => {
      const timestamp = Math.floor(Date.now() / 1000);

      // 1. Missing transferType
      const bodyMissing = JSON.stringify({
        id: 1001,
        transferAmount: 50000,
        accountNumber: BANK_ACCOUNT,
        code: 'TF123456',
      });
      const sigMissing = await generateSePaySignature(SEPAY_SECRET, timestamp, bodyMissing);

      const reqMissing = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${sigMissing}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body: bodyMissing,
      });

      const ctxMissing = createExecutionContext();
      const resMissing = await worker.fetch(reqMissing, testEnv, ctxMissing);
      await waitOnExecutionContext(ctxMissing);
      expect(resMissing.status).toBe(200);
      const jsonMissing = await resMissing.json();
      expect(jsonMissing).toEqual({ success: true, status: 'ignored_unmatched', reason: 'invalid_or_missing_transfer_type' });

      // 2. Outbound transfer
      const bodyOut = JSON.stringify({
        id: 1002,
        transferType: 'out',
        transferAmount: 50000,
        accountNumber: BANK_ACCOUNT,
        code: 'TF123456',
      });
      const sigOut = await generateSePaySignature(SEPAY_SECRET, timestamp, bodyOut);

      const reqOut = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${sigOut}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body: bodyOut,
      });

      const ctxOut = createExecutionContext();
      const resOut = await worker.fetch(reqOut, testEnv, ctxOut);
      await waitOnExecutionContext(ctxOut);
      expect(resOut.status).toBe(200);
      const jsonOut = await resOut.json();
      expect(jsonOut).toEqual({ success: true, status: 'ignored_unmatched', reason: 'invalid_or_missing_transfer_type' });
    });

    it('ignores missing payload accountNumber without mutating DB', async () => {
      const timestamp = Math.floor(Date.now() / 1000);
      const body = JSON.stringify({
        id: 1003,
        transferType: 'in',
        transferAmount: 50000,
        code: 'TF123456',
      });
      const signature = await generateSePaySignature(SEPAY_SECRET, timestamp, body);

      const req = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${signature}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body,
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
      const json = await res.json();
      expect(json).toEqual({ success: true, status: 'ignored_unmatched', reason: 'missing_account_number' });
    });

    it('ignores wrong recipient account number without mutating DB', async () => {
      const timestamp = Math.floor(Date.now() / 1000);
      const body = JSON.stringify({
        id: 1004,
        transferType: 'in',
        transferAmount: 50000,
        accountNumber: '9999999999', // wrong account
        code: 'TF123456',
      });
      const signature = await generateSePaySignature(SEPAY_SECRET, timestamp, body);

      const req = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${signature}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body,
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
      const json = await res.json();
      expect(json).toEqual({ success: true, status: 'ignored_unmatched', reason: 'account_number_mismatch' });
    });

    it('ignores missing payload.code when content/description has no valid payment code candidate', async () => {
      const timestamp = Math.floor(Date.now() / 1000);
      const body = JSON.stringify({
        id: 1005,
        transferType: 'in',
        transferAmount: 50000,
        accountNumber: BANK_ACCOUNT,
        content: 'Chuyen tien mua font khong co ma',
        description: 'Chuyen tien mua font',
        // missing payload.code
      });
      const signature = await generateSePaySignature(SEPAY_SECRET, timestamp, body);

      const req = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${signature}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body,
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
      const json = await res.json();
      expect(json).toEqual({ success: true, status: 'ignored_unmatched', reason: 'missing_payment_code' });
    });

    it('ignores missing payload.code when content/description contains multiple ambiguous candidates', async () => {
      const timestamp = Math.floor(Date.now() / 1000);
      const body = JSON.stringify({
        id: 1006,
        transferType: 'in',
        transferAmount: 50000,
        accountNumber: BANK_ACCOUNT,
        content: 'Chuyen tien TF234567 va TF89ABCD',
        // missing payload.code
      });
      const signature = await generateSePaySignature(SEPAY_SECRET, timestamp, body);

      const req = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${signature}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body,
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
      const json = await res.json();
      expect(json).toEqual({ success: true, status: 'ignored_unmatched', reason: 'ambiguous_payment_code' });
    });

    it('processes verified payment via fallback extraction when payload.code is missing but exactly one valid candidate exists in content', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);
      const sessionService = new SessionService(env.DB);
      const orderService = new OrderService(env.DB);

      await sessionService.upsertTelegramUser({ id: 91199, is_bot: false, first_name: 'FallbackUser' });
      await sessionService.getOrCreateSession('91199', '91199');
      const s0 = await sessionService.getSessionByUserId('91199');
      await sessionService.selectMode('91199', s0!.workflow_token, 'ORIGINAL', s0!.version);
      await sessionService.updateSessionCatalog('91199', catalogId, 'SELECTING_STYLES');
      const s1 = await sessionService.getSessionByUserId('91199');
      await sessionService.setAllStyles('91199', s1!.workflow_token, ['rf_regular'], s1!.version);
      const s2 = await sessionService.getSessionByUserId('91199');
      await sessionService.transitionStatus('91199', s2!.workflow_token, 'SELECTING_STYLES', 'CONFIRMING', s2!.version);

      const s3 = await sessionService.getSessionByUserId('91199');
      const catalog = await catalogService.getCatalogById(catalogId);
      const orderRes = await orderService.createOrderFromSession(s3!, catalog!);
      const paymentCode = orderRes.paymentCode!;
      expect(orderRes.totalAmount).toBe(50000);

      const timestamp = Math.floor(Date.now() / 1000);
      const body = JSON.stringify({
        id: 7788991,
        transferType: 'in',
        transferAmount: 50000,
        accountNumber: BANK_ACCOUNT,
        // payload.code is intentionally null/missing
        code: null,
        content: `Thanh toan don hang ${paymentCode} tu MBBank`,
      });
      const signature = await generateSePaySignature(SEPAY_SECRET, timestamp, body);

      const req = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${signature}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body,
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
      const json = await res.json();
      expect(json).toEqual({ success: true, status: 'processed', order_id: orderRes.orderId });

      // Verify order transitioned to PAID in D1
      const orderInDb = await orderService.getOrderById(orderRes.orderId);
      expect(orderInDb?.status).toBe('PAID');

      // Replay of same transaction id with fallback extraction is idempotent
      const reqReplay = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${signature}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body,
      });
      const ctxReplay = createExecutionContext();
      const resReplay = await worker.fetch(reqReplay, testEnv, ctxReplay);
      await waitOnExecutionContext(ctxReplay);
      expect(resReplay.status).toBe(200);
      const jsonReplay = await resReplay.json();
      expect(jsonReplay).toEqual({ success: true, status: 'duplicate_acknowledged', order_id: orderRes.orderId });
    });

    it('rejects fallback extracted payment code when amount does not match order total', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);
      const sessionService = new SessionService(env.DB);
      const orderService = new OrderService(env.DB);

      await sessionService.upsertTelegramUser({ id: 91198, is_bot: false, first_name: 'AmountMismatchUser' });
      await sessionService.getOrCreateSession('91198', '91198');
      const s0 = await sessionService.getSessionByUserId('91198');
      await sessionService.selectMode('91198', s0!.workflow_token, 'ORIGINAL', s0!.version);
      await sessionService.updateSessionCatalog('91198', catalogId, 'SELECTING_STYLES');
      const s1 = await sessionService.getSessionByUserId('91198');
      await sessionService.setAllStyles('91198', s1!.workflow_token, ['rf_regular'], s1!.version);
      const s2 = await sessionService.getSessionByUserId('91198');
      await sessionService.transitionStatus('91198', s2!.workflow_token, 'SELECTING_STYLES', 'CONFIRMING', s2!.version);

      const s3 = await sessionService.getSessionByUserId('91198');
      const catalog = await catalogService.getCatalogById(catalogId);
      const orderRes = await orderService.createOrderFromSession(s3!, catalog!);
      const paymentCode = orderRes.paymentCode!;

      const timestamp = Math.floor(Date.now() / 1000);
      const body = JSON.stringify({
        id: 7788992,
        transferType: 'in',
        transferAmount: 10000, // order is 50000
        accountNumber: BANK_ACCOUNT,
        content: `Thanh toan ${paymentCode}`,
      });
      const signature = await generateSePaySignature(SEPAY_SECRET, timestamp, body);

      const req = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${signature}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body,
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
      const json = await res.json();
      expect(json).toEqual({ success: true, status: 'ignored_unmatched', reason: 'amount_mismatch' });

      // Order remains AWAITING_PAYMENT
      const orderInDb = await orderService.getOrderById(orderRes.orderId);
      expect(orderInDb?.status).toBe('AWAITING_PAYMENT');
    });
  });

  describe('Atomic Verified Payment, Predicate Binding & Mid-Batch Rollback (BLOCK 4 & BLOCK 5)', () => {
    it('valid webhook atomically transitions order to PAID, creates VERIFIED payment, PENDING job, and PENDING JOB_READY outbox', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);
      const sessionService = new SessionService(env.DB);
      const orderService = new OrderService(env.DB);

      await sessionService.upsertTelegramUser({ id: 91102, is_bot: false, first_name: 'SuccessUser' });
      await sessionService.getOrCreateSession('91102', '91102');
      const s0 = await sessionService.getSessionByUserId('91102');
      await sessionService.selectMode('91102', s0!.workflow_token, 'ORIGINAL', s0!.version);
      await sessionService.updateSessionCatalog('91102', catalogId, 'SELECTING_STYLES');
      const s1 = await sessionService.getSessionByUserId('91102');
      await sessionService.setAllStyles('91102', s1!.workflow_token, ['rf_regular', 'rf_bold'], s1!.version);
      const s2 = await sessionService.getSessionByUserId('91102');
      await sessionService.transitionStatus('91102', s2!.workflow_token, 'SELECTING_STYLES', 'CONFIRMING', s2!.version);

      const s3 = await sessionService.getSessionByUserId('91102');
      const catalog = await catalogService.getCatalogById(catalogId);
      const orderRes = await orderService.createOrderFromSession(s3!, catalog!);
      const paymentCode = orderRes.paymentCode!;
      expect(orderRes.totalAmount).toBe(100000);

      const timestamp = Math.floor(Date.now() / 1000);
      const body = JSON.stringify({
        id: 778899,
        transferType: 'in',
        transferAmount: 100000,
        accountNumber: BANK_ACCOUNT,
        code: paymentCode,
      });
      const signature = await generateSePaySignature(SEPAY_SECRET, timestamp, body);

      const req = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${signature}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body,
      });

      const ctx = createExecutionContext();
      const res = await worker.fetch(req, testEnv, ctx);
      await waitOnExecutionContext(ctx);

      expect(res.status).toBe(200);
      const json = await res.json();
      expect(json).toEqual({ success: true, status: 'processed', order_id: orderRes.orderId });

      // 1. Verify order status = PAID
      const orderInDb = await orderService.getOrderById(orderRes.orderId);
      expect(orderInDb?.status).toBe('PAID');

      // 2. Verify payment record = VERIFIED
      const payment = await env.DB.prepare('SELECT * FROM payments WHERE order_id = ?')
        .bind(orderRes.orderId)
        .first<{ provider: string; transaction_id: string; amount: number; status: string }>();
      expect(payment?.provider).toBe('SEPAY');
      expect(payment?.transaction_id).toBe('778899');
      expect(payment?.amount).toBe(100000);
      expect(payment?.status).toBe('VERIFIED');

      // 3. Verify exactly one fulfillment job = PENDING
      const jobs = await env.DB.prepare('SELECT * FROM fulfillment_jobs WHERE order_id = ?')
        .bind(orderRes.orderId)
        .all<{ id: string; status: string; attempt_count: number }>();
      expect(jobs.results.length).toBe(1);
      expect(jobs.results[0].status).toBe('PENDING');
      expect(jobs.results[0].attempt_count).toBe(0);

      // 4. Verify exactly one outbox event = PENDING with JOB_READY
      const outbox = await env.DB.prepare('SELECT * FROM outbox_events WHERE aggregate_id = ?')
        .bind(orderRes.orderId)
        .all<{ event_type: string; payload: string; status: string }>();
      expect(outbox.results.length).toBe(1);
      expect(outbox.results[0].event_type).toBe('JOB_READY');
      expect(outbox.results[0].status).toBe('PENDING');
      expect(JSON.parse(outbox.results[0].payload)).toEqual({ job_id: jobs.results[0].id });
    });

    it('injected mid-batch failure rolls back earlier statements in the transaction completely (BLOCK 5)', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);
      const sessionService = new SessionService(env.DB);
      const orderService = new OrderService(env.DB);
      const paymentService = new PaymentService(env.DB);

      await sessionService.upsertTelegramUser({ id: 91108, is_bot: false, first_name: 'MidBatchUser' });
      await sessionService.getOrCreateSession('91108', '91108');
      const s0 = await sessionService.getSessionByUserId('91108');
      await sessionService.selectMode('91108', s0!.workflow_token, 'ORIGINAL', s0!.version);
      await sessionService.updateSessionCatalog('91108', catalogId, 'SELECTING_STYLES');
      const s1 = await sessionService.getSessionByUserId('91108');
      await sessionService.setAllStyles('91108', s1!.workflow_token, ['rf_regular'], s1!.version);
      const s2 = await sessionService.getSessionByUserId('91108');
      await sessionService.transitionStatus('91108', s2!.workflow_token, 'SELECTING_STYLES', 'CONFIRMING', s2!.version);

      const s3 = await sessionService.getSessionByUserId('91108');
      const catalog = await catalogService.getCatalogById(catalogId);
      const orderRes = await orderService.createOrderFromSession(s3!, catalog!);
      const paymentCode = orderRes.paymentCode!;

      // Inject a statement that intentionally violates a foreign key or unique constraint
      const failingStatement = env.DB.prepare(
        'INSERT INTO order_items (id, order_id, font_id, price, created_at) VALUES (?, ?, ?, ?, ?)'
      ).bind('bad_item', 'non_existent_foreign_key_order', 'f_1', 100, Date.now());

      // Attempt payment processing with injected mid-batch failure
      await expect(
        paymentService.processVerifiedPayment(
          {
            transactionId: 'tx_mid_batch_fail_1',
            orderId: orderRes.orderId,
            paymentCode: paymentCode,
            expectedAmount: 50000,
          },
          failingStatement
        )
      ).rejects.toThrow();

      // Assert complete rollback:
      // 1. Order remains AWAITING_PAYMENT
      const orderAfterFail = await orderService.getOrderById(orderRes.orderId);
      expect(orderAfterFail?.status).toBe('AWAITING_PAYMENT');

      // 2. Zero payment rows
      const payments = await env.DB.prepare('SELECT * FROM payments WHERE order_id = ?')
        .bind(orderRes.orderId)
        .all();
      expect(payments.results.length).toBe(0);

      // 3. Zero fulfillment job rows
      const jobs = await env.DB.prepare('SELECT * FROM fulfillment_jobs WHERE order_id = ?')
        .bind(orderRes.orderId)
        .all();
      expect(jobs.results.length).toBe(0);

      // 4. Zero outbox event rows
      const outbox = await env.DB.prepare('SELECT * FROM outbox_events WHERE aggregate_id = ?')
        .bind(orderRes.orderId)
        .all();
      expect(outbox.results.length).toBe(0);
    });

    it('duplicate replay of same SePay transaction id is idempotent and acknowledged without duplicate side effects', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);
      const sessionService = new SessionService(env.DB);
      const orderService = new OrderService(env.DB);

      await sessionService.upsertTelegramUser({ id: 91103, is_bot: false, first_name: 'DupUser' });
      await sessionService.getOrCreateSession('91103', '91103');
      const s0 = await sessionService.getSessionByUserId('91103');
      await sessionService.selectMode('91103', s0!.workflow_token, 'ORIGINAL', s0!.version);
      await sessionService.updateSessionCatalog('91103', catalogId, 'SELECTING_STYLES');
      const s1 = await sessionService.getSessionByUserId('91103');
      await sessionService.setAllStyles('91103', s1!.workflow_token, ['rf_regular'], s1!.version);
      const s2 = await sessionService.getSessionByUserId('91103');
      await sessionService.transitionStatus('91103', s2!.workflow_token, 'SELECTING_STYLES', 'CONFIRMING', s2!.version);

      const s3 = await sessionService.getSessionByUserId('91103');
      const catalog = await catalogService.getCatalogById(catalogId);
      const orderRes = await orderService.createOrderFromSession(s3!, catalog!);
      const paymentCode = orderRes.paymentCode!;

      const timestamp = Math.floor(Date.now() / 1000);
      const body = JSON.stringify({
        id: 888111,
        transferType: 'in',
        transferAmount: 50000,
        accountNumber: BANK_ACCOUNT,
        code: paymentCode,
      });
      const signature = await generateSePaySignature(SEPAY_SECRET, timestamp, body);

      // First delivery
      const req1 = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${signature}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body,
      });

      const ctx1 = createExecutionContext();
      const res1 = await worker.fetch(req1, testEnv, ctx1);
      await waitOnExecutionContext(ctx1);
      expect(res1.status).toBe(200);

      // Replay of same transaction id
      const req2 = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${signature}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body,
      });

      const ctx2 = createExecutionContext();
      const res2 = await worker.fetch(req2, testEnv, ctx2);
      await waitOnExecutionContext(ctx2);
      expect(res2.status).toBe(200);
      const json2 = await res2.json();
      expect(json2).toEqual({
        success: true,
        status: 'duplicate_acknowledged',
        order_id: orderRes.orderId,
      });

      // Assert exactly 1 payment, 1 job, 1 outbox
      const payments = await env.DB.prepare('SELECT * FROM payments WHERE order_id = ?')
        .bind(orderRes.orderId)
        .all();
      expect(payments.results.length).toBe(1);

      const jobs = await env.DB.prepare('SELECT * FROM fulfillment_jobs WHERE order_id = ?')
        .bind(orderRes.orderId)
        .all();
      expect(jobs.results.length).toBe(1);

      const outbox = await env.DB.prepare('SELECT * FROM outbox_events WHERE aggregate_id = ?')
        .bind(orderRes.orderId)
        .all();
      expect(outbox.results.length).toBe(1);
    });

    it('concurrent delivery of the same SePay transaction is safe and deduplicated', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);
      const sessionService = new SessionService(env.DB);
      const orderService = new OrderService(env.DB);

      await sessionService.upsertTelegramUser({ id: 91104, is_bot: false, first_name: 'ConcurrentTxUser' });
      await sessionService.getOrCreateSession('91104', '91104');
      const s0 = await sessionService.getSessionByUserId('91104');
      await sessionService.selectMode('91104', s0!.workflow_token, 'ORIGINAL', s0!.version);
      await sessionService.updateSessionCatalog('91104', catalogId, 'SELECTING_STYLES');
      const s1 = await sessionService.getSessionByUserId('91104');
      await sessionService.setAllStyles('91104', s1!.workflow_token, ['rf_regular'], s1!.version);
      const s2 = await sessionService.getSessionByUserId('91104');
      await sessionService.transitionStatus('91104', s2!.workflow_token, 'SELECTING_STYLES', 'CONFIRMING', s2!.version);

      const s3 = await sessionService.getSessionByUserId('91104');
      const catalog = await catalogService.getCatalogById(catalogId);
      const orderRes = await orderService.createOrderFromSession(s3!, catalog!);
      const paymentCode = orderRes.paymentCode!;

      const timestamp = Math.floor(Date.now() / 1000);
      const body = JSON.stringify({
        id: 999333,
        transferType: 'in',
        transferAmount: 50000,
        accountNumber: BANK_ACCOUNT,
        code: paymentCode,
      });
      const signature = await generateSePaySignature(SEPAY_SECRET, timestamp, body);

      const makeReq = () =>
        new Request('http://example.com/webhooks/sepay', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-SePay-Signature': `sha256=${signature}`,
            'X-SePay-Timestamp': String(timestamp),
          },
          body,
        });

      const ctxA = createExecutionContext();
      const ctxB = createExecutionContext();

      const [resA, resB] = await Promise.all([
        worker.fetch(makeReq(), testEnv, ctxA),
        worker.fetch(makeReq(), testEnv, ctxB),
      ]);

      await Promise.all([waitOnExecutionContext(ctxA), waitOnExecutionContext(ctxB)]);

      expect(resA.status).toBe(200);
      expect(resB.status).toBe(200);

      const payments = await env.DB.prepare('SELECT * FROM payments WHERE order_id = ?')
        .bind(orderRes.orderId)
        .all();
      expect(payments.results.length).toBe(1);

      const jobs = await env.DB.prepare('SELECT * FROM fulfillment_jobs WHERE order_id = ?')
        .bind(orderRes.orderId)
        .all();
      expect(jobs.results.length).toBe(1);

      const outbox = await env.DB.prepare('SELECT * FROM outbox_events WHERE aggregate_id = ?')
        .bind(orderRes.orderId)
        .all();
      expect(outbox.results.length).toBe(1);
    });

    it('two distinct valid transactions racing for the same order produce at most one PAID transition, one job, and one outbox', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);
      const sessionService = new SessionService(env.DB);
      const orderService = new OrderService(env.DB);

      await sessionService.upsertTelegramUser({ id: 91105, is_bot: false, first_name: 'RaceUser' });
      await sessionService.getOrCreateSession('91105', '91105');
      const s0 = await sessionService.getSessionByUserId('91105');
      await sessionService.selectMode('91105', s0!.workflow_token, 'ORIGINAL', s0!.version);
      await sessionService.updateSessionCatalog('91105', catalogId, 'SELECTING_STYLES');
      const s1 = await sessionService.getSessionByUserId('91105');
      await sessionService.setAllStyles('91105', s1!.workflow_token, ['rf_regular'], s1!.version);
      const s2 = await sessionService.getSessionByUserId('91105');
      await sessionService.transitionStatus('91105', s2!.workflow_token, 'SELECTING_STYLES', 'CONFIRMING', s2!.version);

      const s3 = await sessionService.getSessionByUserId('91105');
      const catalog = await catalogService.getCatalogById(catalogId);
      const orderRes = await orderService.createOrderFromSession(s3!, catalog!);
      const paymentCode = orderRes.paymentCode!;

      const timestamp = Math.floor(Date.now() / 1000);

      const body1 = JSON.stringify({
        id: 111001,
        transferType: 'in',
        transferAmount: 50000,
        accountNumber: BANK_ACCOUNT,
        code: paymentCode,
      });
      const sig1 = await generateSePaySignature(SEPAY_SECRET, timestamp, body1);

      const body2 = JSON.stringify({
        id: 111002, // different transaction ID
        transferType: 'in',
        transferAmount: 50000,
        accountNumber: BANK_ACCOUNT,
        code: paymentCode,
      });
      const sig2 = await generateSePaySignature(SEPAY_SECRET, timestamp, body2);

      const req1 = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${sig1}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body: body1,
      });

      const req2 = new Request('http://example.com/webhooks/sepay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SePay-Signature': `sha256=${sig2}`,
          'X-SePay-Timestamp': String(timestamp),
        },
        body: body2,
      });

      const ctx1 = createExecutionContext();
      const ctx2 = createExecutionContext();

      const [res1, res2] = await Promise.all([
        worker.fetch(req1, testEnv, ctx1),
        worker.fetch(req2, testEnv, ctx2),
      ]);

      await Promise.all([waitOnExecutionContext(ctx1), waitOnExecutionContext(ctx2)]);

      expect(res1.status).toBe(200);
      expect(res2.status).toBe(200);

      // Exactly 1 fulfillment job and 1 outbox event
      const jobs = await env.DB.prepare('SELECT * FROM fulfillment_jobs WHERE order_id = ?')
        .bind(orderRes.orderId)
        .all();
      expect(jobs.results.length).toBe(1);

      const outbox = await env.DB.prepare('SELECT * FROM outbox_events WHERE aggregate_id = ?')
        .bind(orderRes.orderId)
        .all();
      expect(outbox.results.length).toBe(1);
    });
  });

  describe('Telegram Check Payment UX', () => {
    it('verifies Telegram ownership in D1 and reports current canonical order status without mutating state', async () => {
      const catalogService = new CatalogService(env.DB);
      const catalogId = await catalogService.persistCatalogResult(sampleCatalog);
      const sessionService = new SessionService(env.DB);
      const orderService = new OrderService(env.DB);

      await sessionService.upsertTelegramUser({ id: 91107, is_bot: false, first_name: 'CheckUser' });
      await sessionService.getOrCreateSession('91107', '91107');
      const s0 = await sessionService.getSessionByUserId('91107');
      await sessionService.selectMode('91107', s0!.workflow_token, 'ORIGINAL', s0!.version);
      await sessionService.updateSessionCatalog('91107', catalogId, 'SELECTING_STYLES');
      const s1 = await sessionService.getSessionByUserId('91107');
      await sessionService.setAllStyles('91107', s1!.workflow_token, ['rf_regular'], s1!.version);
      const s2 = await sessionService.getSessionByUserId('91107');
      await sessionService.transitionStatus('91107', s2!.workflow_token, 'SELECTING_STYLES', 'CONFIRMING', s2!.version);

      const s3 = await sessionService.getSessionByUserId('91107');
      const catalog = await catalogService.getCatalogById(catalogId);
      const orderRes = await orderService.createOrderFromSession(s3!, catalog!);

      let lastEditPayload: { text?: string; reply_markup?: any } = {};
      let lastAnswerText = '';

      globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const urlStr = typeof input === 'string' ? input : input.toString();
        if (urlStr.includes('editMessageText') && init?.body) {
          lastEditPayload = JSON.parse(init.body as string);
        }
        if (urlStr.includes('answerCallbackQuery') && init?.body) {
          const body = JSON.parse(init.body as string);
          lastAnswerText = body.text || '';
        }
        return new Response(JSON.stringify({ ok: true, result: { message_id: 888 } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      // 1. Check status while AWAITING_PAYMENT
      const checkUpdate1: TelegramUpdate = {
        update_id: 3001,
        callback_query: {
          id: 'cb_chk_1',
          from: { id: 91107, is_bot: false, first_name: 'CheckUser' },
          data: `ord:chk:${orderRes.orderId}`,
        },
      };

      const req1 = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': TELEGRAM_SECRET,
        },
        body: JSON.stringify(checkUpdate1),
      });

      const ctx1 = createExecutionContext();
      const res1 = await worker.fetch(req1, testEnv, ctx1);
      await waitOnExecutionContext(ctx1);
      expect(res1.status).toBe(200);

      expect(lastEditPayload.text).toContain('AWAITING_PAYMENT');
      expect(lastAnswerText).toContain('AWAITING_PAYMENT');

      // 2. Transition order to PAID via verified payment
      const paymentService = new PaymentService(env.DB);
      await paymentService.processVerifiedPayment({
        transactionId: 'sepay_chk_test_1',
        orderId: orderRes.orderId,
        paymentCode: orderRes.paymentCode!,
        expectedAmount: 50000,
      });

      // 3. Check status again -> reports PAID
      const checkUpdate2: TelegramUpdate = {
        update_id: 3002,
        callback_query: {
          id: 'cb_chk_2',
          from: { id: 91107, is_bot: false, first_name: 'CheckUser' },
          data: `ord:chk:${orderRes.orderId}`,
        },
      };

      const req2 = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': TELEGRAM_SECRET,
        },
        body: JSON.stringify(checkUpdate2),
      });

      const ctx2 = createExecutionContext();
      const res2 = await worker.fetch(req2, testEnv, ctx2);
      await waitOnExecutionContext(ctx2);
      expect(res2.status).toBe(200);

      expect(lastEditPayload.text).toContain('PAID');
      expect(lastAnswerText).toContain('PAID');

      // 4. Unauthorized user cannot check other user's order
      const unauthorizedUpdate: TelegramUpdate = {
        update_id: 3003,
        callback_query: {
          id: 'cb_chk_unauth',
          from: { id: 99999, is_bot: false, first_name: 'Attacker' },
          data: `ord:chk:${orderRes.orderId}`,
        },
      };

      // Upsert attacker session
      await sessionService.upsertTelegramUser({ id: 99999, is_bot: false, first_name: 'Attacker' });
      await sessionService.getOrCreateSession('99999', '99999');

      const req3 = new Request('http://example.com/webhooks/telegram', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Bot-Api-Secret-Token': TELEGRAM_SECRET,
        },
        body: JSON.stringify(unauthorizedUpdate),
      });

      const ctx3 = createExecutionContext();
      const res3 = await worker.fetch(req3, testEnv, ctx3);
      await waitOnExecutionContext(ctx3);
      expect(res3.status).toBe(200);

      expect(lastAnswerText).toContain('unauthorized');
    });
  });
});

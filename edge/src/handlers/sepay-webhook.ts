import type { Env } from '../env';
import { PaymentService } from '../services/payment-service';
import { OrderService } from '../services/order-service';
import { emitStructuredLog } from '../utils/logger';

export interface SePayWebhookPayload {
  id?: unknown;
  gateway?: unknown;
  transactionDate?: unknown;
  accountNumber?: unknown;
  code?: unknown;
  content?: unknown;
  transferType?: unknown;
  transferAmount?: unknown;
  accumulated?: unknown;
  subAccount?: unknown;
  referenceCode?: unknown;
  description?: unknown;
}

export async function verifySePaySignature(
  rawBody: string,
  signatureHeader: string | null,
  timestampHeader: string | null,
  secret: string
): Promise<{ isValid: boolean; reason?: string }> {
  if (!signatureHeader || !timestampHeader || !secret) {
    return { isValid: false, reason: 'missing_auth_headers_or_secret' };
  }

  // 1. Verify strict signature format: sha256=<64 hex chars> (BLOCK 3)
  const match = signatureHeader.trim().match(/^sha256=([0-9a-fA-F]{64})$/);
  if (!match) {
    return { isValid: false, reason: 'invalid_signature_format' };
  }

  // 2. Verify timestamp within 300 seconds (5 minutes) drift
  const timestampNum = Number(timestampHeader);
  if (isNaN(timestampNum)) {
    return { isValid: false, reason: 'invalid_timestamp_format' };
  }

  const nowMs = Date.now();
  const timestampMs = timestampNum > 1e11 ? timestampNum : timestampNum * 1000;
  const driftMs = Math.abs(nowMs - timestampMs);

  if (driftMs > 300_000) {
    return { isValid: false, reason: 'timestamp_drift_exceeded' };
  }

  // 3. Constant-time native HMAC verification using 32-byte buffer
  const providedHex = match[1].toLowerCase();
  const providedBytes = new Uint8Array(32);
  for (let i = 0; i < 32; i++) {
    providedBytes[i] = parseInt(providedHex.substring(i * 2, i * 2 + 2), 16);
  }

  const encoder = new TextEncoder();
  const dataToSign = encoder.encode(`${timestampHeader}.${rawBody}`);
  const keyData = encoder.encode(secret);

  const cryptoKey = await crypto.subtle.importKey(
    'raw',
    keyData,
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['verify']
  );

  const isValid = await crypto.subtle.verify('HMAC', cryptoKey, providedBytes, dataToSign);
  if (!isValid) {
    return { isValid: false, reason: 'signature_mismatch' };
  }

  return { isValid: true };
}

export async function handleSePayWebhook(
  request: Request,
  env: Env,
  _ctx: ExecutionContext
): Promise<Response> {
  // 1. Verify webhook secret and recipient configuration (fail-closed, BLOCK 2)
  if (!env.SEPAY_WEBHOOK_SECRET) {
    return new Response(JSON.stringify({ error: 'SePay webhook secret not configured' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  if (!env.BANK_ACCOUNT_NUMBER || !env.BANK_ACCOUNT_NUMBER.trim()) {
    return new Response(JSON.stringify({ error: 'Recipient bank account not configured' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // 2. Read raw body bytes/string for HMAC validation before JSON parsing
  const rawBody = await request.text();
  const signatureHeader = request.headers.get('X-SePay-Signature');
  const timestampHeader = request.headers.get('X-SePay-Timestamp');

  const authResult = await verifySePaySignature(
    rawBody,
    signatureHeader,
    timestampHeader,
    env.SEPAY_WEBHOOK_SECRET
  );

  if (!authResult.isValid) {
    return new Response(
      JSON.stringify({ error: 'Unauthorized', reason: authResult.reason }),
      {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

const PAYMENT_CODE_REGEX = /\b(TF[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{6})\b/gi;

function resolvePaymentCode(payload: SePayWebhookPayload): { code: string | null; error?: string } {
  // 1. Direct provider-extracted code
  if (typeof payload.code === 'string' && payload.code.trim()) {
    return { code: payload.code.trim().toUpperCase() };
  }

  // 2. Fail-closed fallback: search signed content & description for canonical app payment code
  const candidates = new Set<string>();
  const textToSearch = [
    typeof payload.content === 'string' ? payload.content : '',
    typeof payload.description === 'string' ? payload.description : '',
  ].join(' ');

  if (textToSearch.trim()) {
    const matches = textToSearch.match(PAYMENT_CODE_REGEX);
    if (matches) {
      for (const m of matches) {
        candidates.add(m.trim().toUpperCase());
      }
    }
  }

  if (candidates.size === 1) {
    const [singleCode] = Array.from(candidates);
    return { code: singleCode };
  }

  if (candidates.size > 1) {
    return { code: null, error: 'ambiguous_payment_code' };
  }

  return { code: null, error: 'missing_payment_code' };
}

  // 3. Parse and strictly runtime-validate JSON payload (BLOCK 1 from rereview)
  let payload: SePayWebhookPayload;
  try {
    payload = JSON.parse(rawBody) as SePayWebhookPayload;
  } catch {
    return new Response(JSON.stringify({ success: true, status: 'ignored_invalid_json' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return new Response(JSON.stringify({ success: true, status: 'ignored_invalid_payload' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // Explicit provider ID validation
  let transactionId = '';
  if (typeof payload.id === 'number' && Number.isFinite(payload.id) && payload.id > 0) {
    transactionId = String(payload.id);
  } else if (typeof payload.id === 'string' && /^[0-9a-zA-Z_-]{1,64}$/.test(payload.id.trim())) {
    transactionId = payload.id.trim();
  } else {
    return new Response(
      JSON.stringify({ success: true, status: 'ignored_invalid_payload', reason: 'invalid_provider_id' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // 4. Validate business preconditions before financial transition (BLOCK 2)
  // A. transferType must be string and exactly 'in'
  if (
    typeof payload.transferType !== 'string' ||
    payload.transferType.trim().toLowerCase() !== 'in'
  ) {
    return new Response(
      JSON.stringify({ success: true, status: 'ignored_unmatched', reason: 'invalid_or_missing_transfer_type' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // B. accountNumber must be non-empty string and exact-match configured recipient account
  if (
    typeof payload.accountNumber !== 'string' ||
    !payload.accountNumber.trim()
  ) {
    return new Response(
      JSON.stringify({ success: true, status: 'ignored_unmatched', reason: 'missing_account_number' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  if (payload.accountNumber.trim() !== env.BANK_ACCOUNT_NUMBER.trim()) {
    return new Response(
      JSON.stringify({ success: true, status: 'ignored_unmatched', reason: 'account_number_mismatch' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // C. code must be resolved either from payload.code or unique fallback from signed content/description
  const { code: paymentCode, error: codeError } = resolvePaymentCode(payload);
  if (!paymentCode) {
    return new Response(
      JSON.stringify({ success: true, status: 'ignored_unmatched', reason: codeError || 'missing_payment_code' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // D. transferAmount must be positive number
  if (
    typeof payload.transferAmount !== 'number' ||
    !Number.isFinite(payload.transferAmount) ||
    payload.transferAmount <= 0
  ) {
    return new Response(
      JSON.stringify({ success: true, status: 'ignored_unmatched', reason: 'invalid_transfer_amount' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  const orderService = new OrderService(env.DB);
  const paymentService = new PaymentService(env.DB);

  const order = await orderService.getOrderByPaymentCode(paymentCode);
  if (!order) {
    return new Response(
      JSON.stringify({ success: true, status: 'ignored_unmatched', reason: 'order_not_found' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // E. Order currency must be VND
  if (order.currency !== 'VND') {
    return new Response(
      JSON.stringify({ success: true, status: 'ignored_unmatched', reason: 'unsupported_currency' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // F. Transfer amount must match order total_amount
  if (payload.transferAmount !== order.total_amount) {
    return new Response(
      JSON.stringify({ success: true, status: 'ignored_unmatched', reason: 'amount_mismatch' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // 5. Execute atomic financial transition with full predicate binding (BLOCK 4)
  try {
    const result = await paymentService.processVerifiedPayment({
      transactionId,
      orderId: order.id,
      paymentCode: order.payment_code || paymentCode,
      expectedAmount: order.total_amount,
    });

    if (result.status === 'PROCESSED') {
      emitStructuredLog({
        event: 'payment_accepted',
        order_id: order.id,
        user_id: order.user_id,
        amount: order.total_amount,
        currency: order.currency || 'VND',
        payment_code: order.payment_code || paymentCode,
        provider: 'SEPAY',
      });

      return new Response(
        JSON.stringify({ success: true, status: 'processed', order_id: order.id }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    if (result.status === 'DUPLICATE') {
      return new Response(
        JSON.stringify({ success: true, status: 'duplicate_acknowledged', order_id: order.id }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    if (result.status === 'CONFLICT') {
      return new Response(
        JSON.stringify({ success: true, status: 'order_already_paid', order_id: order.id }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }

    return new Response(
      JSON.stringify({ success: true, status: 'ignored_unmatched', reason: 'order_not_awaiting_payment' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  } catch (err: unknown) {
    return new Response(JSON.stringify({ error: 'Internal Payment Processing Error' }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

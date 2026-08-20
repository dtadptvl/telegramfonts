import type { Env } from '../env';
import { PaymentService } from '../services/payment-service';
import { OrderService } from '../services/order-service';

export interface SePayWebhookPayload {
  id?: number | string;
  gateway?: string;
  transactionDate?: string;
  accountNumber?: string;
  code?: string | null;
  content?: string | null;
  transferType?: string;
  transferAmount?: number;
  accumulated?: number;
  subAccount?: string | null;
  referenceCode?: string | null;
  description?: string | null;
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

  // 3. Parse JSON payload
  let payload: SePayWebhookPayload;
  try {
    payload = JSON.parse(rawBody) as SePayWebhookPayload;
  } catch {
    return new Response(JSON.stringify({ status: 'ignored_invalid_json' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  if (!payload || payload.id === undefined || payload.id === null) {
    return new Response(JSON.stringify({ status: 'ignored_missing_id' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // 4. Validate business preconditions before financial transition (BLOCK 2)
  // A. transferType must be present and exactly 'in'
  if (!payload.transferType || payload.transferType.toLowerCase() !== 'in') {
    return new Response(
      JSON.stringify({ status: 'ignored_unmatched', reason: 'invalid_or_missing_transfer_type' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // B. accountNumber must be present and exact-match configured recipient account
  if (!payload.accountNumber || typeof payload.accountNumber !== 'string') {
    return new Response(
      JSON.stringify({ status: 'ignored_unmatched', reason: 'missing_account_number' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  if (payload.accountNumber.trim() !== env.BANK_ACCOUNT_NUMBER.trim()) {
    return new Response(
      JSON.stringify({ status: 'ignored_unmatched', reason: 'account_number_mismatch' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // C. code must be present in payload.code (Issue #8 contract: no content/description fallback)
  if (!payload.code || typeof payload.code !== 'string' || !payload.code.trim()) {
    return new Response(
      JSON.stringify({ status: 'ignored_unmatched', reason: 'missing_payment_code' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  const paymentCode = payload.code.trim().toUpperCase();

  const orderService = new OrderService(env.DB);
  const paymentService = new PaymentService(env.DB);

  const order = await orderService.getOrderByPaymentCode(paymentCode);
  if (!order) {
    return new Response(
      JSON.stringify({ status: 'ignored_unmatched', reason: 'order_not_found' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // D. Order currency must be VND
  if (order.currency !== 'VND') {
    return new Response(
      JSON.stringify({ status: 'ignored_unmatched', reason: 'unsupported_currency' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // E. Transfer amount must match order total_amount
  const transferAmount = Number(payload.transferAmount);
  if (isNaN(transferAmount) || transferAmount !== order.total_amount) {
    return new Response(
      JSON.stringify({ status: 'ignored_unmatched', reason: 'amount_mismatch' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // 5. Execute atomic financial transition with full predicate binding (BLOCK 4)
  try {
    const result = await paymentService.processVerifiedPayment({
      transactionId: String(payload.id),
      orderId: order.id,
      paymentCode: order.payment_code || paymentCode,
      expectedAmount: order.total_amount,
    });

    if (result.status === 'PROCESSED') {
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
      JSON.stringify({ status: 'ignored_unmatched', reason: 'order_not_awaiting_payment' }),
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

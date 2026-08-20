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

  let expectedHex = signatureHeader.trim();
  if (expectedHex.startsWith('sha256=')) {
    expectedHex = expectedHex.slice(7);
  }

  const encoder = new TextEncoder();
  const dataToSign = encoder.encode(`${timestampHeader}.${rawBody}`);
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
  const calculatedHex = signatureArray
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');

  const calculatedBytes = encoder.encode(calculatedHex.toLowerCase());
  const expectedBytes = encoder.encode(expectedHex.toLowerCase());

  if (calculatedBytes.byteLength !== expectedBytes.byteLength) {
    return { isValid: false, reason: 'signature_mismatch' };
  }

  const matches = crypto.subtle.timingSafeEqual(calculatedBytes, expectedBytes);
  if (!matches) {
    return { isValid: false, reason: 'signature_mismatch' };
  }

  return { isValid: true };
}

export async function handleSePayWebhook(
  request: Request,
  env: Env,
  _ctx: ExecutionContext
): Promise<Response> {
  // 1. Verify webhook secret configuration (fail-closed)
  if (!env.SEPAY_WEBHOOK_SECRET) {
    return new Response(JSON.stringify({ error: 'SePay webhook secret not configured' }), {
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

  // 4. Validate business preconditions before financial transition
  // A. Transfer type must be 'in'
  if (payload.transferType && payload.transferType.toLowerCase() !== 'in') {
    return new Response(
      JSON.stringify({ status: 'ignored_unmatched', reason: 'outbound_transfer' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // B. Account number must match configured recipient if configured
  if (env.BANK_ACCOUNT_NUMBER && payload.accountNumber) {
    const configuredAcc = env.BANK_ACCOUNT_NUMBER.trim();
    const payloadAcc = payload.accountNumber.trim();
    if (configuredAcc !== payloadAcc) {
      return new Response(
        JSON.stringify({ status: 'ignored_unmatched', reason: 'account_number_mismatch' }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    }
  }

  // C. Extract payment code
  let rawCode = payload.code ? String(payload.code).trim() : '';
  if (!rawCode && (payload.content || payload.description)) {
    const textToSearch = `${payload.content || ''} ${payload.description || ''}`;
    const prefix = env.PAYMENT_CODE_PREFIX || 'TF';
    const regex = new RegExp(`\\b(${prefix}[A-Z0-9]{4,10})\\b`, 'i');
    const match = textToSearch.match(regex);
    if (match) {
      rawCode = match[1];
    }
  }

  if (!rawCode) {
    return new Response(
      JSON.stringify({ status: 'ignored_unmatched', reason: 'missing_payment_code' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  const orderService = new OrderService(env.DB);
  const paymentService = new PaymentService(env.DB);

  const order = await orderService.getOrderByPaymentCode(rawCode);
  if (!order) {
    return new Response(
      JSON.stringify({ status: 'ignored_unmatched', reason: 'order_not_found' }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // D. Order must be in VND currency
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

  // 5. Execute atomic financial transition
  try {
    const result = await paymentService.processVerifiedPayment({
      transactionId: String(payload.id),
      orderId: order.id,
      amount: order.total_amount,
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

/**
 * HMAC-SHA256 URL signing and verification for private R2 artifact downloads.
 */

const DEFAULT_TTL_SECONDS = 86400; // 24 hours
const MIN_TTL_SECONDS = 60; // 1 minute
const MAX_TTL_SECONDS = 604800; // 7 days

export function getDownloadTtlSeconds(envTtl?: string): number {
  if (!envTtl) return DEFAULT_TTL_SECONDS;
  const parsed = parseInt(envTtl, 10);
  if (Number.isNaN(parsed)) return DEFAULT_TTL_SECONDS;
  return Math.max(MIN_TTL_SECONDS, Math.min(parsed, MAX_TTL_SECONDS));
}

export function buildCanonicalSignatureString(orderId: string, expires: number): string {
  return `v1:${orderId}:${expires}`;
}

export async function createDownloadSignature(
  orderId: string,
  expires: number,
  secret: string
): Promise<string> {
  if (!secret || !secret.trim()) {
    throw new Error('DOWNLOAD_SIGNING_SECRET_MISSING');
  }

  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw',
    enc.encode(secret.trim()),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );

  const canonical = buildCanonicalSignatureString(orderId, expires);
  const sigBuffer = await crypto.subtle.sign('HMAC', key, enc.encode(canonical));
  return Array.from(new Uint8Array(sigBuffer))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

export async function generateSignedDownloadUrl(
  orderId: string,
  secret: string,
  options?: {
    ttlSeconds?: number;
    expiresTimestamp?: number;
    baseUrl?: string;
  }
): Promise<{ url: string; expires: number; signature: string }> {
  if (!orderId || !/^[a-zA-Z0-9_-]{1,64}$/.test(orderId)) {
    throw new Error('INVALID_ORDER_ID');
  }

  let expires: number;
  if (options?.expiresTimestamp !== undefined) {
    expires = options.expiresTimestamp;
  } else if (options?.ttlSeconds !== undefined) {
    expires = Math.floor(Date.now() / 1000) + options.ttlSeconds;
  } else {
    expires = Math.floor(Date.now() / 1000) + DEFAULT_TTL_SECONDS;
  }
  const signature = await createDownloadSignature(orderId, expires, secret);

  const basePath = `/downloads/${encodeURIComponent(orderId)}`;
  const query = `?expires=${expires}&sig=${signature}`;
  const fullUrl = options?.baseUrl ? `${options.baseUrl.replace(/\/+$/, '')}${basePath}${query}` : `${basePath}${query}`;

  return {
    url: fullUrl,
    expires,
    signature,
  };
}

export async function verifyDownloadSignature(
  orderId: string,
  expiresParam: string | null,
  sigParam: string | null,
  secret: string | undefined,
  nowMs: number = Date.now()
): Promise<{ valid: boolean; reason?: string }> {
  if (!secret || !secret.trim()) {
    return { valid: false, reason: 'SIGNING_SECRET_NOT_CONFIGURED' };
  }

  if (!orderId || !/^[a-zA-Z0-9_-]{1,64}$/.test(orderId)) {
    return { valid: false, reason: 'INVALID_ORDER_ID' };
  }

  if (!expiresParam || !/^\d{9,12}$/.test(expiresParam.trim())) {
    return { valid: false, reason: 'INVALID_EXPIRES_PARAM' };
  }

  if (!sigParam || !/^[0-9a-fA-F]{64}$/.test(sigParam.trim())) {
    return { valid: false, reason: 'INVALID_SIGNATURE_FORMAT' };
  }

  const expires = parseInt(expiresParam.trim(), 10);
  const nowSeconds = Math.floor(nowMs / 1000);

  if (nowSeconds > expires) {
    return { valid: false, reason: 'EXPIRED_SIGNATURE' };
  }

  const expectedSig = await createDownloadSignature(orderId, expires, secret);
  const enc = new TextEncoder();
  const providedBytes = enc.encode(sigParam.trim().toLowerCase());
  const expectedBytes = enc.encode(expectedSig.toLowerCase());

  if (providedBytes.byteLength !== expectedBytes.byteLength) {
    return { valid: false, reason: 'SIGNATURE_MISMATCH' };
  }

  const isMatch = crypto.subtle.timingSafeEqual(providedBytes, expectedBytes);
  if (!isMatch) {
    return { valid: false, reason: 'SIGNATURE_MISMATCH' };
  }

  return { valid: true };
}

import { describe, it, expect } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker, { type Env } from '../src/index';
import { generateSignedDownloadUrl, formatTtlDescription, getDownloadTtlSeconds } from '../src/utils/download-signer';

const SIGNING_SECRET = 'test_download_signing_secret_1234567890';

const testEnv: Env = {
  ...(env as unknown as Env),
  DOWNLOAD_SIGNING_SECRET: SIGNING_SECRET,
  DOWNLOAD_URL_TTL_SECONDS: '3600',
  BASE_URL: 'https://telefont.example.com',
};

async function setupCompletedOrderWithArtifact() {
  const orderId = `ord_dl_${crypto.randomUUID().replace(/-/g, '')}`;
  const jobId = `job_dl_${crypto.randomUUID().replace(/-/g, '')}`;
  const now = Date.now();

  const dummyZip = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x10, 0x20, 0x30, 0x40]);
  const shaBuffer = await crypto.subtle.digest('SHA-256', dummyZip);
  const sha256Hex = Array.from(new Uint8Array(shaBuffer))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
  const artifactKey = `artifacts/${orderId}/${jobId}/${sha256Hex}.zip`;

  // Put artifact in R2
  await env.ARTIFACTS_BUCKET.put(artifactKey, dummyZip, {
    sha256: sha256Hex,
    customMetadata: {
      job_id: jobId,
      order_id: orderId,
      sha256: sha256Hex,
    },
    httpMetadata: {
      contentType: 'application/zip',
      contentDisposition: `attachment; filename="${orderId}.zip"`,
    },
  });

  // Insert completed order
  await env.DB.prepare(
    `INSERT INTO orders (id, user_id, status, total_amount, currency, created_at, updated_at, completed_at)
     VALUES (?, 12345, 'COMPLETED', 100000, 'VND', ?, ?, ?)`
  )
    .bind(orderId, now, now, now)
    .run();

  // Insert fulfillment receipt
  await env.DB.prepare(
    `INSERT INTO fulfillment_receipts (job_id, order_id, artifact_key, artifact_sha256, artifact_size_bytes, completed_at, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?)`
  )
    .bind(jobId, orderId, artifactKey, sha256Hex, dummyZip.byteLength, now, now)
    .run();

  return { orderId, jobId, artifactKey, dummyZip };
}

describe('Phase 6: HMAC Signed Download URL Verification & R2 Streaming', () => {
  it('formats TTL descriptions correctly and honors configurable TTL', () => {
    expect(formatTtlDescription(86400)).toBe('1 day');
    expect(formatTtlDescription(172800)).toBe('2 days');
    expect(formatTtlDescription(3600)).toBe('1 hour');
    expect(formatTtlDescription(7200)).toBe('2 hours');
    expect(formatTtlDescription(300)).toBe('5 minutes');

    expect(getDownloadTtlSeconds('7200')).toBe(7200);
    expect(getDownloadTtlSeconds(undefined)).toBe(86400);
    expect(getDownloadTtlSeconds('invalid')).toBe(86400);
  });

  it('generates absolute download URL with validated HTTPS baseUrl', async () => {
    const signed = await generateSignedDownloadUrl('ord_123', SIGNING_SECRET, {
      baseUrl: 'https://telefont.example.com',
      ttlSeconds: 3600,
    });
    expect(signed.url).toContain('https://telefont.example.com/downloads/ord_123?expires=');
    expect(signed.url).toContain('&sig=');
  });

  it('rejects baseUrl when insecure HTTP or missing when requireHttps is true', async () => {
    await expect(
      generateSignedDownloadUrl('ord_123', SIGNING_SECRET, {
        baseUrl: 'http://insecure.example.com',
        requireHttps: true,
      })
    ).rejects.toThrow('INSECURE_BASE_URL_HTTPS_REQUIRED');

    await expect(
      generateSignedDownloadUrl('ord_123', SIGNING_SECRET, {
        requireHttps: true,
      })
    ).rejects.toThrow('MISSING_BASE_URL');
  });

  it('streams private R2 artifact when HMAC signature is valid and unexpired', async () => {
    const { orderId, dummyZip } = await setupCompletedOrderWithArtifact();

    const signed = await generateSignedDownloadUrl(orderId, SIGNING_SECRET, { ttlSeconds: 3600 });
    const req = new Request(`http://example.com${signed.url}`, {
      method: 'GET',
    });

    const ctx = createExecutionContext();
    const res = await worker.fetch(req, testEnv, ctx);
    await waitOnExecutionContext(ctx);

    expect(res.status).toBe(200);
    expect(res.headers.get('Content-Type')).toBe('application/zip');
    expect(res.headers.get('Content-Disposition')).toBe(`attachment; filename="${orderId}.zip"`);
    expect(res.headers.get('Content-Length')).toBe(dummyZip.byteLength.toString());

    const bodyBytes = new Uint8Array(await res.arrayBuffer());
    expect(bodyBytes).toEqual(dummyZip);
  });

  it('rejects download when signature is tampered or invalid', async () => {
    const { orderId } = await setupCompletedOrderWithArtifact();

    const signed = await generateSignedDownloadUrl(orderId, SIGNING_SECRET, { ttlSeconds: 3600 });
    const tamperedSig = 'a'.repeat(64);
    const req = new Request(`http://example.com/downloads/${orderId}?expires=${signed.expires}&sig=${tamperedSig}`, {
      method: 'GET',
    });

    const ctx = createExecutionContext();
    const res = await worker.fetch(req, testEnv, ctx);
    await waitOnExecutionContext(ctx);

    expect(res.status).toBe(403);
    const data = (await res.json()) as { error: string; reason: string };
    expect(data.reason).toBe('SIGNATURE_MISMATCH');
  });

  it('rejects download when signature has expired', async () => {
    const { orderId } = await setupCompletedOrderWithArtifact();

    // Expired timestamp in the past
    const signed = await generateSignedDownloadUrl(orderId, SIGNING_SECRET, { ttlSeconds: -300 });

    const req = new Request(`http://example.com${signed.url}`, {
      method: 'GET',
    });

    const ctx = createExecutionContext();
    const res = await worker.fetch(req, testEnv, ctx);
    await waitOnExecutionContext(ctx);

    expect(res.status).toBe(403);
    const data = (await res.json()) as { error: string; reason: string };
    expect(data.reason).toBe('EXPIRED_SIGNATURE');
  });

  it('fails closed when DOWNLOAD_SIGNING_SECRET is missing from env', async () => {
    const { orderId } = await setupCompletedOrderWithArtifact();

    const signed = await generateSignedDownloadUrl(orderId, SIGNING_SECRET, { ttlSeconds: 3600 });
    const mockEnv: Env = {
      ...(env as unknown as Env),
      DOWNLOAD_SIGNING_SECRET: undefined,
    };

    const req = new Request(`http://example.com${signed.url}`, {
      method: 'GET',
    });

    const ctx = createExecutionContext();
    const res = await worker.fetch(req, mockEnv, ctx);
    await waitOnExecutionContext(ctx);

    expect(res.status).toBe(403);
  });

  it('rejects download if order is not in COMPLETED state', async () => {
    const { orderId } = await setupCompletedOrderWithArtifact();

    // Revert order status to PROCESSING
    await env.DB.prepare('UPDATE orders SET status = "PROCESSING" WHERE id = ?')
      .bind(orderId)
      .run();

    const signed = await generateSignedDownloadUrl(orderId, SIGNING_SECRET, { ttlSeconds: 3600 });
    const req = new Request(`http://example.com${signed.url}`, {
      method: 'GET',
    });

    const ctx = createExecutionContext();
    const res = await worker.fetch(req, testEnv, ctx);
    await waitOnExecutionContext(ctx);

    expect(res.status).toBe(404);
  });
});

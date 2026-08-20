import type { Env } from '../env';
import { verifyDownloadSignature } from '../utils/download-signer';
import { emitStructuredLog } from '../utils/logger';

export async function handleDownload(
  request: Request,
  env: Env,
  _ctx: ExecutionContext
): Promise<Response> {
  if (request.method !== 'GET') {
    return new Response(JSON.stringify({ error: 'Method Not Allowed' }), {
      status: 405,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const url = new URL(request.url);
  const match = url.pathname.match(/^\/downloads\/([a-zA-Z0-9_-]+)$/);
  if (!match) {
    return new Response(JSON.stringify({ error: 'Not Found' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const orderId = match[1];
  const expiresParam = url.searchParams.get('expires');
  const sigParam = url.searchParams.get('sig');

  // 1. Verify HMAC-SHA256 bearer signature (fail closed if secret missing or signature invalid)
  const sigResult = await verifyDownloadSignature(
    orderId,
    expiresParam,
    sigParam,
    env.DOWNLOAD_SIGNING_SECRET
  );

  if (!sigResult.valid) {
    return new Response(
      JSON.stringify({ error: 'Forbidden', reason: sigResult.reason || 'invalid_download_signature' }),
      {
        status: 403,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  if (!env.DB || !env.ARTIFACTS_BUCKET) {
    return new Response(JSON.stringify({ error: 'Service Unavailable' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // 2. Query canonical D1 order and receipt status
  const order = await env.DB
    .prepare('SELECT id, status FROM orders WHERE id = ?')
    .bind(orderId)
    .first<{ id: string; status: string }>();

  if (!order || order.status !== 'COMPLETED') {
    return new Response(
      JSON.stringify({ error: 'Order not completed or not found' }),
      {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }

  // 3. Resolve artifact storage key strictly from D1 (never trust URL or user input)
  const receipt = await env.DB
    .prepare('SELECT artifact_key, artifact_size_bytes FROM fulfillment_receipts WHERE order_id = ?')
    .bind(orderId)
    .first<{ artifact_key: string; artifact_size_bytes: number }>();

  const storageKey = receipt?.artifact_key;
  if (!storageKey) {
    return new Response(JSON.stringify({ error: 'Artifact record not found' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // 4. Stream artifact object from private R2 bucket
  const object = await env.ARTIFACTS_BUCKET.get(storageKey);
  if (!object || !object.body) {
    return new Response(JSON.stringify({ error: 'Artifact object not found in storage' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const headers = new Headers();
  headers.set('Content-Type', 'application/zip');
  headers.set('Content-Disposition', `attachment; filename="${orderId}.zip"`);
  headers.set('Content-Length', object.size.toString());
  if (object.httpEtag) {
    headers.set('ETag', object.httpEtag);
  }
  headers.set('Cache-Control', 'private, no-transform, max-age=3600');

  emitStructuredLog({
    event: 'download_served',
    order_id: orderId,
    size_bytes: object.size,
  });

  return new Response(object.body, {
    status: 200,
    headers,
  });
}

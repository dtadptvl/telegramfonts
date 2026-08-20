import { describe, it, expect } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker from '../src/index';

describe('Worker Routes', () => {
  it('GET /health returns 200 with status ok', async () => {
    const request = new Request('http://example.com/health', { method: 'GET' });
    const ctx = createExecutionContext();
    const response = await worker.fetch(request, env, ctx);
    await waitOnExecutionContext(ctx);

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    const body = await response.json();
    expect(body).toEqual({ status: 'ok' });
  });

  it('GET /ready returns 200 when D1 is available', async () => {
    const request = new Request('http://example.com/ready', { method: 'GET' });
    const ctx = createExecutionContext();
    const response = await worker.fetch(request, env, ctx);
    await waitOnExecutionContext(ctx);

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    const body = await response.json();
    expect(body).toEqual({ status: 'ready', database: 'connected' });
  });

  it('GET /ready returns 503 when D1 binding is missing', async () => {
    const request = new Request('http://example.com/ready', { method: 'GET' });
    const ctx = createExecutionContext();
    const mockEnv = { ...env, DB: undefined as unknown as D1Database };
    const response = await worker.fetch(request, mockEnv, ctx);
    await waitOnExecutionContext(ctx);

    expect(response.status).toBe(503);
    const body = await response.json() as { status: string; reason: string };
    expect(body.status).toBe('error');
  });

  it('GET /unknown returns 404', async () => {
    const request = new Request('http://example.com/unknown', { method: 'GET' });
    const ctx = createExecutionContext();
    const response = await worker.fetch(request, env, ctx);
    await waitOnExecutionContext(ctx);

    expect(response.status).toBe(404);
    const body = await response.json();
    expect(body).toEqual({ error: 'Not Found' });
  });

  it('POST /health returns 404 (method not allowed on health endpoint)', async () => {
    const request = new Request('http://example.com/health', { method: 'POST' });
    const ctx = createExecutionContext();
    const response = await worker.fetch(request, env, ctx);
    await waitOnExecutionContext(ctx);

    expect(response.status).toBe(404);
  });
});

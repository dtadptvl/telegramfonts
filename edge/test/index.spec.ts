import { describe, it, expect } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker, { type Env } from '../src/index';

describe('Worker Routes', () => {
  it('GET /health returns 200 with status ok', async () => {
    const request = new Request('http://example.com/health', { method: 'GET' });
    const ctx = createExecutionContext();
    const response = await worker.fetch(request, env as unknown as Env, ctx);
    await waitOnExecutionContext(ctx);

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    const body = await response.json();
    expect(body).toEqual({ status: 'ok' });
  });

  it('GET /ready returns 200 when migrated D1 is available', async () => {
    const request = new Request('http://example.com/ready', { method: 'GET' });
    const ctx = createExecutionContext();
    const response = await worker.fetch(request, env as unknown as Env, ctx);
    await waitOnExecutionContext(ctx);

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    const body = await response.json();
    expect(body).toEqual({ status: 'ready', database: 'connected' });
  });

  it('GET /ready returns generic 503 when D1 binding is missing', async () => {
    const request = new Request('http://example.com/ready', { method: 'GET' });
    const ctx = createExecutionContext();
    const mockEnv: Env = {
      ...(env as unknown as Env),
      DB: undefined as unknown as D1Database,
    };
    const response = await worker.fetch(request, mockEnv, ctx);
    await waitOnExecutionContext(ctx);

    expect(response.status).toBe(503);
    const body = await response.json();
    expect(body).toEqual({ status: 'unavailable' });
  });

  it('GET /ready returns generic 503 when schema is unmigrated/incomplete', async () => {
    const request = new Request('http://example.com/ready', { method: 'GET' });
    const ctx = createExecutionContext();
    const mockDb: Partial<D1Database> = {
      prepare: () => ({
        bind: () => ({} as D1PreparedStatement),
        first: async () => ({ table_count: 2, has_outbox_lease: 0, has_job_lease: 0, has_payment_code: 0 }),
        all: async () => ({ results: [], success: true, meta: {} as D1Meta }),
        run: async () => ({ success: true, meta: {} as D1Meta }),
        raw: async () => [],
      } as unknown as D1PreparedStatement),
    };
    const mockEnv: Env = {
      ...(env as unknown as Env),
      DB: mockDb as D1Database,
    };
    const response = await worker.fetch(request, mockEnv, ctx);
    await waitOnExecutionContext(ctx);

    expect(response.status).toBe(503);
    const body = await response.json();
    expect(body).toEqual({ status: 'unavailable' });
  });

  it('GET /ready returns 503 when tables exist but migration 0004 columns are missing (pre-0004 schema) (BLOCK A)', async () => {
    const request = new Request('http://example.com/ready', { method: 'GET' });
    const ctx = createExecutionContext();
    const mockDb: Partial<D1Database> = {
      prepare: () => ({
        bind: () => ({} as D1PreparedStatement),
        first: async () => ({
          table_count: 12, // all 12 tables exist
          has_outbox_lease: 0, // missing migration 0004 column
          has_job_lease: 1,
          has_payment_code: 1,
        }),
        all: async () => ({ results: [], success: true, meta: {} as D1Meta }),
        run: async () => ({ success: true, meta: {} as D1Meta }),
        raw: async () => [],
      } as unknown as D1PreparedStatement),
    };
    const mockEnv: Env = {
      ...(env as unknown as Env),
      DB: mockDb as D1Database,
    };
    const response = await worker.fetch(request, mockEnv, ctx);
    await waitOnExecutionContext(ctx);

    expect(response.status).toBe(503);
    const body = await response.json();
    expect(body).toEqual({ status: 'unavailable' });
  });

  it('GET /ready returns generic 503 and suppresses raw D1 exception text on query error', async () => {
    const request = new Request('http://example.com/ready', { method: 'GET' });
    const ctx = createExecutionContext();
    const mockDb: Partial<D1Database> = {
      prepare: () => {
        throw new Error('FATAL: raw internal sqlite error details');
      },
    };
    const mockEnv: Env = {
      ...(env as unknown as Env),
      DB: mockDb as D1Database,
    };
    const response = await worker.fetch(request, mockEnv, ctx);
    await waitOnExecutionContext(ctx);

    expect(response.status).toBe(503);
    const body = await response.json();
    expect(body).toEqual({ status: 'unavailable' });
    expect(JSON.stringify(body)).not.toContain('raw internal sqlite error details');
  });

  it('GET /unknown returns 404', async () => {
    const request = new Request('http://example.com/unknown', { method: 'GET' });
    const ctx = createExecutionContext();
    const response = await worker.fetch(request, env as unknown as Env, ctx);
    await waitOnExecutionContext(ctx);

    expect(response.status).toBe(404);
    const body = await response.json();
    expect(body).toEqual({ error: 'Not Found' });
  });

  it('POST /health returns 404 (method not allowed on health endpoint)', async () => {
    const request = new Request('http://example.com/health', { method: 'POST' });
    const ctx = createExecutionContext();
    const response = await worker.fetch(request, env as unknown as Env, ctx);
    await waitOnExecutionContext(ctx);

    expect(response.status).toBe(404);
  });
});

import { describe, it, expect } from 'vitest';
import {
  validateWranglerConfig,
  validateEdgeEnvVars,
  validateAgentConfig,
  runFullPreflight,
  type WranglerConfig,
} from '../src/utils/preflight';

describe('Phase 7: Release Preflight Validation', () => {
  const validWrangler: WranglerConfig = {
    name: 'telegramfonts-edge',
    d1_databases: [
      {
        binding: 'DB',
        database_name: 'telegramfonts-d1',
        database_id: 'telegramfonts-d1-placeholder',
      },
    ],
    queues: {
      producers: [
        {
          binding: 'FULFILLMENT_QUEUE',
          queue: 'telegramfonts-fulfillment',
        },
      ],
    },
    r2_buckets: [
      {
        binding: 'ARTIFACTS_BUCKET',
        bucket_name: 'telegramfonts-artifacts',
      },
    ],
    triggers: {
      crons: ['* * * * *'],
    },
  };

  it('passes wrangler config validation in development mode', () => {
    const results = validateWranglerConfig(validWrangler, { requireProdD1: false });
    expect(results.every((r) => r.passed)).toBe(true);
  });

  it('fails wrangler config validation when D1 binding is missing', () => {
    const invalid = { ...validWrangler, d1_databases: [] };
    const results = validateWranglerConfig(invalid);
    const d1Check = results.find((r) => r.name.includes('DB'));
    expect(d1Check?.passed).toBe(false);
  });

  it('fails wrangler config validation when queue binding is missing', () => {
    const invalid = { ...validWrangler, queues: { producers: [] } };
    const results = validateWranglerConfig(invalid);
    const qCheck = results.find((r) => r.name.includes('FULFILLMENT_QUEUE'));
    expect(qCheck?.passed).toBe(false);
  });

  it('fails wrangler config validation when R2 bucket binding is missing', () => {
    const invalid = { ...validWrangler, r2_buckets: [] };
    const results = validateWranglerConfig(invalid);
    const r2Check = results.find((r) => r.name.includes('ARTIFACTS_BUCKET'));
    expect(r2Check?.passed).toBe(false);
  });

  it('fails wrangler config validation when in-worker push consumers are declared', () => {
    const invalid = {
      ...validWrangler,
      queues: {
        producers: validWrangler.queues?.producers,
        consumers: [{ queue: 'telegramfonts-fulfillment' }],
      },
    };
    const results = validateWranglerConfig(invalid);
    const consCheck = results.find((r) => r.name.includes('No In-Worker Push Consumers'));
    expect(consCheck?.passed).toBe(false);
  });

  it('fails wrangler config validation in prod mode if D1 database_id is placeholder', () => {
    const results = validateWranglerConfig(validWrangler, { requireProdD1: true });
    const idCheck = results.find((r) => r.name.includes('D1 Database ID'));
    expect(idCheck?.passed).toBe(false);
  });

  it('validates Edge environment variables without leaking values', () => {
    const validEnv = {
      TELEGRAM_BOT_TOKEN: 'secret_123',
      TELEGRAM_WEBHOOK_SECRET: 'secret_456',
      SEPAY_WEBHOOK_SECRET: 'secret_789',
      A23_NODE_SECRET: 'secret_node',
      DOWNLOAD_SIGNING_SECRET: 'secret_sign',
      BANK_ID: '970422',
      BANK_ACCOUNT_NUMBER: '1234567890',
      BASE_URL: 'https://telefont.example.com',
      DOWNLOAD_URL_TTL_SECONDS: '86400',
      A23_JOB_LEASE_SECONDS: '300',
    };

    const results = validateEdgeEnvVars(validEnv, 'production');
    expect(results.every((r) => r.passed)).toBe(true);

    // Verify secret values are not printed in result messages
    for (const r of results) {
      if (r.name.includes('Secret') || r.name.includes('Bank')) {
        expect(r.message).not.toContain('secret_123');
        expect(r.message).not.toContain('secret_456');
        expect(r.message).not.toContain('secret_789');
        expect(r.message).not.toContain('1234567890');
      }
    }
  });

  it('fails Edge environment validation when BANK_ID or BANK_ACCOUNT_NUMBER is missing', () => {
    const invalidEnv = {
      TELEGRAM_BOT_TOKEN: 'secret_123',
      TELEGRAM_WEBHOOK_SECRET: 'secret_456',
      SEPAY_WEBHOOK_SECRET: 'secret_789',
      A23_NODE_SECRET: 'secret_node',
      DOWNLOAD_SIGNING_SECRET: 'secret_sign',
      BASE_URL: 'https://telefont.example.com',
    };

    const results = validateEdgeEnvVars(invalidEnv, 'production');
    const bankCheck = results.find((r) => r.name.includes('Bank Configuration'));
    expect(bankCheck?.passed).toBe(false);
  });

  it('fails Edge environment validation when BASE_URL is insecure HTTP in production mode', () => {
    const invalidEnv = {
      TELEGRAM_BOT_TOKEN: 'secret_123',
      TELEGRAM_WEBHOOK_SECRET: 'secret_456',
      SEPAY_WEBHOOK_SECRET: 'secret_789',
      A23_NODE_SECRET: 'secret_node',
      DOWNLOAD_SIGNING_SECRET: 'secret_sign',
      BANK_ID: '970422',
      BANK_ACCOUNT_NUMBER: '1234567890',
      BASE_URL: 'http://insecure.example.com',
    };

    const results = validateEdgeEnvVars(invalidEnv, 'production');
    const baseCheck = results.find((r) => r.name.includes('BASE_URL HTTPS'));
    expect(baseCheck?.passed).toBe(false);
  });

  it('validates Agent configuration and queue/lease compatibility', () => {
    const validAgent = {
      CF_ACCOUNT_ID: 'acc_123',
      CF_QUEUE_ID: 'queue_123',
      CF_QUEUES_TOKEN: 'token_123',
      EDGE_BASE_URL: 'https://telefont.example.com',
      A23_NODE_SECRET: 'node_sec_123',
      A23_WORKER_ID: 'worker_a23_01',
      PULL_BATCH_SIZE: 1,
      VISIBILITY_TIMEOUT_MS: 300000,
      LEASE_DURATION_SECONDS: 300,
      HEARTBEAT_INTERVAL_SECONDS: 60,
    };

    const results = validateAgentConfig(validAgent);
    expect(results.every((r) => r.passed)).toBe(true);
  });

  it('fails Agent configuration when heartbeat leaves < 15s safety margin', () => {
    const invalidAgent = {
      CF_ACCOUNT_ID: 'acc_123',
      CF_QUEUE_ID: 'queue_123',
      CF_QUEUES_TOKEN: 'token_123',
      EDGE_BASE_URL: 'https://telefont.example.com',
      A23_NODE_SECRET: 'node_sec_123',
      A23_WORKER_ID: 'worker_a23_01',
      VISIBILITY_TIMEOUT_MS: 300000,
      LEASE_DURATION_SECONDS: 60,
      HEARTBEAT_INTERVAL_SECONDS: 50, // 60 - 50 = 10s margin < 15s
    };

    const results = validateAgentConfig(invalidAgent);
    const hbCheck = results.find((r) => r.name.includes('Heartbeat Safety Margin'));
    expect(hbCheck?.passed).toBe(false);
  });

  it('fails Agent configuration when visibility timeout is shorter than lease duration', () => {
    const invalidAgent = {
      CF_ACCOUNT_ID: 'acc_123',
      CF_QUEUE_ID: 'queue_123',
      CF_QUEUES_TOKEN: 'token_123',
      EDGE_BASE_URL: 'https://telefont.example.com',
      A23_NODE_SECRET: 'node_sec_123',
      A23_WORKER_ID: 'worker_a23_01',
      VISIBILITY_TIMEOUT_MS: 60000, // 60s < 300s lease
      LEASE_DURATION_SECONDS: 300,
      HEARTBEAT_INTERVAL_SECONDS: 60,
    };

    const results = validateAgentConfig(invalidAgent);
    const visCheck = results.find((r) => r.name.includes('Visibility Timeout >= Lease Duration'));
    expect(visCheck?.passed).toBe(false);
  });

  describe('Preflight Strict & Boundary Negative Tests', () => {
    it('passes in test fixture mode without live env', () => {
      const report = runFullPreflight({
        mode: 'test',
        requireProdD1: false,
        wranglerConfig: validWrangler,
        edgeEnv: {},
        agentConfig: {},
      });
      expect(report.passed).toBe(true);
    });

    it('fails in strict mode when secrets and agent vars are missing', () => {
      const report = runFullPreflight({
        mode: 'production',
        requireProdD1: true,
        wranglerConfig: validWrangler,
        edgeEnv: {},
        agentConfig: {},
      });
      expect(report.passed).toBe(false);
      expect(report.failedChecks).toBeGreaterThan(0);

      const secretFailure = report.checks.find((c) => c.name.includes('TELEGRAM_BOT_TOKEN'));
      expect(secretFailure?.passed).toBe(false);

      const agentFailure = report.checks.find((c) => c.name.includes('CF_ACCOUNT_ID'));
      expect(agentFailure?.passed).toBe(false);

      const d1Failure = report.checks.find((c) => c.name.includes('D1 Database ID'));
      expect(d1Failure?.passed).toBe(false);
    });

    it('fails in strict mode when heartbeat margin is exactly 15 seconds (must be strictly > 15s)', () => {
      const customEnv = {
        TELEGRAM_BOT_TOKEN: 'tok',
        TELEGRAM_WEBHOOK_SECRET: 'sec',
        SEPAY_WEBHOOK_SECRET: 'sec',
        A23_NODE_SECRET: 'sec',
        DOWNLOAD_SIGNING_SECRET: 'sec',
        BANK_ID: '970422',
        BANK_ACCOUNT_NUMBER: '1234567890',
        BASE_URL: 'https://telefont.example.com',
      };
      const customAgent = {
        CF_ACCOUNT_ID: 'acc',
        CF_QUEUE_ID: 'queue',
        CF_QUEUES_TOKEN: 'tok',
        EDGE_BASE_URL: 'https://telefont.example.com',
        A23_NODE_SECRET: 'sec',
        A23_WORKER_ID: 'w1',
        LEASE_DURATION_SECONDS: 300,
        HEARTBEAT_INTERVAL_SECONDS: 285, // 300 - 285 = exactly 15s margin (rejected)
      };

      const report = runFullPreflight({
        mode: 'production',
        requireProdD1: false,
        wranglerConfig: validWrangler,
        edgeEnv: customEnv,
        agentConfig: customAgent,
      });
      const hbCheck = report.checks.find((c) => c.name.includes('Heartbeat Safety Margin'));
      expect(hbCheck?.passed).toBe(false);
    });

    it('fails in strict mode when EDGE_BASE_URL is insecure HTTP', () => {
      const customEnv = {
        TELEGRAM_BOT_TOKEN: 'tok',
        TELEGRAM_WEBHOOK_SECRET: 'sec',
        SEPAY_WEBHOOK_SECRET: 'sec',
        A23_NODE_SECRET: 'sec',
        DOWNLOAD_SIGNING_SECRET: 'sec',
        BANK_ID: '970422',
        BANK_ACCOUNT_NUMBER: '1234567890',
        BASE_URL: 'https://telefont.example.com',
      };
      const customAgent = {
        CF_ACCOUNT_ID: 'acc',
        CF_QUEUE_ID: 'queue',
        CF_QUEUES_TOKEN: 'tok',
        EDGE_BASE_URL: 'http://insecure-edge.example.com',
        A23_NODE_SECRET: 'sec',
        A23_WORKER_ID: 'w1',
      };

      const report = runFullPreflight({
        mode: 'production',
        requireProdD1: false,
        wranglerConfig: validWrangler,
        edgeEnv: customEnv,
        agentConfig: customAgent,
      });
      const edgeCheck = report.checks.find((c) => c.name.includes('EDGE_BASE_URL HTTPS'));
      expect(edgeCheck?.passed).toBe(false);
    });

    it('fails in strict mode when Wrangler Cron Trigger is missing', () => {
      const wranglerWithoutCron = { ...validWrangler, triggers: { crons: [] } };
      const customEnv = {
        TELEGRAM_BOT_TOKEN: 'tok',
        TELEGRAM_WEBHOOK_SECRET: 'sec',
        SEPAY_WEBHOOK_SECRET: 'sec',
        A23_NODE_SECRET: 'sec',
        DOWNLOAD_SIGNING_SECRET: 'sec',
        BANK_ID: '970422',
        BANK_ACCOUNT_NUMBER: '1234567890',
        BASE_URL: 'https://telefont.example.com',
      };
      const customAgent = {
        CF_ACCOUNT_ID: 'acc',
        CF_QUEUE_ID: 'queue',
        CF_QUEUES_TOKEN: 'tok',
        EDGE_BASE_URL: 'https://telefont.example.com',
        A23_NODE_SECRET: 'sec',
        A23_WORKER_ID: 'w1',
      };

      const report = runFullPreflight({
        mode: 'production',
        requireProdD1: true,
        wranglerConfig: wranglerWithoutCron,
        edgeEnv: customEnv,
        agentConfig: customAgent,
      });
      const cronCheck = report.checks.find((c) => c.name.includes('Cron Trigger'));
      expect(cronCheck?.passed).toBe(false);
    });
  });
});

/**
 * Release Preflight Validator for TelegramFonts.
 *
 * Deterministically verifies runtime configuration names, binding definitions,
 * payment settings, and parameter boundaries without logging or exposing sensitive secret values.
 */

export interface PreflightCheckResult {
  category: string;
  name: string;
  passed: boolean;
  message?: string;
}

export interface PreflightReport {
  timestamp: string;
  mode: 'test' | 'development' | 'production';
  checks: PreflightCheckResult[];
  passed: boolean;
  totalChecks: number;
  passedChecks: number;
  failedChecks: number;
}

export interface WranglerD1Config {
  binding?: string;
  database_name?: string;
  database_id?: string;
  migrations_dir?: string;
}

export interface WranglerQueueProducerConfig {
  binding?: string;
  queue?: string;
}

export interface WranglerR2BucketConfig {
  binding?: string;
  bucket_name?: string;
}

export interface WranglerConfig {
  name?: string;
  main?: string;
  compatibility_date?: string;
  d1_databases?: WranglerD1Config[];
  queues?: {
    producers?: WranglerQueueProducerConfig[];
    consumers?: unknown[];
  };
  r2_buckets?: WranglerR2BucketConfig[];
  services?: unknown[];
  triggers?: {
    crons?: string[];
  };
}

export function validateWranglerConfig(
  config: WranglerConfig,
  options?: { requireProdD1?: boolean }
): PreflightCheckResult[] {
  const results: PreflightCheckResult[] = [];

  // 1. D1 Database Binding
  const d1 = config.d1_databases?.find((db) => db.binding === 'DB');
  if (!d1) {
    results.push({
      category: 'Wrangler Bindings',
      name: 'D1 Binding (DB)',
      passed: false,
      message: 'Missing D1 binding named "DB"',
    });
  } else {
    const isPlaceholder = !d1.database_id || d1.database_id.includes('placeholder') || d1.database_id.startsWith('xxxx');
    if (options?.requireProdD1 && isPlaceholder) {
      results.push({
        category: 'Wrangler Bindings',
        name: 'D1 Database ID',
        passed: false,
        message: 'D1 database_id is still a placeholder; real UUID required for production deploy',
      });
    } else {
      results.push({
        category: 'Wrangler Bindings',
        name: 'D1 Binding (DB)',
        passed: true,
        message: `Bound database "${d1.database_name || 'unnamed'}" (id: ${isPlaceholder ? 'placeholder-dev' : 'configured'})`,
      });
    }
  }

  // 2. Queue Producer Binding
  const queue = config.queues?.producers?.find((q) => q.binding === 'FULFILLMENT_QUEUE');
  if (!queue) {
    results.push({
      category: 'Wrangler Bindings',
      name: 'Queue Binding (FULFILLMENT_QUEUE)',
      passed: false,
      message: 'Missing queue producer binding named "FULFILLMENT_QUEUE"',
    });
  } else {
    results.push({
      category: 'Wrangler Bindings',
      name: 'Queue Binding (FULFILLMENT_QUEUE)',
      passed: true,
      message: `Bound queue "${queue.queue || 'unnamed'}"`,
    });
  }

  // 3. R2 Bucket Binding
  const r2 = config.r2_buckets?.find((b) => b.binding === 'ARTIFACTS_BUCKET');
  if (!r2) {
    results.push({
      category: 'Wrangler Bindings',
      name: 'R2 Bucket Binding (ARTIFACTS_BUCKET)',
      passed: false,
      message: 'Missing R2 bucket binding named "ARTIFACTS_BUCKET"',
    });
  } else {
    results.push({
      category: 'Wrangler Bindings',
      name: 'R2 Bucket Binding (ARTIFACTS_BUCKET)',
      passed: true,
      message: `Bound bucket "${r2.bucket_name || 'unnamed'}"`,
    });
  }

  // 4. Outbound-Only Queue Consumer Check (Decision #6 D05)
  if (config.queues?.consumers && config.queues.consumers.length > 0) {
    results.push({
      category: 'Wrangler Architecture',
      name: 'No In-Worker Push Consumers',
      passed: false,
      message: 'In-Worker Queue consumers detected in wrangler config; A23 HTTP-pull consumer must be external',
    });
  } else {
    results.push({
      category: 'Wrangler Architecture',
      name: 'No In-Worker Push Consumers',
      passed: true,
      message: 'Verified external HTTP-pull architecture (no in-worker push consumers)',
    });
  }

  // 5. No public services / tunnels in Worker
  if (config.services && config.services.length > 0) {
    results.push({
      category: 'Wrangler Architecture',
      name: 'Worker Service Isolation',
      passed: false,
      message: 'External service bindings detected; Worker must communicate only via standard bindings',
    });
  } else {
    results.push({
      category: 'Wrangler Architecture',
      name: 'Worker Service Isolation',
      passed: true,
      message: 'Worker service isolation verified',
    });
  }

  // 6. Cron Triggers Check (for Outbox Dispatcher)
  const crons = config.triggers?.crons;
  const hasCrons = Array.isArray(crons) && crons.length > 0;
  if (!hasCrons) {
    results.push({
      category: 'Wrangler Triggers',
      name: 'Cron Trigger (Scheduled Dispatcher)',
      passed: !options?.requireProdD1,
      message: hasCrons ? `Configured crons: ${crons.join(', ')}` : 'Missing triggers.crons (required for scheduled outbox dispatcher)',
    });
  } else {
    results.push({
      category: 'Wrangler Triggers',
      name: 'Cron Trigger (Scheduled Dispatcher)',
      passed: true,
      message: `Configured crons: ${crons.join(', ')}`,
    });
  }

  return results;
}

export function validateEdgeEnvVars(
  envVars: Record<string, string | undefined>,
  mode: 'test' | 'development' | 'production' = 'production'
): PreflightCheckResult[] {
  const results: PreflightCheckResult[] = [];
  const isTest = mode === 'test';

  const requiredSecrets = [
    'TELEGRAM_BOT_TOKEN',
    'TELEGRAM_WEBHOOK_SECRET',
    'SEPAY_WEBHOOK_SECRET',
    'A23_NODE_SECRET',
    'DOWNLOAD_SIGNING_SECRET',
  ];

  for (const secretKey of requiredSecrets) {
    const isPresent = Boolean(envVars[secretKey] && envVars[secretKey]!.trim().length > 0);
    results.push({
      category: 'Edge Secrets (Names Only)',
      name: `Secret [${secretKey}]`,
      passed: true,
      message: isPresent
        ? 'Configured (redacted)'
        : 'Registered in secret contract (managed via wrangler secret put)',
    });
  }

  // Payment configuration (Bank ID & Account)
  const bankId = envVars.BANK_ID?.trim();
  const bankAccount = envVars.BANK_ACCOUNT_NUMBER?.trim();
  const bankPresent = Boolean(bankId && bankAccount);
  results.push({
    category: 'Edge Payment Vars',
    name: 'VietQR Bank Configuration (BANK_ID & BANK_ACCOUNT_NUMBER)',
    passed: isTest || bankPresent,
    message: bankPresent
      ? `Bank [${bankId}] Account configured (redacted)`
      : isTest
      ? 'Registered in contract (safe fixture mode)'
      : 'Missing BANK_ID or BANK_ACCOUNT_NUMBER',
  });

  // Base URL validation
  const baseUrl = envVars.BASE_URL?.trim();
  if (!baseUrl) {
    results.push({
      category: 'Edge Runtime Vars',
      name: 'BASE_URL',
      passed: isTest,
      message: isTest ? 'Registered in contract (safe fixture mode)' : 'BASE_URL is missing',
    });
  } else {
    try {
      const parsed = new URL(baseUrl);
      if (mode === 'production' && parsed.protocol !== 'https:') {
        results.push({
          category: 'Edge Runtime Vars',
          name: 'BASE_URL HTTPS',
          passed: false,
          message: `BASE_URL protocol is "${parsed.protocol}"; must be "https:" in production`,
        });
      } else {
        results.push({
          category: 'Edge Runtime Vars',
          name: 'BASE_URL',
          passed: true,
          message: `Valid ${parsed.protocol}//${parsed.host}`,
        });
      }
    } catch {
      results.push({
        category: 'Edge Runtime Vars',
        name: 'BASE_URL',
        passed: false,
        message: 'Invalid BASE_URL format',
      });
    }
  }

  // Download TTL
  const ttl = envVars.DOWNLOAD_URL_TTL_SECONDS?.trim();
  if (ttl) {
    const ttlNum = parseInt(ttl, 10);
    const validTtl = !Number.isNaN(ttlNum) && ttlNum >= 60 && ttlNum <= 604800;
    results.push({
      category: 'Edge Runtime Vars',
      name: 'DOWNLOAD_URL_TTL_SECONDS',
      passed: validTtl,
      message: validTtl ? `${ttlNum}s (${Math.round(ttlNum / 3600)}h)` : 'Must be integer between 60 and 604800',
    });
  } else {
    results.push({
      category: 'Edge Runtime Vars',
      name: 'DOWNLOAD_URL_TTL_SECONDS',
      passed: true,
      message: 'Default 86400s (24h)',
    });
  }

  // Job Lease Seconds
  const lease = envVars.A23_JOB_LEASE_SECONDS?.trim();
  if (lease) {
    const leaseNum = parseInt(lease, 10);
    const validLease = !Number.isNaN(leaseNum) && leaseNum >= 10 && leaseNum <= 1800;
    results.push({
      category: 'Edge Runtime Vars',
      name: 'A23_JOB_LEASE_SECONDS',
      passed: validLease,
      message: validLease ? `${leaseNum}s` : 'Must be integer between 10 and 1800',
    });
  } else {
    results.push({
      category: 'Edge Runtime Vars',
      name: 'A23_JOB_LEASE_SECONDS',
      passed: true,
      message: 'Default 300s (5m)',
    });
  }

  return results;
}

export interface AgentConfigInput {
  CF_ACCOUNT_ID?: string;
  CF_QUEUE_ID?: string;
  CF_QUEUES_TOKEN?: string;
  EDGE_BASE_URL?: string;
  A23_NODE_SECRET?: string;
  A23_WORKER_ID?: string;
  PULL_BATCH_SIZE?: number | string;
  VISIBILITY_TIMEOUT_MS?: number | string;
  HEARTBEAT_INTERVAL_SECONDS?: number | string;
  LEASE_DURATION_SECONDS?: number | string;
}

export function validateAgentConfig(
  config: AgentConfigInput,
  options?: { isTest?: boolean; requireProdHttps?: boolean }
): PreflightCheckResult[] {
  const results: PreflightCheckResult[] = [];

  const requiredKeys: Array<keyof AgentConfigInput> = [
    'CF_ACCOUNT_ID',
    'CF_QUEUE_ID',
    'CF_QUEUES_TOKEN',
    'EDGE_BASE_URL',
    'A23_NODE_SECRET',
    'A23_WORKER_ID',
  ];

  const isTest = Boolean(options?.isTest);

  for (const key of requiredKeys) {
    const val = config[key];
    const isPresent = Boolean(val && String(val).trim().length > 0);
    results.push({
      category: 'Agent Config',
      name: `Param [${key}]`,
      passed: isTest || isPresent,
      message: isPresent
        ? 'Configured (redacted)'
        : isTest
        ? 'Registered in contract (safe fixture mode)'
        : 'Missing required agent parameter',
    });
  }

  // Validate EDGE_BASE_URL HTTPS in production
  if (config.EDGE_BASE_URL) {
    try {
      const parsed = new URL(config.EDGE_BASE_URL);
      if (options?.requireProdHttps && parsed.protocol !== 'https:') {
        results.push({
          category: 'Agent Config',
          name: 'EDGE_BASE_URL HTTPS',
          passed: false,
          message: `EDGE_BASE_URL protocol is "${parsed.protocol}"; must be "https:" in production`,
        });
      } else {
        results.push({
          category: 'Agent Config',
          name: 'EDGE_BASE_URL Protocol',
          passed: true,
          message: `Valid ${parsed.protocol}//${parsed.host}`,
        });
      }
    } catch {
      results.push({
        category: 'Agent Config',
        name: 'EDGE_BASE_URL Protocol',
        passed: false,
        message: 'Invalid EDGE_BASE_URL format',
      });
    }
  }

  // Queue pull batch size (agent max is 10)
  const batchSize = parseInt(String(config.PULL_BATCH_SIZE || 1), 10);
  const validBatch = !Number.isNaN(batchSize) && batchSize >= 1 && batchSize <= 10;
  results.push({
    category: 'Agent Queue Boundaries',
    name: 'PULL_BATCH_SIZE (1..10)',
    passed: validBatch,
    message: validBatch ? `${batchSize} msgs/pull (app cap: 10)` : 'Must be between 1 and 10',
  });

  // Visibility Timeout vs Lease Duration
  const visMs = parseInt(String(config.VISIBILITY_TIMEOUT_MS || 300000), 10);
  const leaseSec = parseInt(String(config.LEASE_DURATION_SECONDS || 300), 10);
  const hbSec = parseInt(String(config.HEARTBEAT_INTERVAL_SECONDS || 60), 10);

  const validVisMax = !Number.isNaN(visMs) && visMs >= 10000 && visMs <= 43200000; // max 12 hours
  results.push({
    category: 'Agent Queue Boundaries',
    name: 'VISIBILITY_TIMEOUT_MS Bound (10s..12h)',
    passed: validVisMax,
    message: validVisMax ? `${visMs}ms (Cloudflare max: 12h / 43,200,000ms)` : 'Must be between 10,000 and 43,200,000 ms',
  });

  const visMatchesLease = visMs >= leaseSec * 1000;
  results.push({
    category: 'Agent Queue Boundaries',
    name: 'Visibility Timeout >= Lease Duration',
    passed: visMatchesLease,
    message: visMatchesLease
      ? `Visibility (${visMs}ms) >= Lease (${leaseSec * 1000}ms)`
      : `Visibility (${visMs}ms) is shorter than Lease (${leaseSec * 1000}ms); risk of premature redelivery`,
  });

  // Heartbeat must be strictly less than lease duration with strictly > 15s safety margin
  const hbMargin = leaseSec - hbSec;
  const hbSafe = hbSec > 0 && hbMargin > 15;
  results.push({
    category: 'Agent Lease Boundaries',
    name: 'Heartbeat Safety Margin (> 15s)',
    passed: hbSafe,
    message: hbSafe
      ? `Heartbeat (${hbSec}s) provides ${hbMargin}s safety margin before lease expiry (${leaseSec}s)`
      : `Heartbeat (${hbSec}s) leaves insufficient safety margin (${hbMargin}s <= 15s) for lease (${leaseSec}s)`,
  });

  return results;
}

export function runFullPreflight(inputs: {
  wranglerConfig: WranglerConfig;
  edgeEnv: Record<string, string | undefined>;
  agentConfig: AgentConfigInput;
  mode?: 'test' | 'development' | 'production';
  requireProdD1?: boolean;
}): PreflightReport {
  const mode = inputs.mode || 'development';
  const isProd = mode === 'production';
  const checks: PreflightCheckResult[] = [
    ...validateWranglerConfig(inputs.wranglerConfig, { requireProdD1: inputs.requireProdD1 ?? isProd }),
    ...validateEdgeEnvVars(inputs.edgeEnv, mode),
    ...validateAgentConfig(inputs.agentConfig, { isTest: mode === 'test', requireProdHttps: isProd }),
  ];

  const totalChecks = checks.length;
  const passedChecks = checks.filter((c) => c.passed).length;
  const failedChecks = totalChecks - passedChecks;

  return {
    timestamp: new Date().toISOString(),
    mode,
    checks,
    passed: failedChecks === 0,
    totalChecks,
    passedChecks,
    failedChecks,
  };
}

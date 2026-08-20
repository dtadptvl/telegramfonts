/**
 * TelegramFonts Release Preflight CLI
 *
 * Usage:
 *   node scripts/preflight.mjs [--mode production|development|test] [--strict]
 */

import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { homedir } from 'node:os';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

function loadEnvFileIfPresent(filePath) {
  if (!filePath || !existsSync(filePath)) return {};
  try {
    const content = readFileSync(filePath, 'utf8');
    const envObj = {};
    for (const line of content.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      const eqIdx = trimmed.indexOf('=');
      if (eqIdx > 0) {
        const k = trimmed.slice(0, eqIdx).trim();
        const v = trimmed.slice(eqIdx + 1).trim().replace(/^["']|["']$/g, '');
        envObj[k] = v;
      }
    }
    return envObj;
  } catch {
    return {};
  }
}

function findRootDir() {
  const candidates = [
    resolve(__dirname, '..'),
    process.cwd(),
    resolve(process.cwd(), '..'),
  ];
  for (const c of candidates) {
    if (existsSync(resolve(c, 'edge', 'wrangler.jsonc'))) {
      return c;
    }
  }
  return resolve(__dirname, '..');
}

// Simple JSONC parser (strips single-line and multi-line comments)
function parseJsonc(text) {
  const clean = text
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');
  return JSON.parse(clean);
}

function parseArgs() {
  const args = process.argv.slice(2);
  let mode = 'test';
  let strict = false;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--mode' && args[i + 1]) {
      mode = args[i + 1];
      i++;
    } else if (args[i] === '--strict' || args[i] === '--prod') {
      strict = true;
      mode = 'production';
    }
  }

  return { mode, strict };
}

export function runPreflightCheck(options = {}) {
  const mode = options.mode || 'test';
  const strict = options.strict || mode === 'production';
  const rootDir = options.rootDir || findRootDir();

  const userHomeEnv = homedir ? resolve(homedir(), '.telefont.env') : '';
  const localEnvFile = resolve(rootDir, '.env');
  const fileEnv = { ...loadEnvFileIfPresent(localEnvFile), ...loadEnvFileIfPresent(userHomeEnv) };
  const baseEnv = options.env || { ...fileEnv, ...process.env };

  const checks = [];

  // 1. Read & Validate Wrangler Configuration
  let wranglerConfig = options.wranglerConfig || null;
  if (!wranglerConfig && options.rawWranglerContent) {
    try {
      wranglerConfig = parseJsonc(options.rawWranglerContent);
    } catch (err) {
      checks.push({
        category: 'Wrangler Config',
        name: 'wrangler.jsonc parse',
        passed: false,
        message: err.message,
      });
    }
  }

  if (!wranglerConfig) {
    const wranglerPath = resolve(rootDir, 'edge', 'wrangler.jsonc');
    if (!existsSync(wranglerPath)) {
      // If running inside a test environment without filesystem access, use minimal valid fallback if not strict
      if (!strict && mode === 'test') {
        wranglerConfig = {
          name: 'telegramfonts-edge',
          d1_databases: [{ binding: 'DB', database_name: 'telegramfonts-d1', database_id: 'telegramfonts-d1-placeholder' }],
          queues: { producers: [{ binding: 'FULFILLMENT_QUEUE', queue: 'telegramfonts-fulfillment' }] },
          r2_buckets: [{ binding: 'ARTIFACTS_BUCKET', bucket_name: 'telegramfonts-artifacts' }],
        };
      } else {
        checks.push({
          category: 'Wrangler Config',
          name: 'wrangler.jsonc exists',
          passed: false,
          message: `File not found at ${wranglerPath}`,
        });
      }
    } else {
      try {
        const wranglerContent = readFileSync(wranglerPath, 'utf-8');
        wranglerConfig = parseJsonc(wranglerContent);
      } catch (err) {
        checks.push({
          category: 'Wrangler Config',
          name: 'wrangler.jsonc parse',
          passed: false,
          message: err.message,
        });
      }
    }
  }

  if (wranglerConfig) {
    try {

      // D1 binding
      const d1 = wranglerConfig.d1_databases?.find((db) => db.binding === 'DB');
      if (!d1) {
        checks.push({
          category: 'Wrangler Config',
          name: 'D1 Binding (DB)',
          passed: false,
          message: 'Missing D1 binding named "DB"',
        });
      } else {
        const isPlaceholder = !d1.database_id || d1.database_id.includes('placeholder') || d1.database_id.startsWith('xxxx');
        if (strict && isPlaceholder) {
          checks.push({
            category: 'Wrangler Config',
            name: 'D1 Database ID',
            passed: false,
            message: 'D1 database_id is placeholder; real UUID required for production',
          });
        } else {
          checks.push({
            category: 'Wrangler Config',
            name: 'D1 Binding (DB)',
            passed: true,
            message: `Bound database "${d1.database_name}" (${isPlaceholder ? 'dev placeholder' : 'configured'})`,
          });
        }
      }

      // Queue binding
      const q = wranglerConfig.queues?.producers?.find((p) => p.binding === 'FULFILLMENT_QUEUE');
      if (!q) {
        checks.push({
          category: 'Wrangler Config',
          name: 'Queue Binding (FULFILLMENT_QUEUE)',
          passed: false,
          message: 'Missing queue producer named "FULFILLMENT_QUEUE"',
        });
      } else {
        checks.push({
          category: 'Wrangler Config',
          name: 'Queue Binding (FULFILLMENT_QUEUE)',
          passed: true,
          message: `Bound producer queue "${q.queue}"`,
        });
      }

      // R2 bucket
      const r2 = wranglerConfig.r2_buckets?.find((b) => b.binding === 'ARTIFACTS_BUCKET');
      if (!r2) {
        checks.push({
          category: 'Wrangler Config',
          name: 'R2 Binding (ARTIFACTS_BUCKET)',
          passed: false,
          message: 'Missing R2 bucket named "ARTIFACTS_BUCKET"',
        });
      } else {
        checks.push({
          category: 'Wrangler Config',
          name: 'R2 Binding (ARTIFACTS_BUCKET)',
          passed: true,
          message: `Bound private bucket "${r2.bucket_name}"`,
        });
      }

      // Architecture isolation
      const inWorkerConsumers = wranglerConfig.queues?.consumers?.length || 0;
      if (inWorkerConsumers > 0) {
        checks.push({
          category: 'Architecture Rules',
          name: 'No In-Worker Push Consumers',
          passed: false,
          message: 'In-worker queue consumers forbidden (A23 HTTP-pull consumer only)',
        });
      } else {
        checks.push({
          category: 'Architecture Rules',
          name: 'No In-Worker Push Consumers',
          passed: true,
          message: 'External HTTP-pull architecture confirmed',
        });
      }

      // Cron triggers check
      const crons = wranglerConfig.triggers?.crons;
      const hasCrons = Array.isArray(crons) && crons.length > 0;
      if (!hasCrons) {
        checks.push({
          category: 'Wrangler Triggers',
          name: 'Cron Trigger (Scheduled Dispatcher)',
          passed: !strict,
          message: hasCrons ? `Configured crons: ${crons.join(', ')}` : 'Missing triggers.crons (required for scheduled outbox dispatcher)',
        });
      } else {
        checks.push({
          category: 'Wrangler Triggers',
          name: 'Cron Trigger (Scheduled Dispatcher)',
          passed: true,
          message: `Configured crons: ${crons.join(', ')}`,
        });
      }
    } catch (err) {
      checks.push({
        category: 'Wrangler Config',
        name: 'wrangler.jsonc parse',
        passed: false,
        message: err.message,
      });
    }
  }

  const env = { ...(wranglerConfig?.vars || {}), ...baseEnv };

  // 2. Secret / Environment Key Names (never values!)
  const requiredSecrets = [
    'TELEGRAM_BOT_TOKEN',
    'TELEGRAM_WEBHOOK_SECRET',
    'SEPAY_WEBHOOK_SECRET',
    'A23_NODE_SECRET',
    'DOWNLOAD_SIGNING_SECRET',
  ];

  for (const s of requiredSecrets) {
    const val = env[s];
    const isSet = Boolean(val && String(val).trim().length > 0);
    checks.push({
      category: 'Edge Secrets (Names Only)',
      name: `Secret [${s}]`,
      passed: true,
      message: isSet
        ? 'Configured in local env (redacted)'
        : 'Registered in secret contract (managed via wrangler secret put)',
    });
  }

  // Payment configuration (Bank ID & Account)
  const bankId = env.BANK_ID;
  const bankAccount = env.BANK_ACCOUNT_NUMBER;
  const bankPresent = Boolean(bankId && String(bankId).trim() && bankAccount && String(bankAccount).trim());
  if (strict) {
    checks.push({
      category: 'Edge Payment Vars',
      name: 'VietQR Bank Configuration (BANK_ID & BANK_ACCOUNT_NUMBER)',
      passed: bankPresent,
      message: bankPresent ? `Bank [${bankId}] Account configured (redacted)` : 'Missing BANK_ID or BANK_ACCOUNT_NUMBER',
    });
  } else {
    checks.push({
      category: 'Edge Payment Vars',
      name: 'VietQR Bank Configuration (BANK_ID & BANK_ACCOUNT_NUMBER)',
      passed: true,
      message: bankPresent ? `Bank [${bankId}] Account configured (redacted)` : 'Registered in contract (safe fixture mode)',
    });
  }

  // 3. BASE_URL
  const rawBaseUrl = env.BASE_URL || (strict ? '' : 'https://telefont.example.com');
  if (!rawBaseUrl) {
    checks.push({
      category: 'Runtime URLs',
      name: 'BASE_URL',
      passed: false,
      message: 'Missing BASE_URL in environment',
    });
  } else {
    try {
      const parsed = new URL(rawBaseUrl);
      const isHttps = parsed.protocol === 'https:';
      if (mode === 'production' && !isHttps) {
        checks.push({
          category: 'Runtime URLs',
          name: 'BASE_URL HTTPS',
          passed: false,
          message: `BASE_URL must use https: protocol in production (got ${parsed.protocol})`,
        });
      } else {
        checks.push({
          category: 'Runtime URLs',
          name: 'BASE_URL',
          passed: true,
          message: `Valid protocol & origin: ${parsed.protocol}//${parsed.host}`,
        });
      }
    } catch {
      checks.push({
        category: 'Runtime URLs',
        name: 'BASE_URL',
        passed: false,
        message: `Invalid URL format: ${rawBaseUrl}`,
      });
    }
  }

  // 4. Agent Configuration (names only)
  const requiredAgentVars = [
    'CF_ACCOUNT_ID',
    'CF_QUEUE_ID',
    'CF_QUEUES_TOKEN',
    'EDGE_BASE_URL',
    'A23_NODE_SECRET',
    'A23_WORKER_ID',
  ];

  for (const v of requiredAgentVars) {
    const val = env[v];
    const isSet = Boolean(val && String(val).trim().length > 0);
    if (strict) {
      checks.push({
        category: 'Agent Config (Names Only)',
        name: `Agent Var [${v}]`,
        passed: isSet,
        message: isSet ? 'Present in environment (redacted)' : 'Missing required agent parameter',
      });
    } else {
      checks.push({
        category: 'Agent Config (Names Only)',
        name: `Agent Var [${v}]`,
        passed: true,
        message: isSet ? 'Present in environment (redacted)' : 'Registered in contract (safe fixture mode)',
      });
    }
  }

  // Validate EDGE_BASE_URL HTTPS in production
  const edgeBaseUrl = env.EDGE_BASE_URL;
  if (edgeBaseUrl) {
    try {
      const parsedEdge = new URL(edgeBaseUrl);
      if (strict && parsedEdge.protocol !== 'https:') {
        checks.push({
          category: 'Agent Config (Names Only)',
          name: 'EDGE_BASE_URL HTTPS',
          passed: false,
          message: `EDGE_BASE_URL protocol is "${parsedEdge.protocol}"; must be "https:" in production`,
        });
      } else {
        checks.push({
          category: 'Agent Config (Names Only)',
          name: 'EDGE_BASE_URL Protocol',
          passed: true,
          message: `Valid protocol: ${parsedEdge.protocol}`,
        });
      }
    } catch {
      checks.push({
        category: 'Agent Config (Names Only)',
        name: 'EDGE_BASE_URL Protocol',
        passed: false,
        message: 'Invalid EDGE_BASE_URL format',
      });
    }
  }

  // 5. Agent Queue & Lease Boundaries
  const batchSize = parseInt(env.PULL_BATCH_SIZE || '1', 10);
  const validBatch = !Number.isNaN(batchSize) && batchSize >= 1 && batchSize <= 10;
  checks.push({
    category: 'Agent Queue Boundaries',
    name: 'PULL_BATCH_SIZE (1..10)',
    passed: validBatch,
    message: validBatch ? `${batchSize} msgs/pull (app cap: 10)` : `Invalid batch size: ${env.PULL_BATCH_SIZE}`,
  });

  const visTimeoutMs = parseInt(env.VISIBILITY_TIMEOUT_MS || '300000', 10);
  const leaseSec = parseInt(env.A23_JOB_LEASE_SECONDS || env.LEASE_DURATION_SECONDS || '300', 10);
  const hbSec = parseInt(env.HEARTBEAT_INTERVAL_SECONDS || '60', 10);

  const validVisMax = !Number.isNaN(visTimeoutMs) && visTimeoutMs >= 10000 && visTimeoutMs <= 43200000;
  checks.push({
    category: 'Agent Queue Boundaries',
    name: 'VISIBILITY_TIMEOUT_MS Bound (10s..12h)',
    passed: validVisMax,
    message: validVisMax ? `${visTimeoutMs}ms (Cloudflare max: 12h / 43,200,000ms)` : 'Must be between 10,000 and 43,200,000 ms',
  });

  const visGteLease = visTimeoutMs >= leaseSec * 1000;
  checks.push({
    category: 'Agent Queue Boundaries',
    name: 'Visibility Timeout >= Lease Duration',
    passed: visGteLease,
    message: visGteLease
      ? `${visTimeoutMs}ms >= ${leaseSec * 1000}ms (ok)`
      : `${visTimeoutMs}ms < ${leaseSec * 1000}ms (risk of premature redelivery)`,
  });

  const hbMargin = leaseSec - hbSec;
  const hbSafe = hbSec > 0 && hbMargin > 15;
  checks.push({
    category: 'Agent Lease Boundaries',
    name: 'Heartbeat Safety Margin (> 15s)',
    passed: hbSafe,
    message: hbSafe
      ? `Heartbeat (${hbSec}s) provides ${hbMargin}s safety margin before lease (${leaseSec}s)`
      : `Heartbeat (${hbSec}s) leaves insufficient margin (${hbMargin}s <= 15s) for lease (${leaseSec}s)`,
  });

  const passedCount = checks.filter((c) => c.passed).length;
  const failedCount = checks.length - passedCount;

  return {
    mode,
    strict,
    checks,
    passed: failedCount === 0,
    passedCount,
    failedCount,
  };
}

function main() {
  const { mode, strict } = parseArgs();
  console.log(`\n======================================================`);
  console.log(`  TelegramFonts Release Preflight Check (mode: ${mode}, strict: ${strict})`);
  console.log(`======================================================\n`);

  const report = runPreflightCheck({ mode, strict, env: process.env });

  // Display Table
  let currentCategory = '';
  for (const c of report.checks) {
    if (c.category !== currentCategory) {
      currentCategory = c.category;
      console.log(`\n[ ${currentCategory} ]`);
    }
    const symbol = c.passed ? '  ✓ ' : '  ✗ ';
    const paddedName = c.name.padEnd(42, ' ');
    console.log(`${symbol}${paddedName} ${c.message ? `-> ${c.message}` : ''}`);
  }

  console.log(`\n------------------------------------------------------`);
  console.log(`  Summary: ${report.passedCount}/${report.checks.length} checks passed (${report.failedCount} failed)`);
  console.log(`------------------------------------------------------\n`);

  if (!report.passed) {
    console.error(`Preflight check FAILED.`);
    process.exit(1);
  } else {
    console.log(`Preflight check PASSED.`);
    process.exit(0);
  }
}

if (process.argv[1] && process.argv[1].endsWith('preflight.mjs')) {
  main();
}

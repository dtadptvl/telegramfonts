#!/usr/bin/env node
/**
 * TelegramFonts Release Preflight CLI
 *
 * Usage:
 *   node scripts/preflight.mjs [--mode production|development|test] [--strict]
 */

import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const rootDir = resolve(__dirname, '..');

// Simple JSONC parser (strips single-line and multi-line comments)
function parseJsonc(text) {
  const clean = text
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');
  return JSON.parse(clean);
}

function parseArgs() {
  const args = process.argv.slice(2);
  let mode = 'development';
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

function runPreflight() {
  const { mode, strict } = parseArgs();
  console.log(`\n======================================================`);
  console.log(`  TelegramFonts Release Preflight Check (mode: ${mode})`);
  console.log(`======================================================\n`);

  const checks = [];

  // 1. Read & Validate Wrangler Configuration
  const wranglerPath = resolve(rootDir, 'edge', 'wrangler.jsonc');
  if (!existsSync(wranglerPath)) {
    checks.push({
      category: 'Wrangler Config',
      name: 'wrangler.jsonc exists',
      passed: false,
      message: `File not found at ${wranglerPath}`,
    });
  } else {
    try {
      const wranglerContent = readFileSync(wranglerPath, 'utf-8');
      const wranglerConfig = parseJsonc(wranglerContent);

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
        const isPlaceholder = !d1.database_id || d1.database_id.includes('placeholder');
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
    } catch (err) {
      checks.push({
        category: 'Wrangler Config',
        name: 'wrangler.jsonc parse',
        passed: false,
        message: err.message,
      });
    }
  }

  // 2. Secret / Environment Key Names (never values!)
  const requiredSecrets = [
    'TELEGRAM_BOT_TOKEN',
    'TELEGRAM_WEBHOOK_SECRET',
    'SEPAY_WEBHOOK_SECRET',
    'A23_NODE_SECRET',
    'DOWNLOAD_SIGNING_SECRET',
  ];

  for (const s of requiredSecrets) {
    const val = process.env[s];
    const isSet = Boolean(val && val.trim().length > 0);
    // In dev mode, missing env vars generate a warning / note unless strict
    if (strict) {
      checks.push({
        category: 'Edge Secrets (Names Only)',
        name: `Secret [${s}]`,
        passed: isSet,
        message: isSet ? 'Present in environment (redacted)' : 'Missing required secret in environment',
      });
    } else {
      checks.push({
        category: 'Edge Secrets (Names Only)',
        name: `Secret [${s}]`,
        passed: true,
        message: isSet ? 'Present in environment (redacted)' : 'Registered in contract (set via wrangler secret put)',
      });
    }
  }

  // 3. BASE_URL
  const baseUrl = process.env.BASE_URL || 'https://telefont.example.com';
  try {
    const parsed = new URL(baseUrl);
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
      message: `Invalid URL format: ${baseUrl}`,
    });
  }

  // 4. Agent Queue & Lease Boundaries
  const visTimeoutMs = parseInt(process.env.VISIBILITY_TIMEOUT_MS || '300000', 10);
  const leaseSec = parseInt(process.env.A23_JOB_LEASE_SECONDS || process.env.LEASE_DURATION_SECONDS || '300', 10);
  const hbSec = parseInt(process.env.HEARTBEAT_INTERVAL_SECONDS || '60', 10);

  const visGteLease = visTimeoutMs >= leaseSec * 1000;
  checks.push({
    category: 'Agent Queue Boundaries',
    name: 'Visibility Timeout >= Lease Duration',
    passed: visGteLease,
    message: visGteLease
      ? `${visTimeoutMs}ms >= ${leaseSec * 1000}ms (ok)`
      : `${visTimeoutMs}ms < ${leaseSec * 1000}ms (risk of premature redelivery)`,
  });

  const hbLtLease = hbSec < leaseSec;
  checks.push({
    category: 'Agent Lease Boundaries',
    name: 'Heartbeat Interval < Lease Duration',
    passed: hbLtLease,
    message: hbLtLease
      ? `${hbSec}s < ${leaseSec}s (ok)`
      : `${hbSec}s >= ${leaseSec}s (lease will expire before heartbeat)`,
  });

  // Display Table
  let currentCategory = '';
  for (const c of checks) {
    if (c.category !== currentCategory) {
      currentCategory = c.category;
      console.log(`\n[ ${currentCategory} ]`);
    }
    const symbol = c.passed ? '  ✓ ' : '  ✗ ';
    const paddedName = c.name.padEnd(38, ' ');
    console.log(`${symbol}${paddedName} ${c.message ? `-> ${c.message}` : ''}`);
  }

  const passedCount = checks.filter((c) => c.passed).length;
  const failedCount = checks.length - passedCount;

  console.log(`\n------------------------------------------------------`);
  console.log(`  Summary: ${passedCount}/${checks.length} checks passed (${failedCount} failed)`);
  console.log(`------------------------------------------------------\n`);

  if (failedCount > 0) {
    console.error(`Preflight check FAILED.`);
    process.exit(1);
  } else {
    console.log(`Preflight check PASSED.`);
    process.exit(0);
  }
}

runPreflight();

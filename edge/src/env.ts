export interface Env {
  // Bindings
  DB: D1Database;
  FULFILLMENT_QUEUE: Queue<unknown>;
  ARTIFACTS_BUCKET: R2Bucket;

  // Secrets & Config (injected via Cloudflare secrets / .dev.vars, not committed)
  ENVIRONMENT?: string;
  BASE_URL?: string; // e.g. 'https://api.telefont.example.com'
  TELEGRAM_BOT_TOKEN?: string;
  TELEGRAM_WEBHOOK_SECRET?: string;
  SEPAY_API_TOKEN?: string;
  SEPAY_WEBHOOK_SECRET?: string;

  // Internal Node Auth (A23 private compute node boundary)
  A23_NODE_SECRET?: string;
  A23_JOB_LEASE_SECONDS?: string;
  // T-FAST30-A23-FIX F4: job-age backstop in milliseconds (default 2100000 =
  // 35 min = 30-min FAST_30 job wall + 5-min operational margin). Heartbeat
  // extensions are refused and exhausted jobs are finalized once the current
  // lease age exceeds this cap (zombie termination via control plane).
  MAX_JOB_AGE_MS?: string;

  // Signed Download Config (Phase 6)
  DOWNLOAD_SIGNING_SECRET?: string;
  DOWNLOAD_URL_TTL_SECONDS?: string; // default 86400, bounded 60..604800

  // Payment Recipient & VietQR Configuration (non-secret typed config)
  BANK_ID?: string; // e.g. 'MB', '970422'
  BANK_ACCOUNT_NUMBER?: string; // e.g. '0000123456789'
  BANK_ACCOUNT_NAME?: string; // e.g. 'TELEFONT STORE'
  VIETQR_TEMPLATE?: string; // e.g. 'compact2', 'qr_only'
  PAYMENT_CODE_PREFIX?: string; // e.g. 'TF' (default: 'TF')
}

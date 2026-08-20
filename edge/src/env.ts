export interface Env {
  // Bindings
  DB: D1Database;
  FULFILLMENT_QUEUE: Queue<unknown>;
  ARTIFACTS_BUCKET: R2Bucket;

  // Secrets & Config (injected via Cloudflare secrets / .dev.vars, not committed)
  ENVIRONMENT?: string;
  TELEGRAM_BOT_TOKEN?: string;
  TELEGRAM_WEBHOOK_SECRET?: string;
  SEPAY_API_TOKEN?: string;
  SEPAY_WEBHOOK_SECRET?: string;
}

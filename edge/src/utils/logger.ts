/**
 * Structured, sanitized logging utility for Cloudflare Worker Control Plane.
 *
 * Ensures all emitted JSON events are stripped of sensitive tokens, passwords,
 * authorization bearer headers, bank account numbers, and cryptographic keys.
 */

export interface LogEventBase {
  event: string;
  timestamp?: string;
  [key: string]: unknown;
}

export interface PaymentAcceptedLogEvent extends LogEventBase {
  event: 'payment_accepted';
  order_id: string;
  user_id: string;
  amount: number;
  currency: string;
  payment_code: string;
  provider: string;
}

export interface OutboxDispatchedLogEvent extends LogEventBase {
  event: 'outbox_dispatched';
  event_id: string;
  event_type: string;
  aggregate_id: string;
  attempt: number;
  status: string;
}

export interface JobClaimedLogEvent extends LogEventBase {
  event: 'job_claimed';
  job_id: string;
  worker_id: string;
  lease_duration_sec: number;
  attempt_count: number;
}

export interface JobHeartbeatLogEvent extends LogEventBase {
  event: 'job_heartbeat';
  job_id: string;
  worker_id: string;
  lease_expires_at: number;
}

export interface JobCompletedLogEvent extends LogEventBase {
  event: 'job_completed';
  job_id: string;
  order_id: string;
  artifact_key: string;
  size_bytes: number;
}

export interface TelegramDeliveredLogEvent extends LogEventBase {
  event: 'telegram_delivered';
  order_id: string;
  chat_id: number;
  event_id: string;
}

export interface DownloadServedLogEvent extends LogEventBase {
  event: 'download_served';
  order_id: string;
  size_bytes: number;
}

export type StructuredLogEvent =
  | PaymentAcceptedLogEvent
  | OutboxDispatchedLogEvent
  | JobClaimedLogEvent
  | JobHeartbeatLogEvent
  | JobCompletedLogEvent
  | TelegramDeliveredLogEvent
  | DownloadServedLogEvent;

const SENSITIVE_SUBSTRINGS = [
  'authorization',
  'token',
  'secret',
  'signature',
  'key',
  'password',
  'account_number',
  'accountnumber',
  'lease_token',
];

export function sanitizeLogPayload<T extends Record<string, unknown>>(payload: T): Record<string, unknown> {
  const clean: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(payload)) {
    const lowerKey = k.toLowerCase();
    const isSensitive = SENSITIVE_SUBSTRINGS.some((s) => lowerKey.includes(s));
    if (isSensitive) {
      clean[k] = '[REDACTED]';
    } else if (v && typeof v === 'object' && !Array.isArray(v)) {
      clean[k] = sanitizeLogPayload(v as Record<string, unknown>);
    } else {
      clean[k] = v;
    }
  }
  return clean;
}

export function emitStructuredLog(event: StructuredLogEvent): void {
  const timestamp = new Date().toISOString();
  const sanitized = sanitizeLogPayload({ ...event, timestamp });
  console.log(JSON.stringify(sanitized));
}

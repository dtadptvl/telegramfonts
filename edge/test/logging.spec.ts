import { describe, it, expect, vi } from 'vitest';
import { sanitizeLogPayload, emitStructuredLog, type StructuredLogEvent } from '../src/utils/logger';

describe('Phase 7: Structured Logging & Redaction', () => {
  it('redacts sensitive keys, tokens, signatures, passwords, and bank accounts', () => {
    const raw = {
      event: 'test_event',
      order_id: 'ord_123',
      user_id: 'usr_456',
      authorization: 'Bearer secret_token_value',
      secret_token: 'top_secret_123',
      signature: 'sha256_sig_value',
      password: 'mypassword',
      bank_account_number: '1234567890123',
      lease_token: '12345678-1234-1234-1234-123456789abc',
      nested: {
        api_key: 'key_value',
        webhook_secret: 'wh_secret',
        safe_field: 'safe_value',
      },
    };

    const sanitized = sanitizeLogPayload(raw);

    expect(sanitized.event).toBe('test_event');
    expect(sanitized.order_id).toBe('ord_123');
    expect(sanitized.user_id).toBe('usr_456');

    expect(sanitized.authorization).toBe('[REDACTED]');
    expect(sanitized.secret_token).toBe('[REDACTED]');
    expect(sanitized.signature).toBe('[REDACTED]');
    expect(sanitized.password).toBe('[REDACTED]');
    expect(sanitized.bank_account_number).toBe('[REDACTED]');
    expect(sanitized.lease_token).toBe('[REDACTED]');

    const nested = sanitized.nested as Record<string, unknown>;
    expect(nested.safe_field).toBe('safe_value');
    expect(nested.api_key).toBe('[REDACTED]');
    expect(nested.webhook_secret).toBe('[REDACTED]');
  });

  it('emitStructuredLog outputs valid JSON string to console without secrets', () => {
    const consoleSpy = vi.spyOn(console, 'log').mockImplementation(() => {});

    try {
      const event: StructuredLogEvent = {
        event: 'payment_accepted',
        order_id: 'ord_test_01',
        user_id: 'usr_test_01',
        amount: 100000,
        currency: 'VND',
        payment_code: 'TP123456',
        provider: 'SEPAY',
      };

      emitStructuredLog(event);

      expect(consoleSpy).toHaveBeenCalledTimes(1);
      const loggedJson = consoleSpy.mock.calls[0][0];
      const parsed = JSON.parse(loggedJson) as Record<string, unknown>;

      expect(parsed.event).toBe('payment_accepted');
      expect(parsed.order_id).toBe('ord_test_01');
      expect(parsed.amount).toBe(100000);
      expect(parsed.timestamp).toBeDefined();
    } finally {
      consoleSpy.mockRestore();
    }
  });
});

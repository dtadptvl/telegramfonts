import { describe, it, expect } from 'vitest';
import { escapeHtml } from '../src/utils/html';

describe('HTML Escaping', () => {
  it('escapes special characters correctly for Telegram HTML mode', () => {
    expect(escapeHtml('Hello & World')).toBe('Hello &amp; World');
    expect(escapeHtml('<script>alert("xss")</script>')).toBe(
      '&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;'
    );
    expect(escapeHtml('Font "Bold" > 12 & < 20')).toBe(
      'Font &quot;Bold&quot; &gt; 12 &amp; &lt; 20'
    );
  });

  it('handles empty and nullish inputs gracefully', () => {
    expect(escapeHtml('')).toBe('');
    expect(escapeHtml(null)).toBe('');
    expect(escapeHtml(undefined)).toBe('');
  });
});

import { describe, it, expect } from 'vitest';
import { normalizeMyFontsUrl } from '../src/utils/myfonts';

describe('MyFonts URL Normalization & Validation', () => {
  it('accepts and normalizes valid collection URLs', () => {
    const res = normalizeMyFontsUrl(
      'https://www.myfonts.com/collections/helvetica-now-font-monotype-imaging'
    );
    expect(res.isValid).toBe(true);
    expect(res.canonicalUrl).toBe(
      'https://www.myfonts.com/collections/helvetica-now-font-monotype-imaging'
    );
    expect(res.canonicalKey).toBe(
      'myfonts:collections/helvetica-now-font-monotype-imaging'
    );
  });

  it('strips query parameters, hashes, and trailing slashes', () => {
    const res = normalizeMyFontsUrl(
      'https://myfonts.com/collections/helvetica-now-font-monotype-imaging/?utm_source=test&foo=bar#styles'
    );
    expect(res.isValid).toBe(true);
    expect(res.canonicalUrl).toBe(
      'https://www.myfonts.com/collections/helvetica-now-font-monotype-imaging'
    );
    expect(res.canonicalKey).toBe(
      'myfonts:collections/helvetica-now-font-monotype-imaging'
    );
  });

  it('accepts product and font family path prefixes', () => {
    const resFont = normalizeMyFontsUrl('https://www.myfonts.com/fonts/linotype/helvetica');
    expect(resFont.isValid).toBe(true);
    expect(resFont.canonicalKey).toBe('myfonts:fonts/linotype/helvetica');

    const resProd = normalizeMyFontsUrl(
      'https://www.myfonts.com/products/helvetica-now-display-extra-bold-757088'
    );
    expect(resProd.isValid).toBe(true);
    expect(resProd.canonicalKey).toBe(
      'myfonts:products/helvetica-now-display-extra-bold-757088'
    );
  });

  it('rejects non-https URLs', () => {
    const res = normalizeMyFontsUrl(
      'http://www.myfonts.com/collections/helvetica-now-font-monotype-imaging'
    );
    expect(res.isValid).toBe(false);
    expect(res.reason).toContain('https');
  });

  it('rejects foreign domains', () => {
    const resGoogle = normalizeMyFontsUrl('https://www.google.com/fonts');
    expect(resGoogle.isValid).toBe(false);
    expect(resGoogle.reason).toContain('myfonts.com');

    const resDafont = normalizeMyFontsUrl('https://www.dafont.com/roboto');
    expect(resDafont.isValid).toBe(false);
  });

  it('rejects non-font paths on myfonts.com', () => {
    const resRoot = normalizeMyFontsUrl('https://www.myfonts.com/');
    expect(resRoot.isValid).toBe(false);

    const resCart = normalizeMyFontsUrl('https://www.myfonts.com/cart');
    expect(resCart.isValid).toBe(false);

    const resAbout = normalizeMyFontsUrl('https://www.myfonts.com/about');
    expect(resAbout.isValid).toBe(false);
  });

  it('rejects malformed URLs and empty inputs', () => {
    expect(normalizeMyFontsUrl('').isValid).toBe(false);
    expect(normalizeMyFontsUrl('not-a-url').isValid).toBe(false);
    expect(normalizeMyFontsUrl('ftp://myfonts.com').isValid).toBe(false);
  });
});

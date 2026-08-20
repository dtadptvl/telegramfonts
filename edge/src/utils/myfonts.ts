export interface NormalizedMyFontsUrl {
  isValid: boolean;
  canonicalUrl?: string;
  canonicalKey?: string;
  reason?: string;
}

const ALLOWED_HOSTS = new Set(['myfonts.com', 'www.myfonts.com']);
const ALLOWED_PATH_PREFIXES = ['/collections/', '/fonts/', '/products/'];

/**
 * Validates and normalizes a MyFonts URL.
 * Rejects non-https URLs, third-party domains, and non-product pages.
 */
export function normalizeMyFontsUrl(rawInput: string): NormalizedMyFontsUrl {
  if (!rawInput || typeof rawInput !== 'string') {
    return { isValid: false, reason: 'Empty or invalid URL input' };
  }

  const trimmed = rawInput.trim();

  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return { isValid: false, reason: 'Malformed URL' };
  }

  if (parsed.protocol !== 'https:') {
    return { isValid: false, reason: 'Only https protocol is allowed' };
  }

  const hostname = parsed.hostname.toLowerCase();
  if (!ALLOWED_HOSTS.has(hostname)) {
    return { isValid: false, reason: 'Host is not myfonts.com' };
  }

  // Clean pathname: remove duplicate slashes and trailing slash
  let pathname = parsed.pathname.toLowerCase().replace(/\/+/g, '/');
  if (pathname.endsWith('/') && pathname.length > 1) {
    pathname = pathname.slice(0, -1);
  }

  // Validate path starts with one of the allowed prefixes and has a slug
  const matchedPrefix = ALLOWED_PATH_PREFIXES.find((prefix) => pathname.startsWith(prefix));
  if (!matchedPrefix) {
    return {
      isValid: false,
      reason: 'URL must point to a font collection, font family, or product page',
    };
  }

  const slug = pathname.slice(matchedPrefix.length).trim();
  if (!slug) {
    return {
      isValid: false,
      reason: 'URL lacks a specific font family or product identifier',
    };
  }

  // Canonical normalized representation (deterministic for deduplication & caching)
  const canonicalUrl = `https://www.myfonts.com${pathname}`;
  const canonicalKey = `myfonts:${pathname.replace(/^\//, '')}`;

  return {
    isValid: true,
    canonicalUrl,
    canonicalKey,
  };
}

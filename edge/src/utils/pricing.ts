/**
 * Pricing rules for the ORIGINAL/VIETNAMESE mode contract (T-PRICE-01).
 * ORIGINAL: 5,000 VND/style (catalog default, preserves any explicit overrides).
 * VIETNAMESE: 8,000 VND/style (never falls back to 5,000; no silent undercharge).
 */
import type { FontMode } from '../types/session';

export const MODE_PRICE_PER_STYLE_VND: Record<FontMode, number> = {
  ORIGINAL: 5000,
  VIETNAMESE: 8000,
};

/**
 * Computes the unit price for one style under the selected mode.
 * @param stylePrice - The catalog-defined price for the style (may be undefined/null).
 * @param mode - The selected font mode.
 * @returns The exact unit price in VND.
 */
export function stylePriceForMode(stylePrice: number | undefined | null, mode: FontMode): number {
  const modePrice = MODE_PRICE_PER_STYLE_VND[mode];
  if (mode === 'VIETNAMESE') {
    // VIETNAMESE must never fall through to the catalog/legacy 5000 default.
    // Preserve explicit overrides only when they exceed the VIETNAMESE rate.
    return Math.max(modePrice, stylePrice ?? modePrice);
  }
  // ORIGINAL: catalog price (default 5000) is honored.
  return stylePrice ?? modePrice;
}
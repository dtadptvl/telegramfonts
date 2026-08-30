export type SessionStatus =
  | 'IDLE'
  | 'AWAITING_CATALOG'
  | 'SELECTING_STYLES'
  | 'SELECTING_FORMATS'
  | 'CONFIRMING'
  | 'ORDER_CREATED';

export type FontFormat = 'TTF' | 'OTF';

export const SUPPORTED_FORMATS: FontFormat[] = ['TTF', 'OTF'];

/**
 * ORIGINAL vs VIETNAMESE font mode product contract (T-PRICE-01).
 * Mode is part of durable order identity, binding compute paths and reuse tiers.
 */
export type FontMode = 'ORIGINAL' | 'VIETNAMESE';

export const SUPPORTED_MODES: FontMode[] = ['ORIGINAL', 'VIETNAMESE'];

/**
 * Validates whether a string is a valid FontMode.
 */
export function isFontMode(value: unknown): value is FontMode {
  return typeof value === 'string' && SUPPORTED_MODES.includes(value as FontMode);
}

export interface TelegramSessionRecord {
  id: string;
  user_id: string;
  chat_id: string;
  workflow_token: string;
  checkout_token: string;
  version: number;
  catalog_id: string | null;
  selected_styles: string; // JSON string of string[]
  selected_formats: string; // JSON string of FontFormat[]
  last_message_id: number | null;
  status: SessionStatus;
  active_order_id: string | null;
  created_at: number;
  updated_at: number;
  /**
   * Durable mode selection for the current /muahang flow.
   * ABSENT (NULL) = legacy session => fail-closed at every executable route.
   */
  mode: FontMode | null;
  /**
   * Temporary storage for source URL when it arrives before mode selection.
   * Consumed atomically with mode selection (set to NULL after selection).
   */
  pending_source_url: string | null;
}
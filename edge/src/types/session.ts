export type SessionStatus =
  | 'IDLE'
  | 'AWAITING_CATALOG'
  | 'SELECTING_STYLES'
  | 'SELECTING_FORMATS'
  | 'CONFIRMING'
  | 'ORDER_CREATED';

export type FontFormat = 'TTF' | 'OTF' | 'WOFF2';

export const SUPPORTED_FORMATS: FontFormat[] = ['TTF', 'OTF', 'WOFF2'];

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
}

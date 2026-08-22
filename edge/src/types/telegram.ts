export interface TelegramUser {
  id: number;
  is_bot: boolean;
  first_name: string;
  last_name?: string;
  username?: string;
}

export interface TelegramChat {
  id: number;
  type: 'private' | 'group' | 'supergroup' | 'channel';
  first_name?: string;
  last_name?: string;
  username?: string;
}

export interface TelegramMessage {
  message_id: number;
  from?: TelegramUser;
  chat: TelegramChat;
  date: number;
  text?: string;
}

export interface TelegramCallbackQuery {
  id: string;
  from: TelegramUser;
  message?: TelegramMessage;
  inline_message_id?: string;
  chat_instance?: string;
  data?: string;
}

export interface TelegramUpdate {
  update_id: number;
  message?: TelegramMessage;
  edited_message?: TelegramMessage;
  callback_query?: TelegramCallbackQuery;
}

export interface InlineKeyboardButton {
  text: string;
  callback_data?: string;
  url?: string;
}

export interface InlineKeyboardMarkup {
  inline_keyboard: InlineKeyboardButton[][];
}

export interface SendMessageParams {
  chat_id: number | string;
  text: string;
  parse_mode?: 'HTML';
  reply_markup?: InlineKeyboardMarkup;
}

export interface EditMessageTextParams {
  chat_id?: number | string;
  message_id?: number;
  inline_message_id?: string;
  text: string;
  parse_mode?: 'HTML';
  reply_markup?: InlineKeyboardMarkup;
}

export interface EditMessageReplyMarkupParams {
  chat_id?: number | string;
  message_id?: number;
  inline_message_id?: string;
  reply_markup: InlineKeyboardMarkup;
}

export interface BotCommand {
  command: string;
  description: string;
}

export interface SetMyCommandsParams {
  commands: BotCommand[];
}

export interface SetChatMenuButtonParams {
  chat_id?: number | string;
  menu_button: {
    type: 'commands';
  };
}

export interface AnswerCallbackQueryParams {
  callback_query_id: string;
  text?: string;
  show_alert?: boolean;
  url?: string;
  cache_time?: number;
}

export interface SendDocumentParams {
  chat_id: number | string;
  document: Blob | Uint8Array;
  filename: string;
  caption?: string;
  parse_mode?: 'HTML';
}


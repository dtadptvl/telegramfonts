import type { Env } from '../env';
import type {
  TelegramUpdate,
  TelegramMessage,
  TelegramCallbackQuery,
  InlineKeyboardMarkup,
} from '../types/telegram';
import type { FontCatalog, Style } from '../types/catalog';
import type { TelegramSessionRecord, FontFormat } from '../types/session';
import { SUPPORTED_FORMATS } from '../types/session';
import { escapeHtml } from '../utils/html';
import { normalizeMyFontsUrl } from '../utils/myfonts';
import { TelegramClient } from '../services/telegram-client';
import { CatalogService } from '../services/catalog-service';
import { SessionService } from '../services/session-service';
import { OrderService } from '../services/order-service';

export async function handleTelegramWebhook(
  request: Request,
  env: Env,
  _ctx: ExecutionContext
): Promise<Response> {
  // 1. Secret & Token validation
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_WEBHOOK_SECRET) {
    return new Response(JSON.stringify({ error: 'Telegram configuration is missing' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const secretHeader = request.headers.get('X-Telegram-Bot-Api-Secret-Token');
  if (secretHeader !== env.TELEGRAM_WEBHOOK_SECRET) {
    return new Response(JSON.stringify({ error: 'Unauthorized' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // 2. Parse Telegram Update
  let update: TelegramUpdate;
  try {
    update = (await request.json()) as TelegramUpdate;
  } catch {
    return new Response(JSON.stringify({ status: 'ignored_invalid_payload' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const tg = new TelegramClient(env.TELEGRAM_BOT_TOKEN);
  const catalogService = new CatalogService(env.DB);
  const sessionService = new SessionService(env.DB);
  const orderService = new OrderService(env.DB);

  try {
    if (update.message) {
      await handleMessage(update.message, tg, sessionService, catalogService);
    } else if (update.callback_query) {
      await handleCallbackQuery(
        update.callback_query,
        tg,
        sessionService,
        catalogService,
        orderService
      );
    }
  } catch {
    // Fail safely without logging secrets or user messages
  }

  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

async function handleMessage(
  message: TelegramMessage,
  tg: TelegramClient,
  sessionService: SessionService,
  catalogService: CatalogService
): Promise<void> {
  if (!message.from || !message.text) return;

  const userId = String(message.from.id);
  const chatId = String(message.chat.id);
  const text = message.text.trim();

  // Upsert user and session
  await sessionService.upsertTelegramUser(message.from);
  await sessionService.getOrCreateSession(userId, chatId);

  if (text === '/start') {
    await sessionService.resetSession(userId, chatId);
    await tg.sendMessage({
      chat_id: chatId,
      text: `<b>Welcome to TeleFont!</b> 🎨\n\nSend me a link to any font family on <b>MyFonts.com</b> to start.\n\n<i>Example:</i> <code>https://www.myfonts.com/collections/helvetica-now-font-monotype-imaging</code>`,
    });
    return;
  }

  // Check for MyFonts URL
  const normalized = normalizeMyFontsUrl(text);
  if (!normalized.isValid || !normalized.canonicalUrl || !normalized.canonicalKey) {
    await tg.sendMessage({
      chat_id: chatId,
      text: `⚠️ <b>Invalid MyFonts Link</b>\n\n${escapeHtml(
        normalized.reason || 'Please provide a valid https MyFonts URL.'
      )}\n\n<i>Example:</i> <code>https://www.myfonts.com/collections/helvetica-now-font-monotype-imaging</code>`,
    });
    return;
  }

  // Create or deduplicate catalog request
  await catalogService.getOrCreateCatalogRequest(
    userId,
    normalized.canonicalUrl,
    normalized.canonicalKey
  );

  // Check if catalog data is already available
  const catalog = await catalogService.getCatalogByCanonicalKey(normalized.canonicalKey);

  if (!catalog) {
    // Catalog pending (future A23 agent will satisfy this)
    await sessionService.setStatus(userId, 'AWAITING_CATALOG');
    await tg.sendMessage({
      chat_id: chatId,
      text: `🔍 <b>Analyzing font catalog...</b>\n\nWe are analyzing:\n<code>${escapeHtml(
        normalized.canonicalUrl
      )}</code>\n\nPlease wait a moment.`,
    });
    return;
  }

  // Catalog is ready! Persist to session and render style selection
  const catalogId = (
    await catalogService.getOrCreateCatalogRequest(
      userId,
      normalized.canonicalUrl,
      normalized.canonicalKey
    )
  ).catalog_id;

  if (catalogId) {
    await sessionService.updateSessionCatalog(userId, catalogId, 'SELECTING_STYLES');
  }

  const session = await sessionService.getSessionByUserId(userId);
  if (session) {
    const { text: msgText, replyMarkup } = renderStyleSelection(catalog, []);
    const sent = await tg.sendMessage({
      chat_id: chatId,
      text: msgText,
      reply_markup: replyMarkup,
    });

    if (sent.ok) {
      const data = (await sent.json()) as { result?: { message_id?: number } };
      if (data.result?.message_id) {
        await sessionService.setStatus(userId, 'SELECTING_STYLES', data.result.message_id);
      }
    }
  }
}

async function handleCallbackQuery(
  query: TelegramCallbackQuery,
  tg: TelegramClient,
  sessionService: SessionService,
  catalogService: CatalogService,
  orderService: OrderService
): Promise<void> {
  const userId = String(query.from.id);
  const data = query.data || '';

  const session = await sessionService.getSessionByUserId(userId);
  if (!session || !session.catalog_id) {
    await tg.answerCallbackQuery({
      callback_query_id: query.id,
      text: 'Session expired or not found. Please send a font link again.',
      show_alert: true,
    });
    return;
  }

  const catalog = await catalogService.getCatalogById(session.catalog_id);
  if (!catalog) {
    await tg.answerCallbackQuery({
      callback_query_id: query.id,
      text: 'Catalog not found. Please send a font link again.',
      show_alert: true,
    });
    return;
  }

  let selectedStyles: string[] = [];
  let selectedFormats: FontFormat[] = [];
  try {
    selectedStyles = JSON.parse(session.selected_styles);
    selectedFormats = JSON.parse(session.selected_formats);
  } catch {
    selectedStyles = [];
    selectedFormats = ['TTF'];
  }

  // Action: Toggle single style -> st:t:<style_id>
  if (data.startsWith('st:t:')) {
    const styleId = data.slice(5);
    const updated = await sessionService.toggleStyleSelection(userId, styleId);
    const { text, replyMarkup } = renderStyleSelection(catalog, updated);

    await tg.editMessageText({
      chat_id: session.chat_id,
      message_id: query.message?.message_id || session.last_message_id || undefined,
      text,
      reply_markup: replyMarkup,
    });
    await tg.answerCallbackQuery({ callback_query_id: query.id });
    return;
  }

  // Action: Select all styles -> st:all
  if (data === 'st:all') {
    const allIds = catalog.styles.map((s) => s.id);
    await sessionService.setAllStyles(userId, allIds);
    const { text, replyMarkup } = renderStyleSelection(catalog, allIds);

    await tg.editMessageText({
      chat_id: session.chat_id,
      message_id: query.message?.message_id || session.last_message_id || undefined,
      text,
      reply_markup: replyMarkup,
    });
    await tg.answerCallbackQuery({ callback_query_id: query.id, text: 'All styles selected' });
    return;
  }

  // Action: Clear styles -> st:clear
  if (data === 'st:clear') {
    await sessionService.clearStyles(userId);
    const { text, replyMarkup } = renderStyleSelection(catalog, []);

    await tg.editMessageText({
      chat_id: session.chat_id,
      message_id: query.message?.message_id || session.last_message_id || undefined,
      text,
      reply_markup: replyMarkup,
    });
    await tg.answerCallbackQuery({ callback_query_id: query.id, text: 'Selection cleared' });
    return;
  }

  // Action: Next to formats -> st:next
  if (data === 'st:next') {
    if (!selectedStyles.length) {
      await tg.answerCallbackQuery({
        callback_query_id: query.id,
        text: 'Please select at least 1 style to continue.',
        show_alert: true,
      });
      return;
    }

    await sessionService.setStatus(userId, 'SELECTING_FORMATS');
    const { text, replyMarkup } = renderFormatSelection(
      catalog,
      selectedStyles.length,
      selectedFormats
    );

    await tg.editMessageText({
      chat_id: session.chat_id,
      message_id: query.message?.message_id || session.last_message_id || undefined,
      text,
      reply_markup: replyMarkup,
    });
    await tg.answerCallbackQuery({ callback_query_id: query.id });
    return;
  }

  // Action: Back to styles -> fmt:back
  if (data === 'fmt:back') {
    await sessionService.setStatus(userId, 'SELECTING_STYLES');
    const { text, replyMarkup } = renderStyleSelection(catalog, selectedStyles);

    await tg.editMessageText({
      chat_id: session.chat_id,
      message_id: query.message?.message_id || session.last_message_id || undefined,
      text,
      reply_markup: replyMarkup,
    });
    await tg.answerCallbackQuery({ callback_query_id: query.id });
    return;
  }

  // Action: Toggle format -> fmt:t:<format>
  if (data.startsWith('fmt:t:')) {
    const format = data.slice(6) as FontFormat;
    if (SUPPORTED_FORMATS.includes(format)) {
      const updatedFormats = await sessionService.toggleFormatSelection(userId, format);
      const { text, replyMarkup } = renderFormatSelection(
        catalog,
        selectedStyles.length,
        updatedFormats
      );

      await tg.editMessageText({
        chat_id: session.chat_id,
        message_id: query.message?.message_id || session.last_message_id || undefined,
        text,
        reply_markup: replyMarkup,
      });
      await tg.answerCallbackQuery({ callback_query_id: query.id });
      return;
    }
  }

  // Action: Next to confirmation -> fmt:next
  if (data === 'fmt:next') {
    if (!selectedFormats.length) {
      await tg.answerCallbackQuery({
        callback_query_id: query.id,
        text: 'Please select at least 1 font format.',
        show_alert: true,
      });
      return;
    }

    await sessionService.setStatus(userId, 'CONFIRMING');
    const { text, replyMarkup } = renderOrderConfirmation(
      catalog,
      selectedStyles,
      selectedFormats
    );

    await tg.editMessageText({
      chat_id: session.chat_id,
      message_id: query.message?.message_id || session.last_message_id || undefined,
      text,
      reply_markup: replyMarkup,
    });
    await tg.answerCallbackQuery({ callback_query_id: query.id });
    return;
  }

  // Action: Cancel -> ord:cancel
  if (data === 'ord:cancel') {
    await sessionService.resetSession(userId, session.chat_id);
    await tg.editMessageText({
      chat_id: session.chat_id,
      message_id: query.message?.message_id || session.last_message_id || undefined,
      text: '❌ <b>Order cancelled.</b>\n\nSend a new MyFonts link whenever you are ready.',
    });
    await tg.answerCallbackQuery({ callback_query_id: query.id, text: 'Order cancelled' });
    return;
  }

  // Action: Confirm Order -> ord:confirm
  if (data === 'ord:confirm') {
    try {
      const result = await orderService.createOrderFromSession(session, catalog);

      const messageText = `🎉 <b>Order Created!</b>\n\n• <b>Order ID:</b> <code>${result.orderId}</code>\n• <b>Status:</b> <code>AWAITING_PAYMENT</code>\n• <b>Amount Due:</b> <b>${result.totalAmount.toLocaleString('vi-VN')} VND</b>\n• <b>Styles Count:</b> ${result.itemsCount}\n\n⏳ <i>Payment instructions and QR code will be provided in the next phase.</i>`;

      await tg.editMessageText({
        chat_id: session.chat_id,
        message_id: query.message?.message_id || session.last_message_id || undefined,
        text: messageText,
      });

      await tg.answerCallbackQuery({
        callback_query_id: query.id,
        text: result.isExisting ? 'Order already placed!' : 'Order created successfully!',
      });
      return;
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to create order';
      await tg.answerCallbackQuery({
        callback_query_id: query.id,
        text: `Error: ${message}`,
        show_alert: true,
      });
      return;
    }
  }

  await tg.answerCallbackQuery({ callback_query_id: query.id });
}

function renderStyleSelection(
  catalog: FontCatalog,
  selectedStyleIds: string[]
): { text: string; replyMarkup: InlineKeyboardMarkup } {
  const text = `📦 <b>${escapeHtml(catalog.familyName)}</b>\n${
    catalog.foundry ? `<i>Foundry: ${escapeHtml(catalog.foundry)}</i>\n` : ''
  }\nSelected styles: <b>${selectedStyleIds.length} / ${
    catalog.styles.length
  }</b>\n\nTap styles below to select or deselect:`;

  const keyboard: InlineKeyboardMarkup['inline_keyboard'] = [];

  for (const style of catalog.styles) {
    const isSelected = selectedStyleIds.includes(style.id);
    const label = `${isSelected ? '✅' : '⬜'} ${style.displayName}`;
    keyboard.push([
      {
        text: label,
        callback_data: `st:t:${style.id}`,
      },
    ]);
  }

  keyboard.push([
    { text: 'Select All', callback_data: 'st:all' },
    { text: 'Clear', callback_data: 'st:clear' },
  ]);

  keyboard.push([
    {
      text: `Next: Select Formats (${selectedStyleIds.length}) ➡️`,
      callback_data: 'st:next',
    },
  ]);

  return { text, replyMarkup: { inline_keyboard: keyboard } };
}

function renderFormatSelection(
  catalog: FontCatalog,
  stylesCount: number,
  selectedFormats: FontFormat[]
): { text: string; replyMarkup: InlineKeyboardMarkup } {
  const text = `📦 <b>${escapeHtml(
    catalog.familyName
  )}</b>\n\nSelected Styles: <b>${stylesCount}</b>\n\nChoose font formats to include:`;

  const formatButtons = SUPPORTED_FORMATS.map((fmt) => {
    const isSelected = selectedFormats.includes(fmt);
    return {
      text: `${isSelected ? '✅' : '⬜'} ${fmt}`,
      callback_data: `fmt:t:${fmt}`,
    };
  });

  const keyboard: InlineKeyboardMarkup['inline_keyboard'] = [
    formatButtons,
    [
      { text: '⬅️ Back to Styles', callback_data: 'fmt:back' },
      { text: 'Review Order ➡️', callback_data: 'fmt:next' },
    ],
  ];

  return { text, replyMarkup: { inline_keyboard: keyboard } };
}

function renderOrderConfirmation(
  catalog: FontCatalog,
  selectedStyleIds: string[],
  selectedFormats: FontFormat[]
): { text: string; replyMarkup: InlineKeyboardMarkup } {
  const validStylesMap = new Map<string, Style>();
  for (const s of catalog.styles) validStylesMap.set(s.id, s);

  const selectedStyles: Style[] = [];
  for (const id of selectedStyleIds) {
    const s = validStylesMap.get(id);
    if (s) selectedStyles.push(s);
  }

  const totalAmount = selectedStyles.reduce(
    (sum, s) => sum + (s.price !== undefined ? s.price : 50000),
    0
  );

  const stylesListText = selectedStyles
    .map((s) => `  • ${escapeHtml(s.displayName)}`)
    .slice(0, 15)
    .join('\n');

  const extraStylesCount = selectedStyles.length > 15 ? selectedStyles.length - 15 : 0;
  const extraText = extraStylesCount > 0 ? `\n  <i>...and ${extraStylesCount} more styles</i>` : '';

  const text = `📋 <b>Order Confirmation</b>\n\n• <b>Font Family:</b> ${escapeHtml(
    catalog.familyName
  )}\n${
    catalog.foundry ? `• <b>Foundry:</b> ${escapeHtml(catalog.foundry)}\n` : ''
  }• <b>Styles (${selectedStyles.length}):</b>\n${stylesListText}${extraText}\n• <b>Formats:</b> ${selectedFormats.join(
    ', '
  )}\n• <b>Total Amount:</b> <b>${totalAmount.toLocaleString('vi-VN')} VND</b>\n\nConfirm to create your order:`;

  const keyboard: InlineKeyboardMarkup['inline_keyboard'] = [
    [
      { text: '❌ Cancel', callback_data: 'ord:cancel' },
      { text: '💳 Confirm Order', callback_data: 'ord:confirm' },
    ],
  ];

  return { text, replyMarkup: { inline_keyboard: keyboard } };
}

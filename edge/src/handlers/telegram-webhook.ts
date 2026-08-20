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
  // 1. Secret & Token validation (fail-closed)
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

  if (!update || typeof update.update_id !== 'number') {
    return new Response(JSON.stringify({ status: 'ignored_empty_update' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // 3. Durable Telegram update_id deduplication (BLOCK 3)
  const userId = update.message?.from
    ? String(update.message.from.id)
    : update.callback_query?.from
    ? String(update.callback_query.from.id)
    : null;

  try {
    await env.DB.prepare(
      `INSERT INTO telegram_updates (update_id, user_id, created_at) VALUES (?, ?, ?)`
    )
      .bind(update.update_id, userId, Date.now())
      .run();
  } catch (err: unknown) {
    // If update_id was already recorded, ignore safely without re-processing (idempotent webhook acknowledgement)
    const isConstraint =
      err instanceof Error &&
      (err.message.includes('UNIQUE constraint failed') || err.message.includes('PRIMARY KEY'));

    if (isConstraint) {
      return new Response(JSON.stringify({ status: 'ignored_duplicate_update' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    // For other DB connectivity errors, return 500 so Telegram retries
    return new Response(JSON.stringify({ error: 'Internal Server Error' }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const tg = new TelegramClient(env.TELEGRAM_BOT_TOKEN);
  const catalogService = new CatalogService(env.DB);
  const sessionService = new SessionService(env.DB);
  const orderService = new OrderService(env.DB);

  // 4. Process update (do not swallow transient DB/network errors)
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
  } catch (err: unknown) {
    // Transient processing failure -> return 500 to signal Telegram to retry
    return new Response(JSON.stringify({ error: 'Internal Processing Error' }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    });
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

  // Create or deduplicate catalog request (atomic conflict-safe)
  const reqRecord = await catalogService.getOrCreateCatalogRequest(
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

  // Catalog is ready! Persist to session with fresh workflow_token and render style selection
  const catalogId = reqRecord.catalog_id || (await catalogService.persistCatalogResult(catalog));
  await sessionService.updateSessionCatalog(userId, catalogId, 'SELECTING_STYLES');

  const session = await sessionService.getSessionByUserId(userId);
  if (session) {
    const { text: msgText, replyMarkup } = renderStyleSelection(
      catalog,
      [],
      session.workflow_token
    );
    const sent = await tg.sendMessage({
      chat_id: chatId,
      text: msgText,
      reply_markup: replyMarkup,
    });

    if (sent.message_id) {
      await sessionService.setStatus(userId, 'SELECTING_STYLES', sent.message_id);
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

  // 1. Session & Catalog verification
  const session = await sessionService.getSessionByUserId(userId);
  if (!session || !session.catalog_id) {
    await tg.answerCallbackQuery({
      callback_query_id: query.id,
      text: 'Session expired. Please send a font link again.',
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

  // 2. Parse callback action and token (BLOCK 2)
  // Format: <prefix>:<action>:<token>[:<param>]
  const parts = data.split(':');
  const [prefix, action, token, param] = parts;

  // Verify workflow token match (stale button protection)
  if (!token || token !== session.workflow_token) {
    await tg.answerCallbackQuery({
      callback_query_id: query.id,
      text: 'This menu is expired. Please use the latest message.',
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

  // Handle STYLE SELECTION actions (Prefix 'st')
  if (prefix === 'st') {
    if (session.status !== 'SELECTING_STYLES') {
      await tg.answerCallbackQuery({
        callback_query_id: query.id,
        text: 'Action is no longer valid in current step.',
        show_alert: true,
      });
      return;
    }

    // Toggle style by index
    if (action === 't' && param !== undefined) {
      const styleIndex = parseInt(param, 10);
      const targetStyle = catalog.styles[styleIndex];

      if (!targetStyle) {
        await tg.answerCallbackQuery({
          callback_query_id: query.id,
          text: 'Style not found in catalog.',
          show_alert: true,
        });
        return;
      }

      const updated = await sessionService.toggleStyleSelection(userId, targetStyle.id);
      const { text, replyMarkup } = renderStyleSelection(
        catalog,
        updated,
        session.workflow_token
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

    // Select all styles
    if (action === 'all') {
      const allIds = catalog.styles.map((s) => s.id);
      await sessionService.setAllStyles(userId, allIds);
      const { text, replyMarkup } = renderStyleSelection(
        catalog,
        allIds,
        session.workflow_token
      );

      await tg.editMessageText({
        chat_id: session.chat_id,
        message_id: query.message?.message_id || session.last_message_id || undefined,
        text,
        reply_markup: replyMarkup,
      });
      await tg.answerCallbackQuery({ callback_query_id: query.id, text: 'All styles selected' });
      return;
    }

    // Clear styles
    if (action === 'clr') {
      await sessionService.clearStyles(userId);
      const { text, replyMarkup } = renderStyleSelection(
        catalog,
        [],
        session.workflow_token
      );

      await tg.editMessageText({
        chat_id: session.chat_id,
        message_id: query.message?.message_id || session.last_message_id || undefined,
        text,
        reply_markup: replyMarkup,
      });
      await tg.answerCallbackQuery({ callback_query_id: query.id, text: 'Selection cleared' });
      return;
    }

    // Next to formats
    if (action === 'nxt') {
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
        selectedFormats,
        session.workflow_token
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

  // Handle FORMAT SELECTION actions (Prefix 'fmt')
  if (prefix === 'fmt') {
    if (session.status !== 'SELECTING_FORMATS') {
      await tg.answerCallbackQuery({
        callback_query_id: query.id,
        text: 'Action is no longer valid in current step.',
        show_alert: true,
      });
      return;
    }

    // Back to styles
    if (action === 'bck') {
      await sessionService.setStatus(userId, 'SELECTING_STYLES');
      const { text, replyMarkup } = renderStyleSelection(
        catalog,
        selectedStyles,
        session.workflow_token
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

    // Toggle format
    if (action === 't' && param !== undefined) {
      const format = param as FontFormat;
      if (SUPPORTED_FORMATS.includes(format)) {
        const updatedFormats = await sessionService.toggleFormatSelection(userId, format);
        const { text, replyMarkup } = renderFormatSelection(
          catalog,
          selectedStyles.length,
          updatedFormats,
          session.workflow_token
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

    // Next to confirmation
    if (action === 'nxt') {
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
        selectedFormats,
        session.workflow_token
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

  // Handle ORDER actions (Prefix 'ord')
  if (prefix === 'ord') {
    // Cancel Order
    if (action === 'ccl') {
      if (
        session.status !== 'SELECTING_STYLES' &&
        session.status !== 'SELECTING_FORMATS' &&
        session.status !== 'CONFIRMING'
      ) {
        await tg.answerCallbackQuery({
          callback_query_id: query.id,
          text: 'Cannot cancel completed or inactive order.',
          show_alert: true,
        });
        return;
      }

      await sessionService.resetSession(userId, session.chat_id);
      await tg.editMessageText({
        chat_id: session.chat_id,
        message_id: query.message?.message_id || session.last_message_id || undefined,
        text: '❌ <b>Order cancelled.</b>\n\nSend a new MyFonts link whenever you are ready.',
      });
      await tg.answerCallbackQuery({ callback_query_id: query.id, text: 'Order cancelled' });
      return;
    }

    // Confirm Order
    if (action === 'cnf') {
      if (session.status !== 'CONFIRMING') {
        await tg.answerCallbackQuery({
          callback_query_id: query.id,
          text: 'Order confirmation is no longer valid.',
          show_alert: true,
        });
        return;
      }

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
      } catch {
        // Controlled generic message without leaking internal D1 error text (BLOCK 5)
        await tg.answerCallbackQuery({
          callback_query_id: query.id,
          text: 'An error occurred while creating your order. Please try again.',
          show_alert: true,
        });
        return;
      }
    }
  }

  await tg.answerCallbackQuery({ callback_query_id: query.id });
}

function renderStyleSelection(
  catalog: FontCatalog,
  selectedStyleIds: string[],
  workflowToken: string
): { text: string; replyMarkup: InlineKeyboardMarkup } {
  const text = `📦 <b>${escapeHtml(catalog.familyName)}</b>\n${
    catalog.foundry ? `<i>Foundry: ${escapeHtml(catalog.foundry)}</i>\n` : ''
  }\nSelected styles: <b>${selectedStyleIds.length} / ${
    catalog.styles.length
  }</b>\n\nTap styles below to select or deselect:`;

  const keyboard: InlineKeyboardMarkup['inline_keyboard'] = [];

  for (let idx = 0; idx < catalog.styles.length; idx++) {
    const style = catalog.styles[idx];
    const isSelected = selectedStyleIds.includes(style.id);
    const label = `${isSelected ? '✅' : '⬜'} ${style.displayName}`;
    keyboard.push([
      {
        text: label,
        callback_data: `st:t:${workflowToken}:${idx}`,
      },
    ]);
  }

  keyboard.push([
    { text: 'Select All', callback_data: `st:all:${workflowToken}` },
    { text: 'Clear', callback_data: `st:clr:${workflowToken}` },
  ]);

  keyboard.push([
    {
      text: `Next: Select Formats (${selectedStyleIds.length}) ➡️`,
      callback_data: `st:nxt:${workflowToken}`,
    },
  ]);

  return { text, replyMarkup: { inline_keyboard: keyboard } };
}

function renderFormatSelection(
  catalog: FontCatalog,
  stylesCount: number,
  selectedFormats: FontFormat[],
  workflowToken: string
): { text: string; replyMarkup: InlineKeyboardMarkup } {
  const text = `📦 <b>${escapeHtml(
    catalog.familyName
  )}</b>\n\nSelected Styles: <b>${stylesCount}</b>\n\nChoose font formats to include:`;

  const formatButtons = SUPPORTED_FORMATS.map((fmt) => {
    const isSelected = selectedFormats.includes(fmt);
    return {
      text: `${isSelected ? '✅' : '⬜'} ${fmt}`,
      callback_data: `fmt:t:${workflowToken}:${fmt}`,
    };
  });

  const keyboard: InlineKeyboardMarkup['inline_keyboard'] = [
    formatButtons,
    [
      { text: '⬅️ Back to Styles', callback_data: `fmt:bck:${workflowToken}` },
      { text: 'Review Order ➡️', callback_data: `fmt:nxt:${workflowToken}` },
    ],
  ];

  return { text, replyMarkup: { inline_keyboard: keyboard } };
}

function renderOrderConfirmation(
  catalog: FontCatalog,
  selectedStyleIds: string[],
  selectedFormats: FontFormat[],
  workflowToken: string
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
      { text: '❌ Cancel', callback_data: `ord:ccl:${workflowToken}` },
      { text: '💳 Confirm Order', callback_data: `ord:cnf:${workflowToken}` },
    ],
  ];

  return { text, replyMarkup: { inline_keyboard: keyboard } };
}

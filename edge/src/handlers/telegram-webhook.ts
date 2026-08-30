import type { Env } from '../env';
import type {
  TelegramUpdate,
  TelegramMessage,
  TelegramCallbackQuery,
  InlineKeyboardMarkup,
} from '../types/telegram';
import type { FontCatalog, Style } from '../types/catalog';
import type { TelegramSessionRecord, FontFormat, FontMode } from '../types/session';
import { SUPPORTED_FORMATS, isFontMode } from '../types/session';
import { escapeHtml } from '../utils/html';
import { MODE_PRICE_PER_STYLE_VND, stylePriceForMode } from '../utils/pricing';
import { normalizeMyFontsUrl } from '../utils/myfonts';
import { generateVietQrUrl } from '../utils/vietqr';
import { TelegramClient } from '../services/telegram-client';
import { CatalogService } from '../services/catalog-service';
import { SessionService, SessionConflictError } from '../services/session-service';
import { OrderService, type OrderRecord } from '../services/order-service';

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

  // 3. Durable Telegram update_id ledger check (BLOCK A)
  const existingUpdate = await env.DB.prepare(
    'SELECT status FROM telegram_updates WHERE update_id = ?'
  )
    .bind(update.update_id)
    .first<{ status: string }>();

  // If already COMPLETED, safely ignore duplicate webhook replay
  if (existingUpdate && existingUpdate.status === 'COMPLETED') {
    return new Response(JSON.stringify({ status: 'ignored_duplicate_update' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const alreadyApplied = existingUpdate?.status === 'APPLIED';
  const userId = update.message?.from
    ? String(update.message.from.id)
    : update.callback_query?.from
    ? String(update.callback_query.from.id)
    : null;

  const now = Date.now();

  // If not previously recorded, record as RECEIVED
  if (!existingUpdate) {
    await env.DB.prepare(
      `INSERT INTO telegram_updates (update_id, user_id, status, created_at, updated_at)
       VALUES (?, ?, 'RECEIVED', ?, ?)`
    )
      .bind(update.update_id, userId, now, now)
      .run();
  }

  const tg = new TelegramClient(env.TELEGRAM_BOT_TOKEN);
  const catalogService = new CatalogService(env.DB);
  const sessionService = new SessionService(env.DB);
  const orderService = new OrderService(env.DB);

  // 4. Process update
  try {
    if (update.message) {
      await handleMessage(
        update.message,
        tg,
        sessionService,
        catalogService,
        update.update_id,
        alreadyApplied
      );
    } else if (update.callback_query) {
      await handleCallbackQuery(
        update.callback_query,
        tg,
        env,
        sessionService,
        catalogService,
        orderService,
        update.update_id,
        alreadyApplied
      );
    }

    // Mark update as COMPLETED upon successful processing
    await env.DB.prepare(
      `UPDATE telegram_updates SET status = 'COMPLETED', updated_at = ? WHERE update_id = ?`
    )
      .bind(Date.now(), update.update_id)
      .run();
  } catch (err: unknown) {
    // Leave update in its current status (RECEIVED or APPLIED) so retry will reprocess safely, and return 500
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
  catalogService: CatalogService,
  updateId: number,
  alreadyApplied: boolean
): Promise<void> {
  if (!message.from || !message.text) return;

  const userId = String(message.from.id);
  const chatId = String(message.chat.id);
  const text = message.text.trim();

  // Upsert user and session
  await sessionService.upsertTelegramUser(message.from);
  await sessionService.getOrCreateSession(userId, chatId);

  if (text === '/start') {
    if (!alreadyApplied) {
      await sessionService.resetSession(userId, chatId, updateId);
    }
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

  // T-PRICE-01 mode gate (/muahang invariant order: ORIGINAL|VIETNAMESE ->
  // MyFonts URL -> catalog/styles -> ...): a valid MyFonts URL never starts
  // catalog work until an explicit ORIGINAL/VIETNAMESE selection exists.
  // Absent mode fails closed here; no ORIGINAL default, no silent inference.
  const modeSession = await sessionService.getSessionByUserId(userId);
  if (!modeSession || modeSession.mode === null) {
    if (!alreadyApplied) {
      await sessionService.setPendingSourceUrl(userId, normalized.canonicalUrl, updateId);
    }
    if (modeSession) {
      const { text: modeText, replyMarkup: modeMarkup } = renderModeSelection(
        modeSession.workflow_token
      );
      await tg.sendMessage({
        chat_id: chatId,
        text: modeText,
        reply_markup: modeMarkup,
      });
    }
    return;
  }

  await startCatalogFlow(
    userId,
    chatId,
    normalized.canonicalUrl,
    normalized.canonicalKey,
    tg,
    sessionService,
    catalogService,
    updateId,
    alreadyApplied
  );
}

/**
 * Shared catalog start/advance flow (T-PRICE-01): used by the URL message path
 * (mode already selected) and by the mode-selection callback continuation
 * (consuming the pending source URL). Callers must enforce the mode gate first.
 */
async function startCatalogFlow(
  userId: string,
  chatId: string,
  canonicalUrl: string,
  canonicalKey: string,
  tg: TelegramClient,
  sessionService: SessionService,
  catalogService: CatalogService,
  updateId?: number,
  alreadyApplied = false
): Promise<void> {
  // Create or deduplicate catalog request (atomic conflict-safe)
  const reqRecord = await catalogService.getOrCreateCatalogRequest(
    userId,
    canonicalUrl,
    canonicalKey
  );

  // Check if catalog data is already available
  const catalog = await catalogService.getCatalogByCanonicalKey(canonicalKey);

  if (!catalog) {
    // Catalog pending (future A23 agent will satisfy this)
    if (!alreadyApplied) {
      await sessionService.setStatusUnconditional(userId, 'AWAITING_CATALOG');
    }
    await tg.sendMessage({
      chat_id: chatId,
      text: `🔍 <b>Analyzing font catalog...</b>\n\nWe are analyzing:\n<code>${escapeHtml(
        canonicalUrl
      )}</code>\n\nPlease wait a moment.`,
    });
    return;
  }

  // Catalog is ready! Persist to session with fresh workflow_token and render style selection
  const catalogId = reqRecord.catalog_id || (await catalogService.persistCatalogResult(catalog));
  if (!alreadyApplied) {
    await sessionService.updateSessionCatalog(userId, catalogId, 'SELECTING_STYLES', updateId);
  }

  const session = await sessionService.getSessionByUserId(userId);
  if (session) {
    let selectedStyleIds: string[] = [];
    try {
      selectedStyleIds = JSON.parse(session.selected_styles);
    } catch {
      selectedStyleIds = [];
    }

    const { text: msgText, replyMarkup } = renderStyleSelection(
      catalog,
      selectedStyleIds,
      session.workflow_token
    );
    const sent = await tg.sendMessage({
      chat_id: chatId,
      text: msgText,
      reply_markup: replyMarkup,
    });

    if (sent.message_id) {
      await sessionService.setStatusUnconditional(userId, 'SELECTING_STYLES', sent.message_id);
    }
  }
}

async function handleCallbackQuery(
  query: TelegramCallbackQuery,
  tg: TelegramClient,
  env: Env,
  sessionService: SessionService,
  catalogService: CatalogService,
  orderService: OrderService,
  updateId: number,
  alreadyApplied: boolean
): Promise<void> {
  const userId = String(query.from.id);
  const data = query.data || '';

  // 1. Session verification
  const session = await sessionService.getSessionByUserId(userId);
  if (!session) {
    await tg.answerCallbackQuery({
      callback_query_id: query.id,
      text: 'Session expired. Please send a font link again.',
      show_alert: true,
    });
    return;
  }

  // On APPLIED retry: skip pre-mutation guards and directly replay the outbound UI from durable post-state
  if (alreadyApplied) {
    await replayAppliedCallbackUI(query, session, tg, env, catalogService, orderService);
    return;
  }

  // Parse callback action and parameters
  const parts = data.split(':');
  const [prefix, action, tokenOrParam, param] = parts;

  // Handle Check Payment Status (does not require catalog or workflow_token)
  if (prefix === 'ord' && action === 'chk') {
    const orderId = tokenOrParam;
    if (!orderId) {
      await tg.answerCallbackQuery({
        callback_query_id: query.id,
        text: 'Invalid order reference.',
        show_alert: true,
      });
      return;
    }

    const order = await orderService.getOrderById(orderId);
    if (!order || order.user_id !== userId) {
      await tg.answerCallbackQuery({
        callback_query_id: query.id,
        text: 'Order not found or unauthorized.',
        show_alert: true,
      });
      return;
    }

    const { text: msgText, replyMarkup } = renderOrderCreatedMessage(order, env);
    await tg.editMessageText({
      chat_id: session.chat_id,
      message_id: query.message?.message_id || session.last_message_id || undefined,
      text: msgText,
      reply_markup: replyMarkup,
    });

    await tg.answerCallbackQuery({
      callback_query_id: query.id,
      text: `Current status: ${order.status}`,
    });
    return;
  }

  // Handle MODE SELECTION actions (Prefix 'mode') - T-PRICE-01
  if (prefix === 'mode') {
    const modeValue = tokenOrParam as FontMode;
    const modeToken = param;

    if (
      action !== 'set' ||
      !isFontMode(modeValue) ||
      !modeToken ||
      modeToken !== session.workflow_token
    ) {
      await tg.answerCallbackQuery({
        callback_query_id: query.id,
        text: 'This menu is expired. Please use the latest message.',
        show_alert: true,
      });
      return;
    }

    if (session.mode !== null) {
      await tg.answerCallbackQuery({
        callback_query_id: query.id,
        text: 'Mode already selected for this session. Send /start to begin a new order.',
        show_alert: true,
      });
      return;
    }

    try {
      await sessionService.selectMode(
        userId,
        session.workflow_token,
        modeValue,
        session.version,
        updateId
      );
    } catch (err: unknown) {
      if (err instanceof SessionConflictError) {
        await tg.answerCallbackQuery({
          callback_query_id: query.id,
          text: 'Action conflict or menu expired. Please refresh.',
          show_alert: true,
        });
        return;
      }
      throw err;
    }

    // Consume the pending source URL (if any) and continue the /muahang flow.
    const pendingUrl = session.pending_source_url;
    if (pendingUrl) {
      const normalized = normalizeMyFontsUrl(pendingUrl);
      if (normalized.isValid && normalized.canonicalUrl && normalized.canonicalKey) {
        await startCatalogFlow(
          userId,
          session.chat_id,
          normalized.canonicalUrl,
          normalized.canonicalKey,
          tg,
          sessionService,
          catalogService,
          updateId,
          alreadyApplied
        );
        await tg.answerCallbackQuery({
          callback_query_id: query.id,
          text: `Mode ${modeValue} selected`,
        });
        return;
      }
    }

    await tg.sendMessage({
      chat_id: session.chat_id,
      text: `✅ <b>Mode: ${modeValue}.</b>\n\nNow send me a link to any font family on <b>MyFonts.com</b> to start.`,
    });
    await tg.answerCallbackQuery({
      callback_query_id: query.id,
      text: `Mode ${modeValue} selected`,
    });
    return;
  }

  // 2. Normal execution path with pre-mutation guards
  if (!session.catalog_id) {
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

  const token = tokenOrParam;

  // Verify workflow token match (stale button protection)
  if (!token || token !== session.workflow_token) {
    await tg.answerCallbackQuery({
      callback_query_id: query.id,
      text: 'This menu is expired. Please use the latest message.',
      show_alert: true,
    });
    return;
  }

  try {
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

        const updatedStyles = await sessionService.toggleStyleSelection(
          userId,
          session.workflow_token,
          targetStyle.id,
          session.version,
          updateId
        );

        const { text, replyMarkup } = renderStyleSelection(
          catalog,
          updatedStyles,
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
        await sessionService.setAllStyles(
          userId,
          session.workflow_token,
          allIds,
          session.version,
          updateId
        );
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
        await sessionService.clearStyles(
          userId,
          session.workflow_token,
          session.version,
          updateId
        );
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
        let currentStyles: string[] = [];
        let currentFormats: FontFormat[] = ['TTF'];
        try {
          currentStyles = JSON.parse(session.selected_styles);
          currentFormats = JSON.parse(session.selected_formats);
        } catch {
          currentStyles = [];
        }

        if (!currentStyles.length) {
          await tg.answerCallbackQuery({
            callback_query_id: query.id,
            text: 'Please select at least 1 style to continue.',
            show_alert: true,
          });
          return;
        }

        await sessionService.transitionStatus(
          userId,
          session.workflow_token,
          'SELECTING_STYLES',
          'SELECTING_FORMATS',
          session.version,
          undefined,
          updateId
        );

        const { text, replyMarkup } = renderFormatSelection(
          catalog,
          currentStyles.length,
          currentFormats,
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

      let currentStyles: string[] = [];
      try {
        currentStyles = JSON.parse(session.selected_styles);
      } catch {
        currentStyles = [];
      }

      // Back to styles
      if (action === 'bck') {
        await sessionService.transitionStatus(
          userId,
          session.workflow_token,
          'SELECTING_FORMATS',
          'SELECTING_STYLES',
          session.version,
          undefined,
          updateId
        );

        const { text, replyMarkup } = renderStyleSelection(
          catalog,
          currentStyles,
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
          const updatedFormats = await sessionService.toggleFormatSelection(
            userId,
            session.workflow_token,
            format,
            session.version,
            updateId
          );

          const { text, replyMarkup } = renderFormatSelection(
            catalog,
            currentStyles.length,
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
        let currentFormats: FontFormat[] = ['TTF'];
        try {
          currentFormats = JSON.parse(session.selected_formats);
        } catch {
          currentFormats = ['TTF'];
        }

        if (!currentFormats.length) {
          await tg.answerCallbackQuery({
            callback_query_id: query.id,
            text: 'Please select at least 1 font format.',
            show_alert: true,
          });
          return;
        }

        // T-PRICE-01 fail-closed: confirmation requires an explicit mode.
        // Legacy sessions with absent mode can never reach order confirmation.
        if (!isFontMode(session.mode)) {
          await tg.answerCallbackQuery({
            callback_query_id: query.id,
            text: 'Font mode is missing for this session. Send /start to begin a new order.',
            show_alert: true,
          });
          return;
        }

        await sessionService.transitionStatus(
          userId,
          session.workflow_token,
          'SELECTING_FORMATS',
          'CONFIRMING',
          session.version,
          undefined,
          updateId
        );

        const { text, replyMarkup } = renderOrderConfirmation(
          catalog,
          currentStyles,
          currentFormats,
          session.workflow_token,
          session.mode
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
        await sessionService.cancelSession(
          userId,
          session.workflow_token,
          session.version,
          updateId
        );

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

        // T-PRICE-01 fail-closed: an order can never be created without an
        // explicit ORIGINAL/VIETNAMESE mode (legacy sessions fail closed here).
        if (!isFontMode(session.mode)) {
          await tg.answerCallbackQuery({
            callback_query_id: query.id,
            text: 'This order cannot proceed: font mode is missing. Send /start to begin a new order.',
            show_alert: true,
          });
          return;
        }

        try {
          const result = await orderService.createOrderFromSession(
            session,
            catalog,
            updateId,
            env.PAYMENT_CODE_PREFIX || 'TF'
          );

          const order = await orderService.getOrderById(result.orderId);
          if (order) {
            const { text: msgText, replyMarkup } = renderOrderCreatedMessage(order, env);
            await tg.editMessageText({
              chat_id: session.chat_id,
              message_id: query.message?.message_id || session.last_message_id || undefined,
              text: msgText,
              reply_markup: replyMarkup,
            });
          }

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
  } catch (err: unknown) {
    if (err instanceof SessionConflictError) {
      await tg.answerCallbackQuery({
        callback_query_id: query.id,
        text: 'Action conflict or menu expired. Please refresh.',
        show_alert: true,
      });
      return;
    }
    throw err;
  }

  await tg.answerCallbackQuery({ callback_query_id: query.id });
}

async function replayAppliedCallbackUI(
  query: TelegramCallbackQuery,
  session: TelegramSessionRecord,
  tg: TelegramClient,
  env: Env,
  catalogService: CatalogService,
  orderService: OrderService
): Promise<void> {
  const messageId = query.message?.message_id || session.last_message_id || undefined;

  const safeAnswer = async (text?: string) => {
    try {
      await tg.answerCallbackQuery({
        callback_query_id: query.id,
        text,
      });
    } catch {
      // Non-blocking best-effort on APPLIED replay
    }
  };

  // T-PRICE-01: replay of a mode-selection callback. The selection is durable;
  // render the post-state UI for the current session stage.
  if ((query.data || '').startsWith('mode:')) {
    if (!session.mode) {
      // Selection never persisted; nothing to replay.
      await safeAnswer();
      return;
    }

    if (session.status === 'IDLE') {
      await tg.editMessageText({
        chat_id: session.chat_id,
        message_id: messageId,
        text: `✅ <b>Mode: ${session.mode}.</b>\n\nNow send me a link to any font family on <b>MyFonts.com</b> to start.`,
      });
      await safeAnswer(`Mode ${session.mode} selected`);
      return;
    }

    if (session.status === 'AWAITING_CATALOG') {
      await tg.editMessageText({
        chat_id: session.chat_id,
        message_id: messageId,
        text: `🔍 <b>Analyzing font catalog...</b>\n\nPlease wait a moment.`,
      });
      await safeAnswer(`Mode ${session.mode} selected`);
      return;
    }

    // SELECTING_STYLES / SELECTING_FORMATS / CONFIRMING / ORDER_CREATED fall
    // through to the generic status replay below.
  }

  if (session.status === 'IDLE') {
    await tg.editMessageText({
      chat_id: session.chat_id,
      message_id: messageId,
      text: '❌ <b>Order cancelled.</b>\n\nSend a new MyFonts link whenever you are ready.',
    });
    await safeAnswer('Order cancelled');
    return;
  }

  if (session.status === 'ORDER_CREATED') {
    if (session.active_order_id) {
      const order = await orderService.getOrderById(session.active_order_id);
      if (order) {
        const { text: msgText, replyMarkup } = renderOrderCreatedMessage(order, env);
        await tg.editMessageText({
          chat_id: session.chat_id,
          message_id: messageId,
          text: msgText,
          reply_markup: replyMarkup,
        });
      }
    }

    await safeAnswer('Order created successfully!');
    return;
  }

  if (!session.catalog_id) {
    await safeAnswer();
    return;
  }

  const catalog = await catalogService.getCatalogById(session.catalog_id);
  if (!catalog) {
    await safeAnswer();
    return;
  }

  let selectedStyles: string[] = [];
  let selectedFormats: FontFormat[] = ['TTF'];
  try {
    selectedStyles = JSON.parse(session.selected_styles);
    selectedFormats = JSON.parse(session.selected_formats);
  } catch {
    selectedStyles = [];
  }

  if (session.status === 'SELECTING_STYLES') {
    const { text, replyMarkup } = renderStyleSelection(
      catalog,
      selectedStyles,
      session.workflow_token
    );
    await tg.editMessageText({
      chat_id: session.chat_id,
      message_id: messageId,
      text,
      reply_markup: replyMarkup,
    });
    await safeAnswer();
    return;
  }

  if (session.status === 'SELECTING_FORMATS') {
    const { text, replyMarkup } = renderFormatSelection(
      catalog,
      selectedStyles.length,
      selectedFormats,
      session.workflow_token
    );
    await tg.editMessageText({
      chat_id: session.chat_id,
      message_id: messageId,
      text,
      reply_markup: replyMarkup,
    });
    await safeAnswer();
    return;
  }

  if (session.status === 'CONFIRMING') {
    // T-PRICE-01 fail-closed: a legacy session with absent mode can never
    // render a valid confirmation (exact price depends on the mode).
    if (!isFontMode(session.mode)) {
      await tg.editMessageText({
        chat_id: session.chat_id,
        message_id: messageId,
        text: '⚠️ <b>Font mode is missing for this session.</b>\n\nSend /start to begin a new order.',
      });
      await safeAnswer();
      return;
    }

    const { text, replyMarkup } = renderOrderConfirmation(
      catalog,
      selectedStyles,
      selectedFormats,
      session.workflow_token,
      session.mode
    );
    await tg.editMessageText({
      chat_id: session.chat_id,
      message_id: messageId,
      text,
      reply_markup: replyMarkup,
    });
    await safeAnswer();
    return;
  }

  await safeAnswer();
}

/**
 * T-PRICE-01: explicit ORIGINAL/VIETNAMESE mode selection screen.
 * The mode callback data format is `mode:set:<MODE>:<workflowToken>` and
 * remains far below Telegram's 64-byte callback limit.
 */
export function renderModeSelection(
  workflowToken: string
): { text: string; replyMarkup: InlineKeyboardMarkup } {
  const originalPrice = MODE_PRICE_PER_STYLE_VND.ORIGINAL.toLocaleString('vi-VN');
  const vietnamesePrice = MODE_PRICE_PER_STYLE_VND.VIETNAMESE.toLocaleString('vi-VN');

  const text = `🌐 <b>Choose your font version</b>\n\n` +
    `• <b>ORIGINAL</b> — exact original font (${originalPrice} VND/style)\n` +
    `• <b>VIETNAMESE</b> — original + full Vietnamese coverage (${vietnamesePrice} VND/style)\n\n` +
    `Select a mode to continue:`;

  const keyboard: InlineKeyboardMarkup['inline_keyboard'] = [
    [
      {
        text: `ORIGINAL · ${originalPrice}đ/style`,
        callback_data: `mode:set:ORIGINAL:${workflowToken}`,
      },
    ],
    [
      {
        text: `VIETNAMESE · ${vietnamesePrice}đ/style`,
        callback_data: `mode:set:VIETNAMESE:${workflowToken}`,
      },
    ],
  ];

  return { text, replyMarkup: { inline_keyboard: keyboard } };
}

export function renderStyleSelection(
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
  workflowToken: string,
  mode: FontMode
): { text: string; replyMarkup: InlineKeyboardMarkup } {
  const validStylesMap = new Map<string, Style>();
  for (const s of catalog.styles) validStylesMap.set(s.id, s);

  const selectedStyles: Style[] = [];
  for (const id of selectedStyleIds) {
    const s = validStylesMap.get(id);
    if (s) selectedStyles.push(s);
  }

  // T-PRICE-01: exact price is mode-bound (identical rule to order creation).
  const totalAmount = selectedStyles.reduce(
    (sum, s) => sum + stylePriceForMode(s.price, mode),
    0
  );
  const unitPrice = MODE_PRICE_PER_STYLE_VND[mode];

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
  }• <b>Mode:</b> <b>${mode}</b> (${unitPrice.toLocaleString('vi-VN')} VND/style)\n• <b>Styles (${
    selectedStyles.length
  }):</b>\n${stylesListText}${extraText}\n• <b>Formats:</b> ${selectedFormats.join(
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

export function renderOrderCreatedMessage(
  order: OrderRecord,
  env: Env
): { text: string; replyMarkup: InlineKeyboardMarkup } {
  const hasBankInfo = Boolean(env.BANK_ID && env.BANK_ACCOUNT_NUMBER);
  const paymentCode = order.payment_code || 'N/A';

  let bankSection = '';
  let qrSection = '';

  if (hasBankInfo && order.payment_code) {
    bankSection = `\n💳 <b>Bank Transfer Info:</b>\n• <b>Bank:</b> <code>${escapeHtml(
      env.BANK_ID!
    )}</code>\n• <b>Account No:</b> <code>${escapeHtml(
      env.BANK_ACCOUNT_NUMBER!
    )}</code>\n${
      env.BANK_ACCOUNT_NAME
        ? `• <b>Account Name:</b> <code>${escapeHtml(env.BANK_ACCOUNT_NAME)}</code>\n`
        : ''
    }• <b>Transfer Content / Code:</b> <code>${escapeHtml(paymentCode)}</code>\n`;

    const vietQrUrl = generateVietQrUrl({
      bankId: env.BANK_ID!,
      accountNumber: env.BANK_ACCOUNT_NUMBER!,
      amount: order.total_amount,
      paymentCode: order.payment_code,
      accountName: env.BANK_ACCOUNT_NAME,
      template: env.VIETQR_TEMPLATE,
    });

    qrSection = `\n📲 <a href="${escapeHtml(vietQrUrl)}"><b>Click here to open VietQR Code</b></a>\n`;
  }

  let statusBadge = `<code>${order.status}</code>`;
  let statusNote = '';
  if (order.status === 'COMPLETED') {
    statusBadge = `<b>COMPLETED 📦</b>`;
    statusNote = `\n🎉 <b>Your font bundle has been delivered directly to this chat as a document above!</b>\n`;
  } else if (order.status === 'PROCESSING') {
    statusBadge = `<b>PROCESSING ⚙️</b>`;
    statusNote = `\n⚙️ <i>Your fonts are currently being generated. This usually takes under a minute.</i>\n`;
  } else if (order.status === 'PAID') {
    statusBadge = `<b>PAID ✅</b>`;
    statusNote = `\n🎉 <i>Payment confirmed! Your order is queued for processing.</i>\n`;
  } else if (order.status === 'AWAITING_PAYMENT') {
    statusBadge = `<b>AWAITING_PAYMENT ⏳</b>`;
    statusNote = `\n⏳ <i>Please transfer the exact amount with the transfer content above. Payment is confirmed automatically within 1-2 minutes.</i>\n`;
  }

  // T-PRICE-01: the durable mode identity travels with the order and is shown
  // in every order-info surface. Absent mode = legacy order (shown as ABSENT).
  const modeLine = order.mode ? `• <b>Mode:</b> <b>${escapeHtml(order.mode)}</b>\n` : `• <b>Mode:</b> <code>ABSENT (legacy)</code>\n`;

  const text = `🎉 <b>Order Info:</b>\n\n• <b>Order ID:</b> <code>${order.id}</code>\n${modeLine}• <b>Status:</b> ${statusBadge}\n• <b>Payment Code:</b> <code>${escapeHtml(
    paymentCode
  )}</code>\n• <b>Amount:</b> <b>${order.total_amount.toLocaleString('vi-VN')} VND</b>\n${order.status === 'AWAITING_PAYMENT' ? bankSection + qrSection : ''}${statusNote}`;

  const keyboard: InlineKeyboardMarkup['inline_keyboard'] = [
    [
      {
        text: '🔄 Refresh Status',
        callback_data: `ord:chk:${order.id}`,
      },
    ],
  ];

  return { text, replyMarkup: { inline_keyboard: keyboard } };
}

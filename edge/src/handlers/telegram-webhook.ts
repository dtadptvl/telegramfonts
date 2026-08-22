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
import { generateVietQrUrl } from '../utils/vietqr';
import {
  ensureCustomerMenu,
  retireInteractiveMessage,
  TelegramClient,
} from '../services/telegram-client';
import { CatalogService } from '../services/catalog-service';
import { SessionService, SessionConflictError } from '../services/session-service';
import { OrderService, type OrderRecord } from '../services/order-service';

export async function handleTelegramWebhook(
  request: Request,
  env: Env,
  ctx: ExecutionContext
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

    if (update.message || update.callback_query) {
      if (typeof ctx?.waitUntil === 'function') {
        ctx.waitUntil(ensureCustomerMenu(tg));
      } else {
        void ensureCustomerMenu(tg);
      }
    }
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
  const session = await sessionService.getOrCreateSession(userId, chatId);
  const commandMatch = text.match(/^\/([a-z0-9_]+)(?:@[^\s]+)?$/i);
  const command = commandMatch?.[1].toLowerCase();

  if (command === 'start') {
    await retireInteractiveMessage(tg, chatId, session.last_message_id);
    if (!alreadyApplied) {
      await sessionService.resetSession(userId, chatId, updateId);
    }
    await tg.sendMessage({
      chat_id: chatId,
      text: `<b>Chào mừng bạn đến với TeleFont!</b> 🎨\n\nChọn <code>/muahang</code> để bắt đầu mua hàng hoặc <code>/trogiup</code> để xem hướng dẫn.`,
    });
    return;
  }

  if (command === 'trogiup') {
    await tg.sendMessage({
      chat_id: chatId,
      text: `<b>Trợ giúp</b>\n\nQuy trình mua hàng:\n1. Chọn <code>/muahang</code>.\n2. Gửi liên kết họ phông trên MyFonts.\n3. Chờ tải danh mục phông chữ.\n4. Chọn kiểu chữ.\n5. Chọn định dạng tệp.\n6. Xác nhận đơn hàng.\n7. Chuyển đúng số tiền với mã thanh toán được hiển thị.\n8. Hệ thống tự động xác nhận thanh toán.\n9. Tệp được xử lý và gửi trực tiếp vào cuộc trò chuyện dưới dạng tệp ZIP.`,
    });
    return;
  }

  if (command === 'muahang') {
    await retireInteractiveMessage(tg, chatId, session.last_message_id);
    if (!alreadyApplied) {
      await sessionService.resetSession(userId, chatId, updateId);
    }
    await tg.sendMessage({
      chat_id: chatId,
      text: `🛒 <b>Mua hàng</b>\n\nHãy gửi liên kết họ phông trên <b>MyFonts.com</b> để bắt đầu.`,
    });
    return;
  }

  // Check for MyFonts URL
  const normalized = normalizeMyFontsUrl(text);
  if (!normalized.isValid || !normalized.canonicalUrl || !normalized.canonicalKey) {
    await tg.sendMessage({
      chat_id: chatId,
      text: `⚠️ <b>Liên kết MyFonts không hợp lệ</b>\n\n${escapeHtml(
        localizeMyFontsReason(normalized.reason)
      )}\n\n<i>Ví dụ:</i> <code>https://www.myfonts.com/collections/helvetica-now-font-monotype-imaging</code>`,
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
    await retireInteractiveMessage(tg, chatId, session.last_message_id);
    await sessionService.setLastMessageId(userId, null);
    if (!alreadyApplied) {
      await sessionService.setStatusUnconditional(userId, 'AWAITING_CATALOG');
    }
    const sent = await tg.sendMessage({
      chat_id: chatId,
      text: `🔍 <b>Đang tải danh mục phông chữ...</b>\n\nĐang phân tích:\n<code>${escapeHtml(
        normalized.canonicalUrl
      )}</code>\n\nVui lòng chờ một chút.`,
    });
    if (sent.message_id) {
      await sessionService.setStatusUnconditional(userId, 'AWAITING_CATALOG', sent.message_id);
    }
    return;
  }

  // Catalog is ready! Persist to session with fresh workflow_token and render style selection
  await retireInteractiveMessage(tg, chatId, session.last_message_id);
  await sessionService.setLastMessageId(userId, null);
  const catalogId = reqRecord.catalog_id || (await catalogService.persistCatalogResult(catalog));
  if (!alreadyApplied) {
    await sessionService.updateSessionCatalog(userId, catalogId, 'SELECTING_STYLES', updateId);
  }

  const updatedSession = await sessionService.getSessionByUserId(userId);
  if (updatedSession) {
    let selectedStyleIds: string[] = [];
    try {
      selectedStyleIds = JSON.parse(updatedSession.selected_styles);
    } catch {
      selectedStyleIds = [];
    }

    const { text: msgText, replyMarkup } = renderStyleSelection(
      catalog,
      selectedStyleIds,
      updatedSession.workflow_token
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

function localizeMyFontsReason(reason?: string): string {
  switch (reason) {
    case 'Empty or invalid URL input':
      return 'Vui lòng gửi một liên kết MyFonts hợp lệ.';
    case 'Malformed URL':
      return 'Định dạng liên kết không hợp lệ.';
    case 'Only https protocol is allowed':
      return 'Liên kết phải sử dụng giao thức https.';
    case 'Host is not myfonts.com':
      return 'Liên kết phải thuộc myfonts.com.';
    case 'URL must point to a font collection, font family, or product page':
      return 'Liên kết phải trỏ đến bộ sưu tập, họ phông hoặc sản phẩm phông chữ.';
    case 'URL lacks a specific font family or product identifier':
      return 'Liên kết chưa có mã họ phông hoặc sản phẩm phông chữ cụ thể.';
    default:
      return 'Vui lòng gửi một liên kết https của họ phông trên MyFonts.';
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
      text: 'Phiên đã hết hạn. Vui lòng gửi lại liên kết phông chữ.',
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
        text: 'Mã đơn hàng không hợp lệ.',
        show_alert: true,
      });
      return;
    }

    const order = await orderService.getOrderById(orderId);
    if (!order || order.user_id !== userId) {
      await tg.answerCallbackQuery({
        callback_query_id: query.id,
        text: 'Không tìm thấy đơn hàng hoặc bạn không có quyền truy cập.',
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
      text: `Trạng thái: ${getOrderStatusLabel(order.status)}`,
    });
    return;
  }

  // 2. Normal execution path with pre-mutation guards
  if (!session.catalog_id) {
    await tg.answerCallbackQuery({
      callback_query_id: query.id,
      text: 'Phiên đã hết hạn. Vui lòng gửi lại liên kết phông chữ.',
      show_alert: true,
    });
    return;
  }

  const catalog = await catalogService.getCatalogById(session.catalog_id);
  if (!catalog) {
    await tg.answerCallbackQuery({
      callback_query_id: query.id,
      text: 'Không tìm thấy danh mục. Vui lòng gửi lại liên kết MyFonts.',
      show_alert: true,
    });
    return;
  }

  const token = tokenOrParam;

  // Verify workflow token match (stale button protection)
  if (!token || token !== session.workflow_token) {
    await tg.answerCallbackQuery({
      callback_query_id: query.id,
      text: 'Menu này đã hết hạn. Vui lòng dùng tin nhắn mới nhất.',
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
          text: 'Thao tác không còn hợp lệ ở bước hiện tại.',
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
            text: 'Không tìm thấy kiểu chữ trong danh mục.',
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
        await tg.answerCallbackQuery({ callback_query_id: query.id, text: 'Đã chọn tất cả kiểu chữ' });
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
        await tg.answerCallbackQuery({ callback_query_id: query.id, text: 'Đã bỏ chọn' });
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
            text: 'Vui lòng chọn ít nhất 1 kiểu chữ để tiếp tục.',
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
          text: 'Thao tác không còn hợp lệ ở bước hiện tại.',
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
            text: 'Vui lòng chọn ít nhất 1 định dạng tệp.',
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
        await sessionService.cancelSession(
          userId,
          session.workflow_token,
          session.version,
          updateId
        );

        await tg.editMessageText({
          chat_id: session.chat_id,
          message_id: query.message?.message_id || session.last_message_id || undefined,
          text: '❌ <b>Đã hủy đơn hàng.</b>\n\nBạn có thể gửi một liên kết MyFonts mới bất cứ lúc nào.',
        });
        await tg.answerCallbackQuery({ callback_query_id: query.id, text: 'Đã hủy đơn hàng' });
        return;
      }

      // Confirm Order
      if (action === 'cnf') {
        if (session.status !== 'CONFIRMING') {
          await tg.answerCallbackQuery({
            callback_query_id: query.id,
            text: 'Xác nhận đơn hàng không còn hợp lệ.',
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
            text: result.isExisting ? 'Đơn hàng đã được tạo trước đó.' : 'Đã tạo đơn hàng thành công.',
          });
          return;
        } catch {
          // Controlled generic message without leaking internal D1 error text (BLOCK 5)
          await tg.answerCallbackQuery({
            callback_query_id: query.id,
            text: 'Đã xảy ra lỗi khi tạo đơn hàng. Vui lòng thử lại.',
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
        text: 'Thao tác bị xung đột hoặc menu đã hết hạn. Vui lòng cập nhật lại.',
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

  if (session.status === 'IDLE') {
    await tg.editMessageText({
      chat_id: session.chat_id,
      message_id: messageId,
      text: '❌ <b>Đã hủy đơn hàng.</b>\n\nBạn có thể gửi một liên kết MyFonts mới bất cứ lúc nào.',
    });
    await safeAnswer('Đã hủy đơn hàng');
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

    await safeAnswer('Đã tạo đơn hàng thành công.');
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
    const { text, replyMarkup } = renderOrderConfirmation(
      catalog,
      selectedStyles,
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

  await safeAnswer();
}

export function renderStyleSelection(
  catalog: FontCatalog,
  selectedStyleIds: string[],
  workflowToken: string
): { text: string; replyMarkup: InlineKeyboardMarkup } {
  const text = `📦 <b>${escapeHtml(catalog.familyName)}</b>\n${
    catalog.foundry ? `<i>Nhà phát hành: ${escapeHtml(catalog.foundry)}</i>\n` : ''
  }\nĐã chọn: <b>${selectedStyleIds.length} / ${
    catalog.styles.length
  }</b>\n\nChạm vào kiểu chữ bên dưới để chọn hoặc bỏ chọn:`;

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
    { text: 'Chọn tất cả', callback_data: `st:all:${workflowToken}` },
    { text: 'Bỏ chọn', callback_data: `st:clr:${workflowToken}` },
  ]);

  keyboard.push([
    {
      text: `Tiếp theo: chọn định dạng (${selectedStyleIds.length}) ➡️`,
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
  )}</b>\n\nSố kiểu chữ đã chọn: <b>${stylesCount}</b>\n\nChọn định dạng tệp cần nhận:`;

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
      { text: '⬅️ Quay lại kiểu chữ', callback_data: `fmt:bck:${workflowToken}` },
      { text: 'Xem lại đơn ➡️', callback_data: `fmt:nxt:${workflowToken}` },
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
    (sum, s) => sum + (s.price !== undefined ? s.price : 5000),
    0
  );

  const stylesListText = selectedStyles
    .map((s) => `  • ${escapeHtml(s.displayName)}`)
    .slice(0, 15)
    .join('\n');

  const extraStylesCount = selectedStyles.length > 15 ? selectedStyles.length - 15 : 0;
  const extraText = extraStylesCount > 0 ? `\n  <i>...và ${extraStylesCount} kiểu chữ khác</i>` : '';

  const text = `📋 <b>Xác nhận đơn hàng</b>\n\n• <b>Họ phông:</b> ${escapeHtml(
    catalog.familyName
  )}\n${
    catalog.foundry ? `• <b>Nhà phát hành:</b> ${escapeHtml(catalog.foundry)}\n` : ''
  }• <b>Kiểu chữ (${selectedStyles.length}):</b>\n${stylesListText}${extraText}\n• <b>Định dạng:</b> ${selectedFormats.join(
    ', '
  )}\n• <b>Tổng tiền:</b> <b>${totalAmount.toLocaleString('vi-VN')} VND</b>\n\nXác nhận để tạo đơn hàng:`;

  const keyboard: InlineKeyboardMarkup['inline_keyboard'] = [
    [
      { text: '❌ Hủy', callback_data: `ord:ccl:${workflowToken}` },
      { text: '💳 Xác nhận đơn', callback_data: `ord:cnf:${workflowToken}` },
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
    bankSection = `\n💳 <b>Thông tin chuyển khoản:</b>\n• <b>Ngân hàng:</b> <code>${escapeHtml(
      env.BANK_ID!
    )}</code>\n• <b>Số tài khoản:</b> <code>${escapeHtml(
      env.BANK_ACCOUNT_NUMBER!
    )}</code>\n${
    env.BANK_ACCOUNT_NAME
        ? `• <b>Tên tài khoản:</b> <code>${escapeHtml(env.BANK_ACCOUNT_NAME)}</code>\n`
        : ''
    }• <b>Nội dung / mã chuyển khoản:</b> <code>${escapeHtml(paymentCode)}</code>\n`;

    const vietQrUrl = generateVietQrUrl({
      bankId: env.BANK_ID!,
      accountNumber: env.BANK_ACCOUNT_NUMBER!,
      amount: order.total_amount,
      paymentCode: order.payment_code,
      accountName: env.BANK_ACCOUNT_NAME,
      template: env.VIETQR_TEMPLATE,
    });

    qrSection = `\n📲 <a href="${escapeHtml(vietQrUrl)}"><b>Mở mã VietQR</b></a>\n`;
  }

  let statusBadge = `<code>${escapeHtml(getOrderStatusLabel(order.status))}</code>`;
  let statusNote = '';
  if (order.status === 'COMPLETED') {
    statusBadge = `<b>Đã hoàn tất 📦</b>`;
    statusNote = `\n🎉 <b>Tệp ZIP đã được gửi trực tiếp vào cuộc trò chuyện này.</b>\n`;
  } else if (order.status === 'PROCESSING') {
    statusBadge = `<b>Đang xử lý ⚙️</b>`;
    statusNote = `\n⚙️ <i>Phông chữ đang được tạo. Thường sẽ mất chưa đến một phút.</i>\n`;
  } else if (order.status === 'PAID') {
    statusBadge = `<b>Đã thanh toán ✅</b>`;
    statusNote = `\n✅ <i>Thanh toán thành công. Đang xử lý tệp...</i>\n`;
  } else if (order.status === 'AWAITING_PAYMENT') {
    statusBadge = `<b>Chờ thanh toán ⏳</b>`;
    statusNote = `\n⏳ <i>Vui lòng chuyển đúng số tiền và ghi đúng mã thanh toán ở trên. Hệ thống sẽ tự động xác nhận trong 1–2 phút.</i>\n`;
  }

  const text = `🎉 <b>Thông tin đơn hàng:</b>\n\n• <b>Mã đơn:</b> <code>${escapeHtml(order.id)}</code>\n• <b>Trạng thái:</b> ${statusBadge}\n• <b>Mã thanh toán:</b> <code>${escapeHtml(
    paymentCode
  )}</code>\n• <b>Số tiền:</b> <b>${order.total_amount.toLocaleString('vi-VN')} VND</b>\n${order.status === 'AWAITING_PAYMENT' ? bankSection + qrSection : ''}${statusNote}`;

  const keyboard: InlineKeyboardMarkup['inline_keyboard'] = [
    [
      {
        text: '🔄 Cập nhật trạng thái',
        callback_data: `ord:chk:${order.id}`,
      },
    ],
  ];

  return { text, replyMarkup: { inline_keyboard: keyboard } };
}

function getOrderStatusLabel(status: string): string {
  switch (status) {
    case 'AWAITING_PAYMENT':
      return 'Chờ thanh toán';
    case 'PAID':
      return 'Đã thanh toán';
    case 'PROCESSING':
      return 'Đang xử lý';
    case 'COMPLETED':
      return 'Đã hoàn tất';
    case 'FAILED':
      return 'Xử lý thất bại';
    case 'CANCELLED':
      return 'Đã hủy';
    default:
      return 'Đang cập nhật';
  }
}

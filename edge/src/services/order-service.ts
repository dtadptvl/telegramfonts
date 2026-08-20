import type { FontCatalog, Style } from '../types/catalog';
import type { TelegramSessionRecord, FontFormat } from '../types/session';

export interface CreateOrderResult {
  orderId: string;
  isExisting: boolean;
  totalAmount: number;
  currency: string;
  itemsCount: number;
}

export class OrderService {
  constructor(private readonly db: D1Database) {}

  async createOrderFromSession(
    session: TelegramSessionRecord,
    catalog: FontCatalog
  ): Promise<CreateOrderResult> {
    const checkoutToken = session.checkout_token;

    // 1. Check if order with this checkout_token already exists (Idempotency)
    const existingOrderByToken = await this.db
      .prepare('SELECT * FROM orders WHERE checkout_token = ?')
      .bind(checkoutToken)
      .first<{ id: string; total_amount: number; currency: string }>();

    if (existingOrderByToken) {
      const items = await this.db
        .prepare('SELECT count(*) as count FROM order_items WHERE order_id = ?')
        .bind(existingOrderByToken.id)
        .first<{ count: number }>();

      return {
        orderId: existingOrderByToken.id,
        isExisting: true,
        totalAmount: existingOrderByToken.total_amount,
        currency: existingOrderByToken.currency,
        itemsCount: items?.count || 0,
      };
    }

    // 2. Validate Session Invariants
    let selectedStyleIds: string[] = [];
    let selectedFormats: FontFormat[] = [];
    try {
      selectedStyleIds = JSON.parse(session.selected_styles);
      selectedFormats = JSON.parse(session.selected_formats);
    } catch {
      throw new Error('Invalid session selection payload');
    }

    if (!selectedStyleIds.length) {
      throw new Error('Cannot create order with empty style selection');
    }

    if (!selectedFormats.length) {
      throw new Error('Cannot create order with empty format selection');
    }

    // Validate styles against authoritative persisted catalog (never trust client)
    const validStylesMap = new Map<string, Style>();
    for (const style of catalog.styles) {
      validStylesMap.set(style.id, style);
    }

    const selectedStyles: Style[] = [];
    for (const styleId of selectedStyleIds) {
      const found = validStylesMap.get(styleId);
      if (!found) {
        throw new Error(`Style ID "${styleId}" does not exist in catalog`);
      }
      selectedStyles.push(found);
    }

    const now = Date.now();
    const orderId = `ord_${crypto.randomUUID().replace(/-/g, '')}`;

    const totalAmount = selectedStyles.reduce(
      (sum, s) => sum + (s.price !== undefined ? s.price : 50000),
      0
    );

    const metadata = JSON.stringify({
      chat_id: session.chat_id,
      catalog_id: session.catalog_id,
      family_name: catalog.familyName,
      foundry: catalog.foundry,
      source_url: catalog.sourceUrl,
      selected_style_ids: selectedStyleIds,
      selected_formats: selectedFormats,
    });

    // 3. Build atomic batch statements
    const statements: D1PreparedStatement[] = [];

    // Order insert
    statements.push(
      this.db
        .prepare(
          `INSERT INTO orders (id, user_id, status, total_amount, currency, metadata, checkout_token, created_at, updated_at)
           VALUES (?, ?, 'AWAITING_PAYMENT', ?, 'VND', ?, ?, ?, ?)`
        )
        .bind(orderId, session.user_id, totalAmount, metadata, checkoutToken, now, now)
    );

    // Order items insert
    for (const style of selectedStyles) {
      const itemId = `item_${crypto.randomUUID().replace(/-/g, '')}`;
      const price = style.price !== undefined ? style.price : 50000;
      statements.push(
        this.db
          .prepare(
            `INSERT INTO order_items (id, order_id, font_id, font_name, price, created_at)
             VALUES (?, ?, ?, ?, ?, ?)`
          )
          .bind(itemId, orderId, style.id, style.displayName, price, now)
      );
    }

    // Session update
    statements.push(
      this.db
        .prepare(
          `UPDATE telegram_sessions SET status = 'ORDER_CREATED', active_order_id = ?, updated_at = ? WHERE id = ?`
        )
        .bind(orderId, now, session.id)
    );

    // Execute atomic batch
    try {
      await this.db.batch(statements);
    } catch (err: unknown) {
      // If concurrent request already inserted with this checkout_token, resolve to existing order
      const existingAfterCatch = await this.db
        .prepare('SELECT * FROM orders WHERE checkout_token = ?')
        .bind(checkoutToken)
        .first<{ id: string; total_amount: number; currency: string }>();

      if (existingAfterCatch) {
        return {
          orderId: existingAfterCatch.id,
          isExisting: true,
          totalAmount: existingAfterCatch.total_amount,
          currency: existingAfterCatch.currency,
          itemsCount: selectedStyles.length,
        };
      }
      throw err;
    }

    return {
      orderId,
      isExisting: false,
      totalAmount,
      currency: 'VND',
      itemsCount: selectedStyles.length,
    };
  }
}

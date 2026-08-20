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
    // Check if session already has an active order (Idempotency)
    if (session.status === 'ORDER_CREATED' && session.active_order_id) {
      const existingOrder = await this.db
        .prepare('SELECT * FROM orders WHERE id = ?')
        .bind(session.active_order_id)
        .first<{ id: string; total_amount: number; currency: string }>();

      if (existingOrder) {
        const items = await this.db
          .prepare('SELECT count(*) as count FROM order_items WHERE order_id = ?')
          .bind(existingOrder.id)
          .first<{ count: number }>();

        return {
          orderId: existingOrder.id,
          isExisting: true,
          totalAmount: existingOrder.total_amount,
          currency: existingOrder.currency,
          itemsCount: items?.count || 0,
        };
      }
    }

    // Invariant checks
    let selectedStyleIds: string[] = [];
    let selectedFormats: FontFormat[] = [];
    try {
      selectedStyleIds = JSON.parse(session.selected_styles);
      selectedFormats = JSON.parse(session.selected_formats);
    } catch {
      throw new Error('Invalid session selection format');
    }

    if (!selectedStyleIds.length) {
      throw new Error('Cannot create order with empty style selection');
    }

    if (!selectedFormats.length) {
      throw new Error('Cannot create order with empty format selection');
    }

    // Validate styles against persisted catalog styles (never trust client/callback text)
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

    // Compute total amount (sum of selected styles' prices)
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

    // Insert Order
    await this.db
      .prepare(
        `INSERT INTO orders (id, user_id, status, total_amount, currency, metadata, created_at, updated_at)
         VALUES (?, ?, 'AWAITING_PAYMENT', ?, 'VND', ?, ?, ?)`
      )
      .bind(orderId, session.user_id, totalAmount, metadata, now, now)
      .run();

    // Insert Order Items
    for (const style of selectedStyles) {
      const itemId = `item_${crypto.randomUUID().replace(/-/g, '')}`;
      const price = style.price !== undefined ? style.price : 50000;
      await this.db
        .prepare(
          `INSERT INTO order_items (id, order_id, font_id, font_name, price, created_at)
           VALUES (?, ?, ?, ?, ?, ?)`
        )
          .bind(itemId, orderId, style.id, style.displayName, price, now)
          .run();
    }

    // Update Session with created order
    await this.db
      .prepare(
        `UPDATE telegram_sessions SET status = 'ORDER_CREATED', active_order_id = ?, updated_at = ? WHERE id = ?`
      )
      .bind(orderId, now, session.id)
      .run();

    return {
      orderId,
      isExisting: false,
      totalAmount,
      currency: 'VND',
      itemsCount: selectedStyles.length,
    };
  }
}

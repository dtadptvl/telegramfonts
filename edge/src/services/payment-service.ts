export interface ProcessPaymentParams {
  transactionId: string;
  orderId: string;
  paymentCode: string;
  expectedAmount: number;
}

export interface ProcessPaymentResult {
  status: 'PROCESSED' | 'DUPLICATE' | 'UNMATCHED' | 'CONFLICT';
  paymentId?: string;
  orderId?: string;
  jobId?: string;
}

export class PaymentService {
  constructor(private readonly db: D1Database) {}

  async processVerifiedPayment(
    params: ProcessPaymentParams,
    injectedFailureStatement?: D1PreparedStatement
  ): Promise<ProcessPaymentResult> {
    const { transactionId, orderId, paymentCode, expectedAmount } = params;

    // 1. Check if this SePay transaction was already processed (Provider-level idempotency)
    const existingPayment = await this.db
      .prepare('SELECT id, status FROM payments WHERE transaction_id = ?')
      .bind(transactionId)
      .first<{ id: string; status: string }>();

    if (existingPayment) {
      return { status: 'DUPLICATE', paymentId: existingPayment.id, orderId };
    }

    const now = Date.now();
    const paymentId = `pay_${crypto.randomUUID().replace(/-/g, '')}`;
    const jobId = `job_${crypto.randomUUID().replace(/-/g, '')}`;
    const outboxId = `outbox_${crypto.randomUUID().replace(/-/g, '')}`;

    // 2. Build strictly conditional atomic batch with FULL financial predicate binding (BLOCK 4)
    // All 4 operations require order to match: id, payment_code, total_amount, currency='VND', status='AWAITING_PAYMENT'
    const statements: D1PreparedStatement[] = [
      // 1. Insert payments row
      this.db
        .prepare(
          `INSERT INTO payments (id, order_id, provider, transaction_id, amount, currency, status, created_at, updated_at)
           SELECT ?, ?, 'SEPAY', ?, ?, 'VND', 'VERIFIED', ?, ?
           WHERE EXISTS (
             SELECT 1 FROM orders
             WHERE id = ? AND payment_code = ? AND total_amount = ? AND currency = 'VND' AND status = 'AWAITING_PAYMENT'
           )`
        )
        .bind(
          paymentId,
          orderId,
          transactionId,
          expectedAmount,
          now,
          now,
          orderId,
          paymentCode,
          expectedAmount
        ),

      // 2. Insert fulfillment_jobs row (unique 1:1 constraint per order)
      this.db
        .prepare(
          `INSERT INTO fulfillment_jobs (id, order_id, status, attempt_count, max_attempts, created_at, updated_at)
           SELECT ?, ?, 'PENDING', 0, 3, ?, ?
           WHERE EXISTS (
             SELECT 1 FROM orders
             WHERE id = ? AND payment_code = ? AND total_amount = ? AND currency = 'VND' AND status = 'AWAITING_PAYMENT'
           )`
        )
        .bind(jobId, orderId, now, now, orderId, paymentCode, expectedAmount),

      // 3. Insert outbox_events row (unique aggregate_type + aggregate_id + event_type constraint)
      this.db
        .prepare(
          `INSERT INTO outbox_events (id, event_type, aggregate_type, aggregate_id, payload, status, created_at)
           SELECT ?, 'JOB_READY', 'ORDER', ?, ?, 'PENDING', ?
           WHERE EXISTS (
             SELECT 1 FROM orders
             WHERE id = ? AND payment_code = ? AND total_amount = ? AND currency = 'VND' AND status = 'AWAITING_PAYMENT'
           )`
        )
        .bind(
          outboxId,
          orderId,
          JSON.stringify({ job_id: jobId }),
          now,
          orderId,
          paymentCode,
          expectedAmount
        ),

      // 4. Transition order status: AWAITING_PAYMENT -> PAID
      this.db
        .prepare(
          `UPDATE orders SET status = 'PAID', updated_at = ?
           WHERE id = ? AND payment_code = ? AND total_amount = ? AND currency = 'VND' AND status = 'AWAITING_PAYMENT'`
        )
        .bind(now, orderId, paymentCode, expectedAmount),
    ];

    if (injectedFailureStatement) {
      statements.push(injectedFailureStatement);
    }

    try {
      const results = await this.db.batch(statements);
      const paymentInsertResult = results[0];
      const orderUpdateResult = results[3];

      if (
        !paymentInsertResult.meta.changes ||
        paymentInsertResult.meta.changes === 0 ||
        !orderUpdateResult.meta.changes ||
        orderUpdateResult.meta.changes === 0
      ) {
        // Business precondition was not met (predicate failed)
        // Check if order was already paid by a racing transaction
        const currentOrder = await this.db
          .prepare('SELECT status FROM orders WHERE id = ?')
          .bind(orderId)
          .first<{ status: string }>();

        if (currentOrder?.status === 'PAID') {
          return { status: 'CONFLICT', orderId };
        }

        return { status: 'UNMATCHED', orderId };
      }

      return { status: 'PROCESSED', paymentId, orderId, jobId };
    } catch (err: unknown) {
      // If concurrent request processed the exact same transactionId, resolve as duplicate
      const duplicateAfterCatch = await this.db
        .prepare('SELECT id FROM payments WHERE transaction_id = ?')
        .bind(transactionId)
        .first<{ id: string }>();

      if (duplicateAfterCatch) {
        return { status: 'DUPLICATE', paymentId: duplicateAfterCatch.id, orderId };
      }

      throw err;
    }
  }
}

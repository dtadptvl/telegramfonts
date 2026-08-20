export interface VietQrParams {
  bankId: string;
  accountNumber: string;
  amount: number;
  paymentCode: string;
  accountName?: string;
  template?: string;
}

const ALPHANUMERIC_CHARS = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';

export function validatePaymentCodePrefix(prefix: string): string {
  if (prefix === undefined || prefix === null || prefix === '') {
    return 'TF';
  }
  const normalized = String(prefix).trim().toUpperCase();
  if (!/^[A-Z0-9]{2,5}$/.test(normalized)) {
    throw new Error(
      `Invalid payment code prefix "${prefix}": must be 2-5 uppercase alphanumeric characters`
    );
  }
  return normalized;
}

export function generatePaymentCode(prefix = 'TF'): string {
  const validatedPrefix = validatePaymentCodePrefix(prefix);
  const randomBytes = new Uint8Array(6);
  crypto.getRandomValues(randomBytes);

  let suffix = '';
  for (let i = 0; i < 6; i++) {
    suffix += ALPHANUMERIC_CHARS[randomBytes[i] % ALPHANUMERIC_CHARS.length];
  }

  return `${validatedPrefix}${suffix}`;
}

export function generateVietQrUrl(params: VietQrParams): string {
  const bank = encodeURIComponent(params.bankId.trim());
  const account = encodeURIComponent(params.accountNumber.trim());
  const template = encodeURIComponent((params.template || 'compact2').trim());
  const amount = Math.round(params.amount);
  const addInfo = encodeURIComponent(params.paymentCode.trim());

  let url = `https://img.vietqr.io/image/${bank}-${account}-${template}.png?amount=${amount}&addInfo=${addInfo}`;

  if (params.accountName && params.accountName.trim()) {
    url += `&accountName=${encodeURIComponent(params.accountName.trim())}`;
  }

  return url;
}

export interface VietQrParams {
  bankId: string;
  accountNumber: string;
  amount: number;
  paymentCode: string;
  accountName?: string;
  template?: string;
}

const ALPHANUMERIC_CHARS = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';

export function generatePaymentCode(prefix = 'TF'): string {
  const cleanPrefix = (prefix || 'TF').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 5);
  const randomBytes = new Uint8Array(6);
  crypto.getRandomValues(randomBytes);

  let suffix = '';
  for (let i = 0; i < 6; i++) {
    suffix += ALPHANUMERIC_CHARS[randomBytes[i] % ALPHANUMERIC_CHARS.length];
  }

  return `${cleanPrefix}${suffix}`;
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

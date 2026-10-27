import { formatCatalogPrice } from './catalogView.js';

export function normalizeCartQuantity(stockQuantity, value) {
  const parsed = Number.parseInt(value, 10);
  const requested = Number.isSafeInteger(parsed) ? parsed : 1;
  return Math.max(1, Math.min(stockQuantity, 1000, requested));
}

export function addProductToCart(items, product, quantity) {
  if (!product.available || product.stock_quantity < 1) return items;
  const requested = normalizeCartQuantity(product.stock_quantity, quantity);
  const existing = items.find((item) => item.sku === product.sku);
  if (!existing) return [...items, { ...product, quantity: requested }];
  return items.map((item) => item.sku === product.sku
    ? { ...item, quantity: Math.min(item.stock_quantity, 1000, item.quantity + requested) }
    : item);
}

export function updateCartQuantity(items, sku, quantity) {
  return items.map((item) => item.sku === sku
    ? { ...item, quantity: normalizeCartQuantity(item.stock_quantity, quantity) }
    : item);
}

export function removeCartItem(items, sku) {
  return items.filter((item) => item.sku !== sku);
}

function toMinorUnits(amount) {
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(String(amount));
  if (!match) return null;
  return BigInt(match[1]) * 100n + BigInt((match[2] ?? '').padEnd(2, '0'));
}

function fromMinorUnits(value) {
  const whole = value / 100n;
  const fraction = String(value % 100n).padStart(2, '0');
  return `${whole}.${fraction}`;
}

export function cartLineTotal(item) {
  const unit = toMinorUnits(item.unit_price.amount);
  if (unit === null) return { amount: item.unit_price.amount, currency: item.unit_price.currency };
  return { amount: fromMinorUnits(unit * BigInt(item.quantity)), currency: item.unit_price.currency };
}

export function cartTotals(items) {
  const totals = new Map();
  for (const item of items) {
    const unit = toMinorUnits(item.unit_price.amount);
    if (unit === null) continue;
    const currency = item.unit_price.currency;
    totals.set(currency, (totals.get(currency) ?? 0n) + unit * BigInt(item.quantity));
  }
  return [...totals.entries()].map(([currency, amount]) => ({
    currency, amount: fromMinorUnits(amount),
  }));
}

export function formatCartMoney(money, locale) {
  return formatCatalogPrice(money, locale);
}

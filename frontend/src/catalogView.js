export const CATALOG_AVAILABILITY = Object.freeze(['all', 'available', 'unavailable']);
export const CATALOG_SORTS = Object.freeze(['featured', 'name-asc', 'name-desc', 'price-asc', 'price-desc']);
export const CATALOG_CATEGORY_LABELS = Object.freeze({
  laptops: 'Laptops', displays: 'Displays', docks_hubs: 'Docks and hubs',
  input_devices: 'Input devices', audio: 'Audio', storage: 'Storage',
  bags_protection: 'Bags and protection', warranties_services: 'Warranties and services',
});

const normalize = (value) => String(value ?? '').trim().toLocaleLowerCase();

function decimalParts(value) {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(String(value));
  if (!match) return null;
  const fraction = (match[2] ?? '').replace(/0+$/, '');
  return { whole: match[1].replace(/^0+(?=\d)/, ''), fraction };
}

function compareDecimalStrings(left, right) {
  const a = decimalParts(left);
  const b = decimalParts(right);
  if (!a || !b) return String(left).localeCompare(String(right));
  if (a.whole.length !== b.whole.length) return a.whole.length - b.whole.length;
  const whole = a.whole.localeCompare(b.whole);
  if (whole !== 0) return whole;
  const width = Math.max(a.fraction.length, b.fraction.length);
  return a.fraction.padEnd(width, '0').localeCompare(b.fraction.padEnd(width, '0'));
}

function comparePrice(left, right) {
  const currency = left.unit_price.currency.localeCompare(right.unit_price.currency);
  return currency || compareDecimalStrings(left.unit_price.amount, right.unit_price.amount);
}

export function selectCatalogProducts(products, {
  query = '', category = 'all', availability = 'all', sort = 'featured',
} = {}) {
  const safeAvailability = CATALOG_AVAILABILITY.includes(availability) ? availability : 'all';
  const safeSort = CATALOG_SORTS.includes(sort) ? sort : 'featured';
  const term = normalize(query);

  const selected = products.filter((product) => {
    if (category !== 'all' && product.category !== category) return false;
    if (safeAvailability === 'available' && !product.available) return false;
    if (safeAvailability === 'unavailable' && product.available) return false;
    if (!term) return true;
    return [product.name, product.sku, product.description].some((value) => normalize(value).includes(term));
  });

  return selected
    .map((product, index) => ({ product, index }))
    .sort((left, right) => {
      let comparison = 0;
      if (safeSort === 'featured') {
        comparison = Number(right.product.available) - Number(left.product.available)
          || left.product.name.localeCompare(right.product.name);
      } else if (safeSort === 'name-asc') {
        comparison = left.product.name.localeCompare(right.product.name);
      } else if (safeSort === 'name-desc') {
        comparison = right.product.name.localeCompare(left.product.name);
      } else if (safeSort === 'price-asc') {
        comparison = comparePrice(left.product, right.product);
      } else if (safeSort === 'price-desc') {
        comparison = comparePrice(right.product, left.product);
      }
      return comparison || left.index - right.index;
    })
    .map(({ product }) => product);
}

export function formatCatalogPrice(unitPrice, locale) {
  const amount = Number(unitPrice.amount);
  if (!Number.isFinite(amount) || !/^[A-Z]{3}$/.test(unitPrice.currency)) {
    return `${unitPrice.currency} ${unitPrice.amount}`;
  }
  try {
    return new Intl.NumberFormat(locale, {
      style: 'currency', currency: unitPrice.currency,
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    }).format(amount);
  } catch {
    return `${unitPrice.currency} ${unitPrice.amount}`;
  }
}

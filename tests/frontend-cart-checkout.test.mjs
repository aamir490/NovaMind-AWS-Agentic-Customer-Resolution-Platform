import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import {
  addProductToCart, cartLineTotal, cartTotals, removeCartItem, updateCartQuantity,
} from '../frontend/src/cart.js';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { renderToStaticMarkup } = require('react-dom/server');

function product(overrides = {}) {
  return {
    id: '00000000-0000-4000-8000-000000000010', sku: 'LAP-1', name: 'Laptop',
    description: 'Business laptop', available: true, category: 'laptops',
    image_path: '/products/laptops.svg', stock_quantity: 5,
    unit_price: { amount: '1299.99', currency: 'USD' }, ...overrides,
  };
}

test('cart adds distinct products, merges duplicate SKUs, caps stock, updates, and removes immutably', () => {
  const laptop = product();
  const dock = product({ id: '00000000-0000-4000-8000-000000000011', sku: 'DOC-1',
    name: 'Dock', stock_quantity: 12, unit_price: { amount: '149.99', currency: 'USD' } });
  const original = [];
  const one = addProductToCart(original, laptop, 2);
  const two = addProductToCart(one, dock, 1);
  const capped = addProductToCart(two, laptop, 10);
  assert.deepEqual(original, []);
  assert.deepEqual(capped.map(({ sku, quantity }) => ({ sku, quantity })), [
    { sku: 'LAP-1', quantity: 5 }, { sku: 'DOC-1', quantity: 1 },
  ]);
  const updated = updateCartQuantity(capped, 'DOC-1', 4);
  assert.equal(updated[1].quantity, 4);
  assert.equal(capped[1].quantity, 1);
  assert.deepEqual(removeCartItem(updated, 'LAP-1').map((item) => item.sku), ['DOC-1']);
  assert.equal(addProductToCart(updated, product({ sku: 'OUT-1', available: false,
    stock_quantity: 0 }), 1), updated);
});

test('cart computes exact line subtotals and totals from backend amount and currency values', () => {
  const items = [
    { ...product(), quantity: 3 },
    { ...product({ sku: 'DOC-1', unit_price: { amount: '149.99', currency: 'USD' } }), quantity: 2 },
    { ...product({ sku: 'EUR-1', unit_price: { amount: '10.50', currency: 'EUR' } }), quantity: 2 },
  ];
  assert.deepEqual(cartLineTotal(items[0]), { amount: '3899.97', currency: 'USD' });
  assert.deepEqual(cartTotals(items), [
    { amount: '4199.95', currency: 'USD' }, { amount: '21.00', currency: 'EUR' },
  ]);
});

function* elements(node) {
  if (Array.isArray(node)) { for (const child of node) yield* elements(child); }
  else if (node?.props) { yield node; yield* elements(node.props.children); }
}

function find(tree, predicate) {
  const result = [...elements(tree)].find(predicate);
  assert.ok(result, 'Expected element to be present');
  return result;
}

async function componentHarness(t) {
  const hookModule = '\0cart-checkout-hooks';
  const { createServer } = await import(pathToFileURL(require.resolve('vite')).href);
  const { default: react } = await import(pathToFileURL(require.resolve('@vitejs/plugin-react')).href);
  const server = await createServer({ root: fileURLToPath(new URL('../frontend', import.meta.url)),
    configFile: false, server: { middlewareMode: true, hmr: false }, appType: 'custom',
    plugins: [{ name: 'cart-checkout-hooks', enforce: 'pre',
      resolveId(id) { if (id === 'virtual:cart-checkout-hooks') return hookModule; },
      transform(code, id) {
        if (/(?:CustomerPortal|PlaceOrder)\.jsx$/.test(id)) {
          return code.replace("import { useState } from 'react';",
            "import { useState } from 'virtual:cart-checkout-hooks';");
        }
        return code;
      },
      load(id) {
        if (id !== hookModule) return;
        return `let current;
          export function render(Component, props, slots) {
            current = { slots, index: 0 };
            try { return Component(props); } finally { current = null; }
          }
          export function useState(initial) {
            const { slots } = current, index = current.index++;
            if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial;
            return [slots[index], value => { slots[index] = typeof value === 'function'
              ? value(slots[index]) : value; }];
          }`;
      },
    }, react()] });
  t.after(() => server.close());
  return {
    hooks: await server.ssrLoadModule('virtual:cart-checkout-hooks'),
    CustomerPortal: (await server.ssrLoadModule('/src/CustomerPortal.jsx')).default,
    CustomerCatalog: (await server.ssrLoadModule('/src/CustomerCatalog.jsx')).default,
    PlaceOrder: (await server.ssrLoadModule('/src/PlaceOrder.jsx')).default,
  };
}

test('customer portal retains a multi-product cart and exposes quantity/remove callbacks', async (t) => {
  const { hooks, CustomerPortal, CustomerCatalog, PlaceOrder } = await componentHarness(t);
  const slots = [];
  const portal = () => hooks.render(CustomerPortal, { api: {}, identity: {} }, slots);
  const catalog = find(portal(), (element) => element.type === CustomerCatalog);
  catalog.props.onAddToCart(product(), 2);
  catalog.props.onAddToCart(product({ id: '00000000-0000-4000-8000-000000000011',
    sku: 'DOC-1', name: 'Dock', stock_quantity: 10 }), 3);
  find(portal(), (element) => element.type === 'button'
    && [...elements(element.props.children)].some((child) => child.type === 'span'
      && child.props.className === 'portal-cart-count')).props.onClick();
  let checkout = find(portal(), (element) => element.type === PlaceOrder);
  assert.deepEqual(checkout.props.items.map(({ sku, quantity }) => ({ sku, quantity })), [
    { sku: 'LAP-1', quantity: 2 }, { sku: 'DOC-1', quantity: 3 },
  ]);
  checkout.props.onUpdateQuantity('LAP-1', 4);
  checkout.props.onRemove('DOC-1');
  checkout = find(portal(), (element) => element.type === PlaceOrder);
  assert.deepEqual(checkout.props.items.map(({ sku, quantity }) => ({ sku, quantity })), [
    { sku: 'LAP-1', quantity: 4 },
  ]);
});

test('checkout submits all items once, blocks duplicates, and retains a success summary', async (t) => {
  const { hooks, PlaceOrder } = await componentHarness(t);
  let calls = 0, resolveRequest, successes = 0;
  const api = { createMyOrder(items) {
    calls++;
    assert.deepEqual(items, [{ sku: 'LAP-1', quantity: 2 }, { sku: 'DOC-1', quantity: 1 }]);
    return new Promise((resolve) => { resolveRequest = resolve; });
  } };
  const items = [{ ...product(), quantity: 2 }, { ...product({ sku: 'DOC-1', name: 'Dock',
    unit_price: { amount: '149.99', currency: 'USD' } }), quantity: 1 }];
  const slots = [];
  const checkout = () => hooks.render(PlaceOrder, { api, items, onSuccess: () => successes++,
    onCancel() {}, onUpdateQuantity() {}, onRemove() {}, onViewOrders() {} }, slots);
  const submit = () => find(checkout(), (element) => element.type === 'form')
    .props.onSubmit({ preventDefault() {} });
  const pending = submit();
  await submit();
  assert.equal(calls, 1);
  assert.equal(find(checkout(), (element) => element.props.type === 'submit').props.disabled, true);
  resolveRequest({ id: '00000000-0000-4000-8000-000000000020', customer_id: 'customer',
    items: [{ sku: 'LAP-1', name: 'Laptop', quantity: 2 }, { sku: 'DOC-1', name: 'Dock', quantity: 1 }] });
  await pending;
  const html = renderToStaticMarkup(checkout());
  assert.equal(calls, 1);
  assert.equal(successes, 1);
  assert.match(html, /Order placed/);
  assert.match(html, /\$2,749\.97/);
  assert.doesNotMatch(html, /<form/);
});

test('checkout allows retry for a definitive stock error but locks an uncertain POST result', async (t) => {
  const { hooks, PlaceOrder } = await componentHarness(t);
  const items = [{ ...product(), quantity: 2 }];
  const definitiveSlots = [];
  let definitiveCalls = 0;
  const definitive = () => hooks.render(PlaceOrder, { items, onSuccess() {}, onCancel() {},
    onUpdateQuantity() {}, onRemove() {}, onViewOrders() {}, api: { async createMyOrder() {
      definitiveCalls++;
      const error = new Error('Requested quantity exceeds available stock');
      error.status = 409;
      throw error;
    } } }, definitiveSlots);
  await find(definitive(), (element) => element.type === 'form').props.onSubmit({ preventDefault() {} });
  assert.match(renderToStaticMarkup(definitive()), /Requested quantity exceeds available stock/);
  assert.equal(find(definitive(), (element) => element.props.type === 'submit').props.disabled, false);

  const uncertainSlots = [];
  let uncertainCalls = 0;
  const uncertain = () => hooks.render(PlaceOrder, { items, onSuccess() {}, onCancel() {},
    onUpdateQuantity() {}, onRemove() {}, onViewOrders() {}, api: { async createMyOrder() {
      uncertainCalls++;
      throw new Error('Network connection lost');
    } } }, uncertainSlots);
  const uncertainSubmit = () => find(uncertain(), (element) => element.type === 'form')
    .props.onSubmit({ preventDefault() {} });
  await uncertainSubmit();
  await uncertainSubmit();
  const html = renderToStaticMarkup(uncertain());
  assert.equal(uncertainCalls, 1);
  assert.match(html, /result could not be confirmed/);
  assert.match(html, /Check My Orders/);
  assert.equal(find(uncertain(), (element) => element.props.type === 'submit').props.disabled, true);
});

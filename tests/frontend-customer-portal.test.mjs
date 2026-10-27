/**
 * Phase 17B.2 — Customer Portal focused tests.
 *
 * Coverage matrix
 * ---------------
 * Catalog API client
 *   ✓ listCatalog — returns validated product list
 *   ✓ listCatalog available_only=true — passes correct query param
 *   ✓ listCatalog — available field is a boolean, not coerced
 *   ✓ listCatalog — rejects invalid response (not array)
 *   ✓ listCatalog — rejects product missing required fields
 *   ✓ listCatalog — rejects product with non-string amount
 *   ✓ listCatalog — unauthenticated 401 propagated
 *   ✓ getCatalogProduct — returns single product by id
 *   ✓ getCatalogProduct — rejects malformed UUID input before network
 *
 * Order creation (createMyOrder)
 *   ✓ createMyOrder — POST body contains only {items:[{sku,quantity}]}
 *   ✓ createMyOrder — customer_id is NOT present in the request body
 *   ✓ createMyOrder — quantity limits enforced client-side (0, 1001, non-integer)
 *   ✓ createMyOrder — empty items array rejected before network
 *   ✓ createMyOrder — over-100-items array rejected before network
 *   ✓ createMyOrder — server 422 propagated correctly
 *   ✓ createMyOrder — server 404 (unknown SKU) propagated
 *   ✓ createMyOrder — returns validated order (id, customer_id, items)
 *   ✓ createMyOrder — single submission: a second call with same data is a
 *                     separate network request (guard tested at component level;
 *                     here we confirm the API itself makes exactly one request)
 *
 * My Orders (listMyOrders)
 *   ✓ listMyOrders — returns list of orders for authenticated customer
 *   ✓ listMyOrders — uses authenticated GET /my/orders
 *   ✓ listMyOrders — empty list returned as empty array
 *   ✓ listMyOrders — rejects invalid response (not array)
 *   ✓ listMyOrders — rejects order missing customer_id
 *   ✓ listMyOrders — unauthenticated 401 propagated
 *
 * Authentication / request shape
 *   ✓ all customer API calls use Bearer token
 *   ✓ all customer API calls use no-store cache + omit credentials
 *   ✓ all customer API calls use correct HTTP methods (GET/POST)
 *   ✓ token is never embedded in any URL
 *
 * Internal controls not exposed to customers
 *   ✓ api.js exports no run-start, run-snapshot, pending-reviews, or
 *     decide-review methods callable from customer code paths
 *     (verified by confirming customer flow never hits those URLs)
 *
 * Disposal / session lifecycle
 *   ✓ disposed api rejects customer API calls
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi } from '../frontend/src/api.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const uuid = (n = 1) =>
  `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;

/** Build a well-formed Product fixture. */
function product(overrides = {}) {
  return {
    id: uuid(10),
    sku: 'LAP-1',
    name: 'ProBook Laptop 15',
    description: '15-inch business laptop.',
    available: true,
    category: 'laptops',
    image_path: '/products/laptops.svg',
    stock_quantity: 5,
    unit_price: { amount: '1299.99', currency: 'USD' },
    ...overrides,
  };
}

/** Build a well-formed Order fixture. */
function order(overrides = {}) {
  return {
    id: uuid(20),
    customer_id: uuid(21),
    items: [{ sku: 'LAP-1', name: 'ProBook Laptop 15', quantity: 1 }],
    ...overrides,
  };
}

/** createApi with a single-response mock fetcher. */
function client(payload, { status = 200, token = 'customer-test-token-xxxx' } = {}) {
  return createApi(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => (status >= 200 && status < 300
      ? payload
      : { detail: typeof payload === 'string' ? payload : 'error' }),
  }), { token });
}

/** createApi that records every (url, options) call made. */
function recordingClient(responses, { token = 'customer-test-token-xxxx' } = {}) {
  const calls = [];
  let index = 0;
  const api = createApi(async (url, options) => {
    calls.push({ url, options });
    const resp = responses[index++] ?? responses.at(-1);
    const ok = resp.status === undefined || resp.status < 400;
    return {
      ok,
      status: resp.status ?? 200,
      json: async () => (ok ? resp.body : { detail: resp.body ?? 'error' }),
    };
  }, { token });
  return { api, calls };
}

// ---------------------------------------------------------------------------
// listCatalog
// ---------------------------------------------------------------------------

test('listCatalog returns a validated product array', async () => {
  const products = [product(), product({ id: uuid(11), sku: 'ACC-1', name: 'Dock',
    available: false, stock_quantity: 0 })];
  const result = await client(products).listCatalog();
  assert.equal(result.length, 2);
  assert.equal(result[0].sku, 'LAP-1');
  assert.equal(result[1].available, false);
  // Projection: only expected fields present
  assert.deepEqual(Object.keys(result[0]).sort(),
    ['available', 'category', 'description', 'id', 'image_path', 'name', 'sku',
      'stock_quantity', 'unit_price'].sort());
  assert.deepEqual(Object.keys(result[0].unit_price).sort(), ['amount', 'currency'].sort());
});

test('listCatalog without availableOnly sends no query param', async () => {
  const { api, calls } = recordingClient([{ body: [product()] }]);
  await api.listCatalog();
  assert.equal(calls[0].url, '/api/catalog');
});

test('listCatalog with availableOnly=true appends correct query param', async () => {
  const { api, calls } = recordingClient([{ body: [product()] }]);
  await api.listCatalog({ availableOnly: true });
  assert.equal(calls[0].url, '/api/catalog?available_only=true');
});

test('listCatalog available field must be a boolean', async () => {
  // 'true' as a string should be rejected (the schema requires typeof === boolean)
  await assert.rejects(
    client([product({ available: 'true' })]).listCatalog(),
    /catalog product/,
  );
});

test('listCatalog validates category, local image path, stock, and derived availability', async () => {
  await assert.rejects(client([product({ category: 'unknown' })]).listCatalog(), /catalog product/);
  await assert.rejects(client([product({ image_path: 'https://example.com/product.jpg' })]).listCatalog(), /catalog product/);
  await assert.rejects(client([product({ stock_quantity: -1 })]).listCatalog(), /catalog product/);
  await assert.rejects(client([product({ stock_quantity: 0, available: true })]).listCatalog(), /catalog product/);
  const unavailable = await client([product({ stock_quantity: 0, available: false })]).listCatalog();
  assert.equal(unavailable[0].stock_quantity, 0);
});

test('listCatalog rejects non-array response', async () => {
  await assert.rejects(client({ items: [] }).listCatalog(), /catalog list/);
  await assert.rejects(client(null).listCatalog(), /catalog list/);
});

test('listCatalog rejects product missing required fields', async () => {
  // missing sku
  await assert.rejects(
    client([product({ sku: undefined })]).listCatalog(),
    /catalog product/,
  );
  // missing unit_price
  await assert.rejects(
    client([product({ unit_price: undefined })]).listCatalog(),
    /catalog product/,
  );
  // non-uuid id
  await assert.rejects(
    client([product({ id: 'not-a-uuid' })]).listCatalog(),
    /catalog product/,
  );
});

test('listCatalog rejects product with non-string unit_price amount', async () => {
  await assert.rejects(
    client([product({ unit_price: { amount: 1299.99, currency: 'USD' } })]).listCatalog(),
    /catalog product/,
  );
});

test('listCatalog propagates 401 without retry', async () => {
  let calls = 0;
  let expired = 0;
  const api = createApi(
    async () => { calls++; return { ok: false, status: 401, json: async () => ({ detail: 'UNAUTHENTICATED' }) }; },
    { onUnauthorized: () => expired++ },
  );
  await assert.rejects(api.listCatalog(), { status: 401 });
  assert.equal(calls, 1);
  assert.equal(expired, 1);
});

test('listCatalog returns empty array for empty catalog', async () => {
  const result = await client([]).listCatalog();
  assert.deepEqual(result, []);
});

// ---------------------------------------------------------------------------
// getCatalogProduct
// ---------------------------------------------------------------------------

test('getCatalogProduct returns a single validated product', async () => {
  const p = product();
  const result = await client(p).getCatalogProduct(p.id);
  assert.equal(result.id, p.id);
  assert.equal(result.sku, 'LAP-1');
});

test('getCatalogProduct rejects malformed UUID before making a network request', async () => {
  let calls = 0;
  const api = createApi(async () => { calls++; throw new Error('unexpected request'); });
  await assert.rejects(api.getCatalogProduct('not-a-uuid'), /product id/);
  assert.equal(calls, 0);
});

test('getCatalogProduct propagates 404', async () => {
  await assert.rejects(
    client('Product not found', { status: 404 }).getCatalogProduct(uuid(99)),
    { status: 404 },
  );
});

// ---------------------------------------------------------------------------
// createMyOrder — request shape and security
// ---------------------------------------------------------------------------

test('createMyOrder POST body contains only items array — no customer_id', async () => {
  const { api, calls } = recordingClient([{ body: order() }]);
  await api.createMyOrder([{ sku: 'LAP-1', quantity: 2 }]);

  assert.equal(calls.length, 1);
  const { url, options } = calls[0];
  assert.equal(url, '/api/my/orders');
  assert.equal(options.method, 'POST');

  const body = JSON.parse(options.body);

  // The body must have exactly one key: items
  assert.deepEqual(Object.keys(body), ['items']);
  // customer_id must be absent
  assert.equal('customer_id' in body, false,
    'customer_id must NEVER be sent in the order creation request body');
  // items structure
  assert.equal(body.items.length, 1);
  assert.equal(body.items[0].sku, 'LAP-1');
  assert.equal(body.items[0].quantity, 2);
  // name must NOT be sent (server resolves it)
  assert.equal('name' in body.items[0], false);
});

test('createMyOrder — extra fields beyond sku/quantity are stripped from items', async () => {
  const { api, calls } = recordingClient([{ body: order() }]);
  // Pass an item with an extra field that should be stripped
  await api.createMyOrder([{ sku: 'ACC-1', quantity: 1, customer_id: 'INJECTED', name: 'INJECTED' }]);
  const body = JSON.parse(calls[0].options.body);
  assert.equal('customer_id' in body.items[0], false);
  assert.equal('name' in body.items[0], false);
  assert.deepEqual(Object.keys(body.items[0]).sort(), ['quantity', 'sku'].sort());
});

test('createMyOrder uses authenticated Bearer token', async () => {
  const { api, calls } = recordingClient([{ body: order() }], { token: 'my-customer-token-1234' });
  await api.createMyOrder([{ sku: 'LAP-1', quantity: 1 }]);
  assert.equal(calls[0].options.headers.Authorization, 'Bearer my-customer-token-1234');
});

test('createMyOrder uses no-store cache and omit credentials', async () => {
  const { api, calls } = recordingClient([{ body: order() }]);
  await api.createMyOrder([{ sku: 'LAP-1', quantity: 1 }]);
  assert.equal(calls[0].options.cache, 'no-store');
  assert.equal(calls[0].options.credentials, 'omit');
  assert.equal(calls[0].options.redirect, 'error');
});

test('createMyOrder token never appears in any URL', async () => {
  const secretToken = 'super-secret-token-xyz';
  const { api, calls } = recordingClient([{ body: order() }], { token: secretToken });
  await api.createMyOrder([{ sku: 'LAP-1', quantity: 1 }]);
  assert.equal(calls[0].url.includes(secretToken), false,
    'Token must never be embedded in the URL');
});

// ---------------------------------------------------------------------------
// createMyOrder — client-side validation (no network hit)
// ---------------------------------------------------------------------------

test('createMyOrder rejects quantity = 0 before sending a request', async () => {
  let calls = 0;
  const api = createApi(async () => { calls++; throw new Error('unexpected'); });
  await assert.rejects(
    api.createMyOrder([{ sku: 'LAP-1', quantity: 0 }]),
    /order item/,
  );
  assert.equal(calls, 0);
});

test('createMyOrder rejects quantity > 1000 before sending a request', async () => {
  let calls = 0;
  const api = createApi(async () => { calls++; throw new Error('unexpected'); });
  await assert.rejects(
    api.createMyOrder([{ sku: 'LAP-1', quantity: 1001 }]),
    /order item/,
  );
  assert.equal(calls, 0);
});

test('createMyOrder rejects non-integer quantity before sending a request', async () => {
  let calls = 0;
  const api = createApi(async () => { calls++; throw new Error('unexpected'); });
  await assert.rejects(
    api.createMyOrder([{ sku: 'LAP-1', quantity: 1.5 }]),
    /order item/,
  );
  assert.equal(calls, 0);
});

test('createMyOrder rejects empty items array before sending a request', async () => {
  let calls = 0;
  const api = createApi(async () => { calls++; throw new Error('unexpected'); });
  await assert.rejects(api.createMyOrder([]), /order items/);
  assert.equal(calls, 0);
});

test('createMyOrder rejects items array over 100 before sending a request', async () => {
  let calls = 0;
  const api = createApi(async () => { calls++; throw new Error('unexpected'); });
  const tooMany = Array.from({ length: 101 }, (_, i) => ({ sku: `SKU-${i}`, quantity: 1 }));
  await assert.rejects(api.createMyOrder(tooMany), /order items/);
  assert.equal(calls, 0);
});

// ---------------------------------------------------------------------------
// createMyOrder — server error propagation
// ---------------------------------------------------------------------------

test('createMyOrder propagates 422 from server (unknown SKU)', async () => {
  await assert.rejects(
    client([{ msg: 'SKU not found' }], { status: 422 }).createMyOrder([{ sku: 'BAD-SKU', quantity: 1 }]),
    { status: 422 },
  );
});

test('createMyOrder propagates 404 from server (customer not found)', async () => {
  await assert.rejects(
    client('Customer not found', { status: 404 }).createMyOrder([{ sku: 'LAP-1', quantity: 1 }]),
    { status: 404 },
  );
});

test('createMyOrder makes exactly one network request per call', async () => {
  const { api, calls } = recordingClient([{ body: order() }]);
  await api.createMyOrder([{ sku: 'LAP-1', quantity: 1 }]);
  assert.equal(calls.length, 1);
});

// ---------------------------------------------------------------------------
// createMyOrder — response validation
// ---------------------------------------------------------------------------

test('createMyOrder returns validated order object', async () => {
  const created = order({ items: [{ sku: 'LAP-1', name: 'ProBook Laptop 15', quantity: 3 }] });
  const result = await client(created).createMyOrder([{ sku: 'LAP-1', quantity: 3 }]);
  assert.ok(result.id.match(/^[0-9a-f-]{36}$/i));
  assert.ok(result.customer_id.match(/^[0-9a-f-]{36}$/i));
  assert.equal(result.items[0].sku, 'LAP-1');
  assert.equal(result.items[0].quantity, 3);
  // Projection: only known fields
  assert.ok(Array.isArray(result.items));
});

test('createMyOrder rejects response with missing order id', async () => {
  await assert.rejects(
    client(order({ id: undefined })).createMyOrder([{ sku: 'LAP-1', quantity: 1 }]),
    /created order/,
  );
});

test('createMyOrder rejects response with empty items', async () => {
  await assert.rejects(
    client(order({ items: [] })).createMyOrder([{ sku: 'LAP-1', quantity: 1 }]),
    /created order/,
  );
});

// ---------------------------------------------------------------------------
// listMyOrders
// ---------------------------------------------------------------------------

test('listMyOrders uses authenticated GET /my/orders', async () => {
  const { api, calls } = recordingClient(
    [{ body: [order()] }],
    { token: 'customer-list-token-xxxx' },
  );
  await api.listMyOrders();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/my/orders');
  assert.equal(calls[0].options.method, 'GET');
  assert.equal(calls[0].options.body, undefined);
  assert.equal(calls[0].options.headers.Authorization, 'Bearer customer-list-token-xxxx');
  assert.equal(calls[0].options.cache, 'no-store');
  assert.equal(calls[0].options.credentials, 'omit');
});

test('listMyOrders returns validated order array', async () => {
  const o1 = order({ id: uuid(20) });
  const o2 = order({ id: uuid(21) });
  const result = await client([o1, o2]).listMyOrders();
  assert.equal(result.length, 2);
  assert.equal(result[0].id, o1.id);
  assert.equal(result[1].id, o2.id);
  for (const r of result) {
    assert.ok(Array.isArray(r.items));
    assert.equal(typeof r.customer_id, 'string');
  }
});

test('listMyOrders returns empty array when customer has no orders', async () => {
  const result = await client([]).listMyOrders();
  assert.deepEqual(result, []);
});

test('listMyOrders rejects non-array response', async () => {
  await assert.rejects(client({ items: [] }).listMyOrders(), /my orders list/);
  await assert.rejects(client(null).listMyOrders(), /my orders list/);
});

test('listMyOrders rejects order missing customer_id', async () => {
  await assert.rejects(
    client([order({ customer_id: undefined })]).listMyOrders(),
    /my order/,
  );
});

test('listMyOrders rejects order with non-uuid id', async () => {
  await assert.rejects(
    client([order({ id: 'bad-id' })]).listMyOrders(),
    /my order/,
  );
});

test('listMyOrders propagates 401 and triggers onUnauthorized', async () => {
  let expired = 0;
  const api = createApi(
    async () => ({ ok: false, status: 401, json: async () => ({ detail: 'UNAUTHENTICATED' }) }),
    { onUnauthorized: () => expired++ },
  );
  await assert.rejects(api.listMyOrders(), { status: 401 });
  assert.equal(expired, 1);
});

test('listMyOrders propagates 403 for non-customer role', async () => {
  await assert.rejects(
    client('Access denied', { status: 403 }).listMyOrders(),
    { status: 403 },
  );
});

// ---------------------------------------------------------------------------
// Session disposal
// ---------------------------------------------------------------------------

test('disposed api rejects listCatalog with session-ended error', async () => {
  const api = createApi(async () => ({ ok: true, json: async () => [] }));
  api.dispose();
  await assert.rejects(api.listCatalog(), /session has ended/);
});

test('disposed api rejects createMyOrder with session-ended error', async () => {
  const api = createApi(async () => ({ ok: true, json: async () => order() }));
  api.dispose();
  await assert.rejects(
    api.createMyOrder([{ sku: 'LAP-1', quantity: 1 }]),
    /session has ended/,
  );
});

test('disposed api rejects listMyOrders with session-ended error', async () => {
  const api = createApi(async () => ({ ok: true, json: async () => [] }));
  api.dispose();
  await assert.rejects(api.listMyOrders(), /session has ended/);
});

// ---------------------------------------------------------------------------
// Internal controls not reachable via customer API surface
// ---------------------------------------------------------------------------

test('customer API calls never hit internal staff URLs', async () => {
  // Confirm that the customer-facing API methods only ever call
  // /api/catalog, /api/my/orders — never /api/runs, /api/reviews,
  // /api/cases/:id/runs, /api/runs/:id/decision, or /api/diagnostics.
  const staffPaths = ['/api/runs', '/api/reviews', '/api/diagnostics'];
  const { api, calls } = recordingClient([
    { body: [product()] },   // listCatalog
    { body: order() },       // createMyOrder
    { body: [order()] },     // listMyOrders
    { body: product() },     // getCatalogProduct
  ]);

  await api.listCatalog();
  await api.createMyOrder([{ sku: 'LAP-1', quantity: 1 }]);
  await api.listMyOrders();
  await api.getCatalogProduct(uuid(10));

  for (const { url } of calls) {
    for (const staffPath of staffPaths) {
      assert.equal(url.startsWith(staffPath), false,
        `Customer API call must not hit staff path ${staffPath}, got: ${url}`);
    }
  }

  // Confirm all actual paths used
  assert.deepEqual(calls.map((c) => c.url), [
    '/api/catalog',
    '/api/my/orders',
    '/api/my/orders',
    `/api/catalog/${uuid(10)}`,
  ]);
});

// ---------------------------------------------------------------------------
// Availability semantics documented
// ---------------------------------------------------------------------------

test('listCatalog distinguishes available and unavailable products correctly', async () => {
  const available = product({ available: true });
  const unavailable = product({ id: uuid(11), sku: 'LAP-2', available: false, stock_quantity: 0 });
  const result = await client([available, unavailable]).listCatalog();
  const avail = result.filter((p) => p.available);
  const unavail = result.filter((p) => !p.available);
  assert.equal(avail.length, 1);
  assert.equal(avail[0].sku, 'LAP-1');
  assert.equal(unavail.length, 1);
  assert.equal(unavail[0].sku, 'LAP-2');
});

// ===========================================================================
// Phase 17B.3 — createMyCase() API client tests
// ===========================================================================

const caseUuid = (n = 1) => `cccccccc-0000-4000-8000-${String(n).padStart(12, '0')}`;

/** Build a well-formed SupportCase fixture. */
function supportCase(overrides = {}) {
  return {
    id: caseUuid(1),
    customer_id: uuid(21),
    order_id: uuid(20),
    subject: 'Item arrived damaged',
    description: 'The item was visibly damaged when I opened the package.',
    status: 'open',
    created_at: '2026-10-05T10:00:00Z',
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// createMyCase — request shape and security
// ---------------------------------------------------------------------------

test('createMyCase sends POST to /api/my/cases', async () => {
  const { api, calls } = recordingClient([{ body: supportCase() }]);
  await api.createMyCase({ orderId: uuid(20), subject: 'Test', description: 'A description here.' });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/my/cases');
  assert.equal(calls[0].options.method, 'POST');
});

test('createMyCase body contains order_id, subject, description — nothing else', async () => {
  const orderId = uuid(20);
  const { api, calls } = recordingClient([{ body: supportCase() }]);
  await api.createMyCase({ orderId, subject: 'My subject', description: 'My description.' });
  const body = JSON.parse(calls[0].options.body);
  assert.deepEqual(Object.keys(body).sort(), ['description', 'order_id', 'subject'].sort());
  assert.equal(body.order_id, orderId);
  assert.equal(body.subject, 'My subject');
  assert.equal(body.description, 'My description.');
});

test('createMyCase body does NOT contain customer_id', async () => {
  const { api, calls } = recordingClient([{ body: supportCase() }]);
  await api.createMyCase({ orderId: uuid(20), subject: 'Sub', description: 'Desc ok.' });
  const body = JSON.parse(calls[0].options.body);
  assert.equal('customer_id' in body, false,
    'customer_id must NEVER be sent by the frontend in createMyCase');
});

test('createMyCase does not send user_id, role, status, or case id', async () => {
  const { api, calls } = recordingClient([{ body: supportCase() }]);
  await api.createMyCase({ orderId: uuid(20), subject: 'Sub', description: 'Desc ok.' });
  const body = JSON.parse(calls[0].options.body);
  for (const forbidden of ['user_id', 'role', 'status', 'id']) {
    assert.equal(forbidden in body, false, `'${forbidden}' must not be in createMyCase body`);
  }
});

test('createMyCase uses authenticated Bearer token', async () => {
  const { api, calls } = recordingClient([{ body: supportCase() }], { token: 'intake-test-token-xyz' });
  await api.createMyCase({ orderId: uuid(20), subject: 'Sub', description: 'Desc.' });
  assert.equal(calls[0].options.headers.Authorization, 'Bearer intake-test-token-xyz');
});

test('createMyCase uses no-store cache and omit credentials', async () => {
  const { api, calls } = recordingClient([{ body: supportCase() }]);
  await api.createMyCase({ orderId: uuid(20), subject: 'Sub', description: 'Desc.' });
  assert.equal(calls[0].options.cache, 'no-store');
  assert.equal(calls[0].options.credentials, 'omit');
  assert.equal(calls[0].options.redirect, 'error');
});

test('createMyCase token never appears in the URL', async () => {
  const secret = 'super-secret-intake-token';
  const { api, calls } = recordingClient([{ body: supportCase() }], { token: secret });
  await api.createMyCase({ orderId: uuid(20), subject: 'Sub', description: 'Desc.' });
  assert.equal(calls[0].url.includes(secret), false);
});

// ---------------------------------------------------------------------------
// createMyCase — client-side validation (no network hit)
// ---------------------------------------------------------------------------

test('createMyCase rejects non-UUID order_id before sending a request', async () => {
  let calls = 0;
  const api = createApi(async () => { calls++; throw new Error('unexpected'); });
  await assert.rejects(
    api.createMyCase({ orderId: 'not-a-uuid', subject: 'Sub', description: 'Desc.' }),
    /case order_id/,
  );
  assert.equal(calls, 0);
});

test('createMyCase rejects empty subject before sending a request', async () => {
  let calls = 0;
  const api = createApi(async () => { calls++; throw new Error('unexpected'); });
  await assert.rejects(
    api.createMyCase({ orderId: uuid(20), subject: '', description: 'Desc.' }),
    /case subject/,
  );
  assert.equal(calls, 0);
});

test('createMyCase rejects whitespace-only subject before sending a request', async () => {
  let calls = 0;
  const api = createApi(async () => { calls++; throw new Error('unexpected'); });
  await assert.rejects(
    api.createMyCase({ orderId: uuid(20), subject: '   ', description: 'Desc.' }),
    /case subject/,
  );
  assert.equal(calls, 0);
});

test('createMyCase rejects subject over 200 chars before sending a request', async () => {
  let calls = 0;
  const api = createApi(async () => { calls++; throw new Error('unexpected'); });
  await assert.rejects(
    api.createMyCase({ orderId: uuid(20), subject: 'x'.repeat(201), description: 'Desc.' }),
    /case subject/,
  );
  assert.equal(calls, 0);
});

test('createMyCase accepts subject of exactly 200 chars', async () => {
  const { api } = recordingClient([{ body: supportCase() }]);
  await api.createMyCase({ orderId: uuid(20), subject: 'x'.repeat(200), description: 'Desc ok.' });
  // no throw expected
});

test('createMyCase rejects empty description before sending a request', async () => {
  let calls = 0;
  const api = createApi(async () => { calls++; throw new Error('unexpected'); });
  await assert.rejects(
    api.createMyCase({ orderId: uuid(20), subject: 'Sub', description: '' }),
    /case description/,
  );
  assert.equal(calls, 0);
});

test('createMyCase rejects description over 4000 chars before sending a request', async () => {
  let calls = 0;
  const api = createApi(async () => { calls++; throw new Error('unexpected'); });
  await assert.rejects(
    api.createMyCase({ orderId: uuid(20), subject: 'Sub', description: 'x'.repeat(4001) }),
    /case description/,
  );
  assert.equal(calls, 0);
});

test('createMyCase accepts description of exactly 4000 chars', async () => {
  const { api } = recordingClient([{ body: supportCase() }]);
  await api.createMyCase({ orderId: uuid(20), subject: 'Sub', description: 'x'.repeat(4000) });
  // no throw expected
});

// ---------------------------------------------------------------------------
// createMyCase — server error propagation
// ---------------------------------------------------------------------------

test('createMyCase propagates 403 (cross-customer order)', async () => {
  await assert.rejects(
    client('Access denied', { status: 403 }).createMyCase({
      orderId: uuid(20), subject: 'Sub', description: 'Desc.',
    }),
    { status: 403 },
  );
});

test('createMyCase propagates 422 from server (invalid input)', async () => {
  await assert.rejects(
    client([{ msg: 'value too long' }], { status: 422 }).createMyCase({
      orderId: uuid(20), subject: 'Sub', description: 'Desc.',
    }),
    { status: 422 },
  );
});

test('createMyCase propagates 401 and triggers onUnauthorized', async () => {
  let expired = 0;
  const api = createApi(
    async () => ({ ok: false, status: 401, json: async () => ({ detail: 'UNAUTHENTICATED' }) }),
    { onUnauthorized: () => expired++ },
  );
  await assert.rejects(
    api.createMyCase({ orderId: uuid(20), subject: 'Sub', description: 'Desc.' }),
    { status: 401 },
  );
  assert.equal(expired, 1);
});

// ---------------------------------------------------------------------------
// createMyCase — response validation
// ---------------------------------------------------------------------------

test('createMyCase returns validated case object', async () => {
  const sc = supportCase();
  const result = await client(sc).createMyCase({
    orderId: uuid(20), subject: 'Sub', description: 'Desc.',
  });
  assert.ok(result.id.match(/^[0-9a-f-]{36}$/i));
  assert.ok(result.customer_id.match(/^[0-9a-f-]{36}$/i));
  assert.equal(result.status, 'open');
  assert.equal(typeof result.subject, 'string');
  assert.equal(typeof result.created_at, 'string');
});

test('createMyCase rejects response missing case id', async () => {
  await assert.rejects(
    client(supportCase({ id: undefined })).createMyCase({
      orderId: uuid(20), subject: 'Sub', description: 'Desc.',
    }),
    /created case/,
  );
});

test('createMyCase rejects response missing customer_id', async () => {
  await assert.rejects(
    client(supportCase({ customer_id: undefined })).createMyCase({
      orderId: uuid(20), subject: 'Sub', description: 'Desc.',
    }),
    /created case/,
  );
});

// ---------------------------------------------------------------------------
// Disposed session
// ---------------------------------------------------------------------------

test('disposed api rejects createMyCase with session-ended error', async () => {
  const api = createApi(async () => ({ ok: true, json: async () => supportCase() }));
  api.dispose();
  await assert.rejects(
    api.createMyCase({ orderId: uuid(20), subject: 'Sub', description: 'Desc.' }),
    /session has ended/,
  );
});

// ---------------------------------------------------------------------------
// createMyCase never hits internal staff URLs
// ---------------------------------------------------------------------------

test('createMyCase only hits /api/my/cases — never any internal staff URL', async () => {
  const staffPaths = ['/api/runs', '/api/reviews', '/api/diagnostics', '/api/cases'];
  const { api, calls } = recordingClient([{ body: supportCase() }]);
  await api.createMyCase({ orderId: uuid(20), subject: 'Sub', description: 'Desc.' });
  assert.equal(calls[0].url, '/api/my/cases');
  for (const staffPath of staffPaths) {
    assert.equal(calls[0].url.startsWith(staffPath), false,
      `createMyCase must not hit staff path ${staffPath}`);
  }
});

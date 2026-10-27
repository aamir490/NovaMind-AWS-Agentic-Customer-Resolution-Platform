import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { renderToStaticMarkup } = require('react-dom/server');

// Exercise the two real components' callbacks with retained useState slots.
// This small hook harness needs no DOM/test-renderer dependency; it does not
// simulate browser events, effects, or React's scheduling.
const hookModule = '\0intake-navigation-state';
function* elements(node) {
  if (Array.isArray(node)) { for (const child of node) yield* elements(child); }
  else if (node && typeof node === 'object' && node.props) {
    yield node;
    yield* elements(node.props.children);
  }
}
const find = (tree, predicate) => {
  const element = [...elements(tree)].find(predicate);
  assert.ok(element, 'Expected component or control to remain mounted');
  return element;
};

test('support intake retries failures, blocks duplicates, and stays successful until explicit Back', async (t) => {
  const { createServer } = await import(pathToFileURL(require.resolve('vite')).href);
  const { default: react } = await import(pathToFileURL(require.resolve('@vitejs/plugin-react')).href);
  const server = await createServer({ root: fileURLToPath(new URL('../frontend', import.meta.url)),
    configFile: false, server: { middlewareMode: true, hmr: false }, appType: 'custom',
    plugins: [{ name: 'intake-navigation-state', enforce: 'pre',
      resolveId(id) {
        if (id === 'virtual:intake-navigation-state') return hookModule;
      },
      transform(code, id) {
        if (/(?:CustomerPortal|ReportProblem)\.jsx$/.test(id)) {
          return code
            .replace("import { useState } from 'react';", "import { useState } from 'virtual:intake-navigation-state';")
            .replace("import { useRef, useState } from 'react';", "import { useRef, useState } from 'virtual:intake-navigation-state';");
        }
      },
      load(id) {
        if (id !== hookModule) return;
        return `let current;
          export function render(Component, props, slots) {
            current = { slots, index: 0 };
            try { return Component(props); } finally { current = null; }
          }
          export function useState(initial) {
            const { slots } = current;
            const index = current.index++;
            if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial;
            return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }];
          }
          export function useRef(initial) {
            const { slots } = current;
            const index = current.index++;
            if (!(index in slots)) slots[index] = { current: initial };
            return slots[index];
          }`;
      },
    }, react()] });
  t.after(() => server.close());
  const { render } = await server.ssrLoadModule('virtual:intake-navigation-state');
  const { default: CustomerPortal } = await server.ssrLoadModule('/src/CustomerPortal.jsx');
  const { default: ReportProblem } = await server.ssrLoadModule('/src/ReportProblem.jsx');
  const { default: MyOrders } = await server.ssrLoadModule('/src/MyOrders.jsx');
  const { default: MySupportRequests } = await server.ssrLoadModule('/src/MySupportRequests.jsx');
  const order = { id: '00000000-0000-4000-8000-000000000020', items: [{ sku: 'LAP-1', name: 'Laptop', quantity: 1 }] };
  const created = { id: '00000000-0000-4000-8000-000000000030', subject: 'Wrong item', status: 'open' };
  let calls = 0, resolveRequest, rejectRequest, firstIdempotencyKey;
  const api = { createMyCase(body) {
    calls++;
    assert.equal(body.orderId, order.id);
    assert.equal(body.subject, 'Wrong item');
    assert.equal(body.description, 'The item does not match my order.');
    assert.match(body.idempotencyKey, /^[0-9a-f-]{36}$/i);
    if (firstIdempotencyKey === undefined) firstIdempotencyKey = body.idempotencyKey;
    else assert.equal(body.idempotencyKey, firstIdempotencyKey, 'retry must reuse its idempotency key');
    return new Promise((resolve, reject) => { resolveRequest = resolve; rejectRequest = reject; });
  } };
  const portalSlots = [], intakeSlots = [];
  const portal = () => render(CustomerPortal, { api }, portalSlots);
  const intake = () => render(ReportProblem, find(portal(), (el) => el.type === ReportProblem).props, intakeSlots);
  find(portal(), (el) => el.type === 'button' && el.props.children.includes('My Orders')).props.onClick();
  find(portal(), (el) => el.type === MyOrders).props.onReportProblem(order);
  find(intake(), (el) => el.props.id === 'rp-subject').props.onChange({ target: { value: 'Wrong item' } });
  find(intake(), (el) => el.props.id === 'rp-description').props.onChange({ target: { value: 'The item does not match my order.' } });
  const submit = () => find(intake(), (el) => el.type === 'form').props.onSubmit({ preventDefault() {} });
  const failed = submit();
  assert.equal(find(intake(), (el) => el.props.type === 'submit').props.disabled, true);
  await submit();
  assert.equal(calls, 1);
  rejectRequest(new Error('Please retry'));
  await failed;
  assert.match(renderToStaticMarkup(intake()), /Please retry/);
  assert.equal(find(intake(), (el) => el.props.type === 'submit').props.disabled, false);
  const succeeded = submit();
  await submit();
  assert.equal(calls, 2);
  resolveRequest(created);
  await succeeded;
  for (let i = 0; i < 2; i++) {
    const html = renderToStaticMarkup(intake());
    assert.match(html, /Support request created/);
    assert.ok(html.includes(created.id));
    assert.match(html, />OPEN</);
    assert.doesNotMatch(html, /<form|Submit support request/);
  }
  assert.equal(calls, 2);
  find(intake(), (el) => el.type === 'button' && el.props.children === 'Back to my orders').props.onClick();
  find(portal(), (el) => el.type === MyOrders);
  assert.equal([...elements(portal())].some((el) => el.type === ReportProblem), false);
  find(portal(), (el) => el.type === 'button' && el.props.children === 'My Support Requests').props.onClick();
  find(portal(), (el) => el.type === MySupportRequests);
  assert.equal(calls, 2, 'Opening support history must not resubmit intake');
});

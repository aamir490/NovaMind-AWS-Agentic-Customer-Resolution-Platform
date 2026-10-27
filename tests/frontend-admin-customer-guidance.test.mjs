import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { renderToStaticMarkup } = require('react-dom/server');

let server, hooks, AdminCustomerGuidance;
before(async () => {
  const { createServer } = await import(pathToFileURL(require.resolve('vite')).href);
  const { default: react } = await import(pathToFileURL(require.resolve('@vitejs/plugin-react')).href);
  server = await createServer({ root: fileURLToPath(new URL('../frontend', import.meta.url)),
    configFile: false, server: { middlewareMode: true, hmr: false }, appType: 'custom',
    plugins: [{ name: 'admin-guidance-hooks', enforce: 'pre',
      resolveId(id) { if (id === 'virtual:admin-guidance-hooks') return '\0admin-guidance-hooks'; },
      transform(code, id) {
        if (/CaseReview\.jsx$/.test(id)) {
          return code.replace("import { useEffect, useRef, useState } from 'react';",
            "import { useEffect, useRef, useState } from 'virtual:admin-guidance-hooks';");
        }
      },
      load(id) {
        if (id !== '\0admin-guidance-hooks') return;
        return `let current;
          export function render(Component, props, slots) {
            current = { slots, index: 0, effects: [] };
            let result, effects;
            try { result = Component(props); effects = current.effects; }
            finally { current = null; }
            effects.forEach(effect => effect());
            return result;
          }
          export function useState(initial) {
            const { slots } = current, index = current.index++;
            if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial;
            return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }];
          }
          export function useRef(initial) { return useState(() => ({ current: initial }))[0]; }
          export function useEffect(effect, deps) {
            const { slots } = current, index = current.index++, old = slots[index];
            if (!old || deps.some((dep, i) => !Object.is(dep, old.deps[i]))) {
              current.effects.push(() => { old?.cleanup?.(); slots[index] = { deps, cleanup: effect() }; });
            }
          }`;
      },
    }, react()] });
  hooks = await server.ssrLoadModule('virtual:admin-guidance-hooks');
  ({ AdminCustomerGuidance } = await server.ssrLoadModule('/src/CaseReview.jsx'));
});
after(async () => { await server?.close(); });

function* elements(node) {
  if (Array.isArray(node)) { for (const child of node) yield* elements(child); }
  else if (node?.props) { yield node; yield* elements(node.props.children); }
}

function find(tree, predicate) {
  const result = [...elements(tree)].find(predicate);
  assert.ok(result, 'Expected element to be present');
  return result;
}

test('ADMIN explicitly reviews, edits, and sends guidance without changing case state', async () => {
  const caseId = '00000000-0000-4000-8000-000000000001';
  const supportCase = { id: caseId, status: 'open' };
  const draft = {
    issueType: 'malfunction', recommendedDecision: 'REQUEST_EVIDENCE',
    verifiedFacts: ['The controller intermittently stops responding.'],
    missingEvidence: ['Result after testing another USB port.'],
    decisionReason: 'More diagnostic evidence is needed.',
    customerFacingResponse: 'Please test another USB port and tell us whether the issue continues.',
    troubleshootingSteps: ['Test another USB port.'],
    responseSource: 'BEDROCK_VALIDATED',
    citations: [{ documentId: 'warranty-guidance', chunkId: 'a'.repeat(64),
      title: 'Warranty guidance', version: 'v1' }],
  };
  const calls = [];
  let completed = null;
  const api = { sendCustomerGuidance: async (receivedCaseId, request) => {
    calls.push({ receivedCaseId, request });
    return { message: request.message, sentAt: '2026-10-21T10:00:00Z' };
  } };
  const slots = [];
  const render = () => hooks.render(AdminCustomerGuidance, {
    api, supportCase, draft, latest: null, onComplete: (value) => { completed = value; },
  }, slots);

  render();
  let tree = render();
  let html = renderToStaticMarkup(tree);
  assert.match(html, /Grounded AI draft/);
  assert.match(html, /Bedrock validated/);
  assert.match(html, /More diagnostic evidence is needed/);
  assert.match(html, /Approved troubleshooting steps/);
  assert.match(html, /Warranty guidance/);
  assert.match(html, /Nothing is sent automatically/);
  assert.match(html, /Send guidance/);
  assert.equal(calls.length, 0);

  const edited = 'Please test a known-good cable and another USB port, then report the indicator state.';
  find(tree, (element) => element.type === 'textarea').props.onChange({ target: { value: edited } });
  tree = render();
  await find(tree, (element) => element.type === 'form').props.onSubmit({ preventDefault() {} });
  tree = render();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].receivedCaseId, caseId);
  assert.equal(calls[0].request.message, edited);
  assert.match(calls[0].request.idempotencyKey,
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  assert.deepEqual(completed, { message: edited, sentAt: '2026-10-21T10:00:00Z' });
  assert.equal(supportCase.status, 'open');
  assert.equal(find(tree, (element) => element.type === 'textarea').props.disabled, true);
  assert.match(renderToStaticMarkup(tree), /Guidance sent/);
});

import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { renderToStaticMarkup } = require('react-dom/server');

let server, hooks, InvestigationControlPanel, AdminInvestigation;
before(async () => {
  const { createServer } = await import(pathToFileURL(require.resolve('vite')).href);
  const { default: react } = await import(pathToFileURL(require.resolve('@vitejs/plugin-react')).href);
  server = await createServer({ root: fileURLToPath(new URL('../frontend', import.meta.url)),
    configFile: false, server: { middlewareMode: true, hmr: false }, appType: 'custom',
    plugins: [{ name: 'investigation-control-hooks', enforce: 'pre',
      resolveId(id) { if (id === 'virtual:investigation-control-hooks') return '\0investigation-control-hooks'; },
      transform(code, id) {
        if (/Dashboard\.jsx$/.test(id)) {
          return code.replace("import { useCallback, useEffect, useRef, useState } from 'react';",
            "import { useCallback, useEffect, useRef, useState } from 'virtual:investigation-control-hooks';");
        }
        if (/CaseReview\.jsx$/.test(id)) {
          return code.replace("import { useEffect, useRef, useState } from 'react';",
            "import { useEffect, useRef, useState } from 'virtual:investigation-control-hooks';");
        }
      },
      load(id) {
        if (id !== '\0investigation-control-hooks') return;
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
          }
          export function useCallback(callback) { return callback; }`;
      },
    }, react()] });
  hooks = await server.ssrLoadModule('virtual:investigation-control-hooks');
  ({ InvestigationControlPanel } = await server.ssrLoadModule('/src/Dashboard.jsx'));
  ({ AdminInvestigation } = await server.ssrLoadModule('/src/CaseReview.jsx'));
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

const control = { configuredMode: 'MANUAL', effectiveMode: 'MANUAL',
  automaticCapabilityAvailable: true, version: 1, updatedBy: null,
  updatedAt: '2026-10-21T10:00:00Z', counts: { total: 6, pending: 1, running: 1,
    reviewRequired: 1, completed: 2, failed: 1, recoveryRequired: 0 } };

test('Dashboard mode control shows durable mode and investigation status without changing it', () => {
  const changes = [];
  const tree = InvestigationControlPanel({ control, isAdmin: true, phase: 'idle', error: null,
    onChange: (mode) => changes.push(mode) });
  const html = renderToStaticMarkup(tree);
  assert.match(html, /MANUAL/);
  assert.match(html, /newly submitted|ADMIN must start/);
  assert.match(html, /<strong>1<\/strong> pending/);
  assert.match(html, /<strong>2<\/strong> completed/);
  assert.equal(changes.length, 0);
  const toggle = find(tree, (element) => element.type === 'input' && element.props.role === 'switch');
  assert.equal(toggle.props.checked, false);
  assert.equal(toggle.props.disabled, false);
  toggle.props.onChange({ target: { checked: true } });
  assert.deepEqual(changes, ['AUTO']);
});

test('AUTO switch is disabled for reviewers and when server capability is absent', () => {
  for (const props of [
    { isAdmin: false, automaticCapabilityAvailable: true },
    { isAdmin: true, automaticCapabilityAvailable: false },
  ]) {
    const tree = InvestigationControlPanel({ control: { ...control,
      automaticCapabilityAvailable: props.automaticCapabilityAvailable },
    isAdmin: props.isAdmin, phase: 'idle', error: null, onChange() {} });
    const toggle = find(tree, (element) => element.type === 'input');
    assert.equal(toggle.props.disabled, true);
  }
});

test('Cases requires an explicit ADMIN start and reuses its idempotency key after failure', async () => {
  const caseId = '00000000-0000-4000-8000-000000000001';
  const supportCase = { id: caseId, status: 'open' };
  const investigation = { caseId, jobId: '00000000-0000-4000-8000-000000000002',
    state: 'PENDING', jobStatus: 'PENDING', workflowId: null, workflowStatus: null,
    provider: null, submissionMode: 'MANUAL', modeVersion: 1,
    submittedAt: '2026-10-21T10:00:00Z', executionStarted: false, canStart: true,
    actionsExecuted: false };
  const calls = [];
  const api = {
    async startCaseInvestigation(receivedCaseId, request) {
      calls.push({ receivedCaseId, request });
      if (calls.length === 1) throw new Error('Temporary local failure');
      return { ...investigation, state: 'RUNNING', canStart: false, executionStarted: true };
    },
    async caseInvestigation() { return investigation; },
  };
  let completed = null;
  const slots = [];
  const render = () => hooks.render(AdminInvestigation, { api, supportCase, investigation,
    onComplete: (value) => { completed = value; } }, slots);
  render();
  let tree = render();
  assert.match(renderToStaticMarkup(tree), /Investigate with AI/);
  assert.match(renderToStaticMarkup(tree), /no refund, return, replacement/i);
  await find(tree, (element) => element.type === 'button'
    && element.props.children === 'Investigate with AI').props.onClick();
  tree = render();
  assert.match(renderToStaticMarkup(tree), /Temporary local failure/);
  await find(tree, (element) => element.type === 'button'
    && element.props.children === 'Investigate with AI').props.onClick();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].receivedCaseId, caseId);
  assert.equal(calls[0].request.idempotencyKey, calls[1].request.idempotencyKey);
  assert.deepEqual(Object.keys(calls[0].request), ['idempotencyKey']);
  assert.equal(completed.state, 'RUNNING');
  assert.equal(supportCase.status, 'open');
});

test('Admin investigation renders the separately fetched persisted draft when snapshot fields are absent', () => {
  const supportCase = { id: '00000000-0000-4000-8000-000000000001', status: 'open' };
  const investigation = { caseId: supportCase.id, jobId: null, state: 'COMPLETED', jobStatus: 'COMPLETED',
    workflowId: '00000000-0000-4000-8000-000000000002', workflowStatus: 'COMPLETED', provider: 'bedrock',
    submissionMode: 'MANUAL', modeVersion: 1, submittedAt: '2026-10-21T10:00:00Z', executionStarted: true,
    canStart: false, actionsExecuted: false };
  const resolutionDraft = { issueType: 'damaged', recommendedDecision: 'REQUEST_EVIDENCE',
    decisionReason: 'Evidence is required before support can decide the case.',
    customerFacingResponse: 'Please provide the requested evidence.', responseSource: 'DETERMINISTIC_FALLBACK',
    citations: [{ chunkId: 'chunk-1', title: 'Returns policy', documentId: 'returns', version: '1' }] };
  const tree = hooks.render(AdminInvestigation, { api: {}, supportCase, investigation, resolutionDraft,
    onComplete: () => {} }, []);
  const markup = renderToStaticMarkup(tree);
  assert.match(markup, /Request more evidence/);
  assert.match(markup, /Evidence is required/);
  assert.match(markup, /Deterministic fallback \(not Bedrock reasoning\)/);
  assert.match(markup, /Please provide the requested evidence/);
  assert.match(markup, /Returns policy/);
});

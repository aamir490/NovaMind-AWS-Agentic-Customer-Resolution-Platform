import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { readFile } from 'node:fs/promises';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const react = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

let server, AdminRefundRecommendation;
before(async () => {
  const { createServer } = await import(pathToFileURL(require.resolve('vite')).href);
  const { default: reactPlugin } = await import(pathToFileURL(require.resolve('@vitejs/plugin-react')).href);
  server = await createServer({ root: fileURLToPath(new URL('../frontend', import.meta.url)),
    configFile: false, server: { middlewareMode: true, hmr: false }, appType: 'custom',
    plugins: [reactPlugin()] });
  ({ AdminRefundRecommendation } = await server.ssrLoadModule('/src/CaseReview.jsx'));
});
after(async () => { await server?.close(); });

const citation = { documentId: 'return-policy', chunkId: 'a'.repeat(64),
  title: 'Return policy', version: 'demo-v1' };

function recommendation(outcome) {
  return {
    outcome, sku: 'LAP-1', policyId: 'standard-return', policyVersion: 'demo-v1',
    decisionReason: `Python assessment: ${outcome}.`, responseSource: 'DETERMINISTIC_FALLBACK',
    verifiedEvidence: ['case_id:00000000-0000-4000-8000-000000000001', 'purchased_sku:LAP-1'],
    missingEvidence: outcome === 'NEED_MORE_EVIDENCE' ? ['trusted delivery evidence'] : [],
    citations: [citation],
  };
}

test('ADMIN refund panel renders every authoritative outcome with closed evidence fields', () => {
  const labels = {
    ELIGIBLE: 'Eligible for staff review',
    NOT_ELIGIBLE: 'Not eligible',
    NEED_MORE_EVIDENCE: 'More evidence required',
  };
  for (const outcome of Object.keys(labels)) {
    const html = renderToStaticMarkup(react.createElement(AdminRefundRecommendation,
      { recommendation: recommendation(outcome) }));
    assert.match(html, new RegExp(labels[outcome]));
    assert.match(html, /LAP-1/);
    assert.match(html, new RegExp(`Python assessment: ${outcome}`));
    assert.match(html, /purchased_sku:LAP-1/);
    assert.match(html, /Return policy/);
    assert.match(html, /Deterministic Python fallback/);
    if (outcome === 'NEED_MORE_EVIDENCE') assert.match(html, /trusted delivery evidence/);
  }
});

test('no recommendation hides the panel and the panel exposes no payment control', async () => {
  assert.equal(renderToStaticMarkup(react.createElement(AdminRefundRecommendation,
    { recommendation: null })), '');
  const html = renderToStaticMarkup(react.createElement(AdminRefundRecommendation,
    { recommendation: recommendation('ELIGIBLE') }));
  assert.doesNotMatch(html, /<button|<form|refund payment|issue refund/i);
  assert.match(html, /does not approve, issue, or send a refund/);

  // Visibility is gated by the existing ADMIN conditional at the CaseReview composition boundary.
  const source = await readFile(new URL('../frontend/src/CaseReview.jsx', import.meta.url), 'utf8');
  assert.match(source, /identity\?\.role === 'ADMIN'.*AdminRefundRecommendation/s);
});

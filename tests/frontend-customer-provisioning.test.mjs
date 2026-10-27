import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const main = () => readFile(new URL('../frontend/src/main.jsx', import.meta.url), 'utf8');
const panel = () => readFile(new URL('../frontend/src/CustomerProvisioning.jsx', import.meta.url), 'utf8');

test('customer management is Admin-only in navigation and rendering', async () => {
  const source = await main();
  assert.match(source, /id: 'customers',[\s\S]*adminOnly: true/);
  assert.match(source, /!entry\.adminOnly[\s\S]*session\?\.identity\?\.role === 'ADMIN'/);
  assert.match(source, /activeView === 'customers' && session\?\.identity\?\.role === 'ADMIN'/);
});

test('customer provisioning warns on uncertain POST results and exposes verification', async () => {
  const source = await panel();
  assert.match(source, /Do not retry/);
  assert.match(source, /api\.getCustomer\(verificationId\.trim\(\)\)/);
  assert.match(source, /navigator\.clipboard\.writeText/);
  assert.match(source, /phase === 'submitting'\) return/);
});

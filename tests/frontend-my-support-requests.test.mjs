import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createApi } from '../frontend/src/api.js';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { renderToStaticMarkup } = require('react-dom/server');
const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const supportCase = (overrides = {}) => ({ id: id(1), order_id: id(2),
  subject: 'Wrong item', description: 'Please help with my order.\nThe item is wrong.',
  status: 'open', created_at: '2026-10-04T10:00:00Z', updated_at: '2026-10-05T12:00:00Z', ...overrides });
const client = (body) => createApi(async () => ({ ok: true, json: async () => body }));

test('customer case reads use authenticated GETs, no customer_id, and only display fields', async () => {
  const calls = [];
  const raw = supportCase({ customer_id: id(3), review: 'PRIVATE', agent: 'PRIVATE', proposal: 'PRIVATE' });
  const api = createApi(async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => calls.length === 1 ? [raw] : raw };
  }, { token: 'customer-token' });
  assert.deepEqual(await api.listMyCases({ customer_id: id(9) }), [supportCase()]);
  assert.deepEqual(await api.getMyCase(id(1), { customer_id: id(9) }), supportCase());
  assert.deepEqual(calls.map(({ url }) => url), ['/api/cases', `/api/cases/${id(1)}`]);
  for (const { options } of calls) {
    assert.equal(options.method, 'GET');
    assert.equal(options.headers.Authorization, 'Bearer customer-token');
    assert.equal(options.body, undefined);
    assert.equal(options.cache, 'no-store');
    assert.equal(options.credentials, 'omit');
    assert.equal(options.redirect, 'error');
  }
});

test('empty list is preserved; malformed responses and wrong detail IDs reject', async () => {
  assert.deepEqual(await client([]).listMyCases(), []);
  for (const body of [null, {}, [null], [supportCase({ updated_at: null })]]) {
    await assert.rejects(client(body).listMyCases(), /invalid my support request/);
  }
  for (const body of [null, supportCase({ id: id(4) }), supportCase({ order_id: null }),
    supportCase({ description: '' }), supportCase({ created_at: 'bad' })]) {
    await assert.rejects(client(body).getMyCase(id(1)), /invalid my support request/);
  }
  let calls = 0;
  const api = createApi(async () => { calls++; });
  await assert.rejects(api.getMyCase('../other?customer_id=123'), /case id/);
  assert.equal(calls, 0);
});

test('customer reads propagate errors and expire unauthorized sessions without retries', async () => {
  for (const method of [(api) => api.listMyCases(), (api) => api.getMyCase(id(1))]) {
    for (const status of [401, 403, 404, 500]) {
      let calls = 0, expired = 0;
      const api = createApi(async () => {
        calls++;
        return { ok: false, status, json: async () => ({ detail: 'Cannot load request' }) };
      }, { onUnauthorized: () => expired++ });
      await assert.rejects(method(api), { status, message: 'Cannot load request' });
      assert.equal(calls, 1);
      assert.equal(expired, status === 401 ? 1 : 0);
    }
    await assert.rejects(method(createApi(async () => { throw new Error('Offline'); })), /Offline/);
  }
});

test('customer reads support cancellation and session disposal', async () => {
  const signals = [];
  const api = createApi(async (url, options) => {
    signals.push(options.signal);
    return { ok: true, json: async () => url === '/api/cases' ? [] : supportCase() };
  });
  const controller = new AbortController();
  await api.listMyCases({ signal: controller.signal });
  await api.getMyCase(id(1));
  controller.abort();
  assert.equal(signals[0].aborted, true);
  api.dispose();
  assert.equal(signals[1].aborted, true);
  await assert.rejects(api.listMyCases(), /session has ended/);
  await assert.rejects(api.getMyCase(id(1)), /session has ended/);
});

// Retained hooks and effect cleanup, following the existing intake-navigation
// harness. Exercises real JSX and callbacks without adding a DOM dependency.
// It does not model browser events or React concurrent scheduling.
let server, hooks, CustomerPortal, MySupportRequests, SupportMessage, App;
before(async () => {
  const { createServer } = await import(pathToFileURL(require.resolve('vite')).href);
  const { default: react } = await import(pathToFileURL(require.resolve('@vitejs/plugin-react')).href);
  server = await createServer({ root: fileURLToPath(new URL('../frontend', import.meta.url)),
    configFile: false, server: { middlewareMode: true, hmr: false }, appType: 'custom',
    plugins: [{ name: 'support-request-hooks', enforce: 'pre',
      resolveId(id) { if (id === 'virtual:support-hooks') return '\0support-hooks'; },
      transform(code, id) {
        if (/(?:CustomerPortal|MySupportRequests|main)\.jsx$/.test(id)) {
          code = code.replace(/import \{ ([^}]+) \} from 'react';/,
            "import { $1 } from 'virtual:support-hooks';");
        }
        if (/\/main\.jsx$/.test(id)) {
          code = code.replace('function App()', 'export function App()')
            .replace(/^createRoot\(document.*$/m, '');
        }
        return code;
      },
      load(id) {
        if (id !== '\0support-hooks') return;
        return `export { StrictMode } from 'react';
          let current;
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
          export function unmount(slots) { slots.forEach(slot => slot?.cleanup?.()); }`;
      },
    }, react()] });
  hooks = await server.ssrLoadModule('virtual:support-hooks');
  ({ default: CustomerPortal } = await server.ssrLoadModule('/src/CustomerPortal.jsx'));
  ({ default: MySupportRequests, SupportMessage } = await server.ssrLoadModule('/src/MySupportRequests.jsx'));
  ({ App } = await server.ssrLoadModule('/src/main.jsx'));
});

test('support updates render single-line numbered guidance as safe ordered content', () => {
  const message = 'Try these steps: 1. Disconnect the controller. 2. Try another USB port. 3. Report the indicator state.';
  const html = renderToStaticMarkup(SupportMessage({ message }));
  assert.match(html, /<p>Try these steps:<\/p>/);
  assert.match(html, /<ol>/);
  for (const item of ['Disconnect the controller.', 'Try another USB port.', 'Report the indicator state.']) {
    assert.ok(html.includes(item));
  }
  assert.ok(html.indexOf('Disconnect the controller.') < html.indexOf('Try another USB port.'));
  assert.ok(html.indexOf('Try another USB port.') < html.indexOf('Report the indicator state.'));
  assert.doesNotMatch(html, /dangerouslySetInnerHTML/);
});

test('support updates render multiline paragraphs and lists while escaping markup', () => {
  const message = `Start with these checks.

1. Restart the controller.
2) Test a known-good cable.

- Keep the controller connected.
• Note the indicator color.

Reply with <script>alert("unsafe")</script> only as literal text.`;
  const html = renderToStaticMarkup(SupportMessage({ message }));
  assert.match(html, /<ol>/);
  assert.match(html, /<ul>/);
  assert.equal((html.match(/<li/g) ?? []).length, 4);
  for (const text of ['Start with these checks.', 'Restart the controller.', 'Test a known-good cable.',
    'Keep the controller connected.', 'Note the indicator color.', 'Reply with']) assert.ok(html.includes(text));
  assert.match(html, /&lt;script&gt;alert\(&quot;unsafe&quot;\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
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
const button = (tree, text) => find(tree, (el) => el.type === 'button' && el.props.children === text);
const tick = () => new Promise((resolve) => setImmediate(resolve));
function view(api) {
  const slots = [];
  let readSlots = [], key;
  return {
    render() {
      const read = hooks.render(MySupportRequests, { api }, slots);
      if (key !== read.key) { hooks.unmount(readSlots); readSlots = []; key = read.key; }
      return hooks.render(read.type, read.props, readSlots);
    },
    unmount() { hooks.unmount(readSlots); },
  };
}

test('list loading, subject/status/date, detail fetch, all detail fields, and Back navigation', async () => {
  const calls = [];
  const api = createApi(async (url, options) => {
    calls.push([url, options.method]);
    return { ok: true, json: async () => url === '/api/cases' ? [supportCase()]
      : url.endsWith('/resolution') ? { case: supportCase({ status: 'in_review' }), latest: {
        action: 'REQUEST_MORE_EVIDENCE',
        customer_message: 'Please upload a clear photo of the damaged item.', decided_at: '2026-10-06T12:00:00Z',
      } }
      : url.endsWith('/lifecycle') ? {
        case_id: id(1), case_status: 'in_review', progress: 'ADDITIONAL_EVIDENCE_REQUIRED',
        message: 'Please upload a clear photo of the damaged item.', updated_at: '2026-10-06T12:00:00Z',
      }
      : url.endsWith('/customer-communications/latest') ? {
        message: 'Try another USB port and let us know whether the controller still disconnects.',
        sent_at: '2026-10-07T12:00:00Z',
      }
      : supportCase({ status: 'in_review' }) };
  });
  const page = view(api);
  assert.match(renderToStaticMarkup(page.render()), /Loading your support requests/);
  await tick();
  const list = renderToStaticMarkup(page.render());
  assert.match(list, /Wrong item/);
  assert.match(list, />open</);
  assert.match(list, /Created date/);
  assert.match(list, /dateTime="2026-10-04T10:00:00Z"/i);
  assert.doesNotMatch(list, /Please help with my order/);
  button(page.render(), 'View details').props.onClick();
  assert.match(renderToStaticMarkup(page.render()), /Loading your support request/);
  await tick();
  const detail = renderToStaticMarkup(page.render());
  for (const label of ['Case ID', 'Order ID', 'Subject', 'Description', 'Status', 'Created', 'Updated']) {
    assert.ok(detail.includes(`<dt>${label}</dt>`));
  }
  for (const value of [id(1), id(2), 'Wrong item', 'Please help with my order.', 'in_review',
    '2026-10-04T10:00:00Z', '2026-10-05T12:00:00Z']) assert.ok(detail.includes(value));
  assert.match(detail, /Latest case decision/);
  assert.match(detail, /Latest Support Update/);
  assert.match(detail, /Try another USB port and let us know whether the controller still disconnects/);
  assert.match(detail, /dateTime="2026-10-07T12:00:00Z"/i);
  assert.match(detail, /More evidence requested/);
  assert.match(detail, /Please upload a clear photo of the damaged item/);
  assert.doesNotMatch(detail, /We need a clear photo of the damage/);
  assert.doesNotMatch(detail, /admin_user_id|00000000-0000-4000-8000-000000000013/);
  assert.doesNotMatch(detail, /<form|<input|<textarea|Approve|Reject|Run agent|Proposal/);
  button(page.render(), 'Back to My Support Requests').props.onClick();
  page.render();
  await tick();
  assert.match(renderToStaticMarkup(page.render()), /View details/);
  assert.deepEqual(calls, [['/api/cases', 'GET'], [`/api/cases/${id(1)}`, 'GET'],
    [`/api/cases/${id(1)}/resolution`, 'GET'],
    [`/api/cases/${id(1)}/lifecycle`, 'GET'],
    [`/api/cases/${id(1)}/customer-communications/latest`, 'GET'], ['/api/cases', 'GET']]);
  page.unmount();
});

test('empty list and list error are distinct; retry recovers', async () => {
  let fail = true;
  const page = view({ listMyCases: async () => {
    if (fail) throw new Error('Service unavailable');
    return [];
  } });
  page.render();
  await tick();
  let html = renderToStaticMarkup(page.render());
  assert.match(html, /role="alert"/);
  assert.match(html, /Service unavailable/);
  assert.doesNotMatch(html, /No support requests yet/);
  fail = false;
  button(page.render(), 'Try again').props.onClick();
  assert.match(renderToStaticMarkup(page.render()), /Loading your support requests/);
  await tick();
  html = renderToStaticMarkup(page.render());
  assert.match(html, /No support requests yet/);
  assert.doesNotMatch(html, /role="alert"|View details/);
  page.unmount();
});

test('detail errors keep Back available and retry fetches detail again', async () => {
  let fail = true;
  const page = view({ listMyCases: async () => [supportCase()], getMyCase: async () => {
    if (fail) throw new Error('Request not found');
    return supportCase();
  } });
  page.render(); await tick();
  button(page.render(), 'View details').props.onClick();
  page.render(); await tick();
  assert.match(renderToStaticMarkup(page.render()), /Request not found/);
  button(page.render(), 'Back to My Support Requests');
  fail = false;
  button(page.render(), 'Try again').props.onClick();
  page.render(); await tick();
  assert.match(renderToStaticMarkup(page.render()), /Please help with my order/);
  page.unmount();
});

test('Back during detail loading cancels the read and ignores its late response', async () => {
  let resolveDetail, signal;
  const page = view({ listMyCases: async () => [supportCase()], getMyCase: (caseId, options) => {
    signal = options.signal;
    return new Promise((resolve) => { resolveDetail = resolve; });
  } });
  page.render(); await tick();
  button(page.render(), 'View details').props.onClick();
  page.render();
  button(page.render(), 'Back to My Support Requests').props.onClick();
  page.render();
  assert.equal(signal.aborted, true);
  resolveDetail(supportCase({ subject: 'STALE DETAIL' }));
  await tick();
  const html = renderToStaticMarkup(page.render());
  assert.match(html, /View details/);
  assert.doesNotMatch(html, /STALE DETAIL/);
  page.unmount();
});

test('portal navigation mounts customer support; app isolates CUSTOMER from staff workspaces', () => {
  const slots = [];
  const portal = () => hooks.render(CustomerPortal, { api: {} }, slots);
  button(portal(), 'My Support Requests').props.onClick();
  find(portal(), (el) => el.type === MySupportRequests);
  assert.equal(button(portal(), 'My Support Requests').props['aria-current'], 'page');
  for (const role of ['CUSTOMER', 'REVIEWER', 'ADMIN', null]) {
    // App's existing session state is seeded; authentication itself is unchanged.
    const session = role ? { api: {}, identity: { role } } : null;
    const tree = hooks.render(App, {}, [{ configuration: null, error: null }, 'idle', 'cases', session]);
    const nodes = [...elements(tree)];
    assert.equal(nodes.some((el) => el.type === CustomerPortal), role === 'CUSTOMER');
    const staff = ['CaseReview', 'AIWorkspace', 'ReviewQueue', 'AuditWorkspace', 'DiagnosticsWorkspace'];
    if (role === 'CUSTOMER') {
      assert.equal(nodes.some((el) => typeof el.type === 'function' && staff.includes(el.type.name)), false);
      assert.equal(nodes.some((el) => el.type === 'button' &&
        ['Cases', 'AI Workspace', 'Reviews', 'Audit', 'Diagnostics'].includes(el.props.children)), false);
    } else {
      assert.equal(nodes.some((el) => el.type === MySupportRequests), false);
    }
  }
});

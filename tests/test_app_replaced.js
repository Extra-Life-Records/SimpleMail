/* Run with: node --test tests/test_app_replaced.js */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

function app() {
  const elements = new Map();
  const element = () => ({ textContent: '', hidden: true, disabled: false, style: {}, className: '',
    classList: { add() {}, remove() {}, toggle() {} }, addEventListener() {} });
  const cache = new Map();
  const context = vm.createContext({
    window: { addEventListener() {} }, URL, console,
    localStorage: { setItem: (k, v) => cache.set(k, String(v)), getItem: k => cache.get(k) ?? null,
      removeItem: k => cache.delete(k), key: n => [...cache.keys()][n], get length() { return cache.size; } },
    document: { getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
      querySelectorAll: () => [] },
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
  });
  const run = js => vm.runInContext(js, context);
  run(source);
  return { run, elements, cache };
}
const tick = () => new Promise(yes => setImmediate(yes));

test('a swapped-in build shows the restart banner', async () => {
  const a = app();
  a.run("api = { app_replaced: async () => ({ replaced: true }) }");
  assert.equal(await a.run('checkAppReplaced()'), true);
  assert.equal(a.elements.get('restart-status').hidden, false);
});

test('an unchanged build keeps the banner hidden, and old backends are tolerated', async () => {
  const a = app();
  a.run("api = { app_replaced: async () => ({ replaced: false }) }");
  assert.equal(await a.run('checkAppReplaced()'), false);
  assert.equal(a.elements.get('restart-status').hidden, true);
  a.run('api = {}');
  assert.equal(await a.run('checkAppReplaced()'), false);
});

test('the zlib error becomes a plain restart message and triggers the check', async () => {
  const a = app();
  a.run("api = { app_replaced: async () => ({ replaced: true }) }");
  a.run("toast('Keep this draft open until saving succeeds: error: Error -3 while decompressing data: incorrect header check', true)");
  assert.match(a.elements.get('toast').textContent, /^SimpleMail was updated while it was open/);
  await tick();
  assert.equal(a.elements.get('restart-status').hidden, false);
});

test('the compose status line gets the plain message too', () => {
  const a = app();
  assert.match(a.run("friendlyError(Error('Error -3 while decompressing data: incorrect header check'))"), /^SimpleMail was updated/);
});

test('other errors keep their text', () => {
  const a = app();
  a.run("toast('Could not discard draft: busy', true)");
  assert.equal(a.elements.get('toast').textContent, 'Could not discard draft: busy');
});

test('restart hands every recovery draft to the app', async () => {
  const a = app();
  const draft = { id: 'd1', account_id: 'one', revision: 0, payload: { body: 'unsaved' } };
  a.cache.set('simplemail-compose:one:d1', JSON.stringify(draft));
  a.cache.set('sm-list-width', '300');
  let sent;
  a.run('api = { restart_app: async drafts => { globalThis.sent = drafts; } }');
  await a.run('restartApp()');
  sent = a.run('globalThis.sent');
  assert.deepEqual(JSON.parse(JSON.stringify(sent)), [draft]);
});

test('drafts handed back after a restart return to recovery storage', async () => {
  const a = app();
  const draft = { id: 'd1', account_id: 'one', revision: 0, payload: { body: 'unsaved' } };
  a.run(`api = { take_restart_drafts: async () => [${JSON.stringify(draft)}, {id: 'junk'}] }`);
  await a.run('restoreRestartDrafts()');
  assert.deepEqual(JSON.parse(a.cache.get('simplemail-compose:one:d1')), draft);
  assert.equal(a.cache.size, 1);
  assert.match(a.elements.get('toast').textContent, /Your open draft was kept/);
});

test('a failed restart says so and re-enables the button', async () => {
  const a = app();
  a.run("api = { restart_app: async () => { throw Error('no powershell'); } }");
  await a.run('restartApp()');
  assert.match(a.elements.get('toast').textContent, /Restart failed: Error: no powershell/);
  assert.equal(a.elements.get('restart-now').disabled, false);
});

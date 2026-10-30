/* Run with: node --test tests/test_auto_update.js */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/autoupdate.js'), 'utf8');

function page({ available = true, applyFails = false } = {}) {
  const open = new Set();
  const listeners = {};
  const timers = [];
  let clock = 1_000_000;
  const calls = { check: 0, apply: 0, toasts: [] };
  const context = vm.createContext({
    Date: { now: () => clock },
    document: {
      activeElement: null,
      getElementById: (id) => ({ classList: { contains: (c) => c === 'show' && open.has(id) } }),
      addEventListener: (ev, fn) => { listeners[ev] = fn; },
    },
    setTimeout: (fn, ms) => { timers.push({ fn, at: clock + ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; },
    toast: (m) => calls.toasts.push(m),
    api: {
      check_update: async () => { calls.check++; return { available, new_version: 'v9.9.9' }; },
      apply_update: async () => { calls.apply++; if (applyFails) throw new Error('download failed'); return { ok: true }; },
    },
  });
  vm.runInContext(source, context);
  // run every timer that is due by `ms` from now, in order
  async function advance(ms) {
    const until = clock + ms;
    for (;;) {
      const next = timers.filter(t => t.fn && t.at <= until).sort((a, b) => a.at - b.at)[0];
      if (!next) break;
      clock = Math.max(clock, next.at);
      const fn = next.fn; next.fn = null;
      await fn();
    }
    clock = until;
  }
  return { context, open, listeners, calls, advance, type: () => listeners.keydown() };
}

test('installs on its own after two quiet minutes', async () => {
  const p = page();
  p.context.startAutoUpdate();
  await p.advance(0);
  assert.equal(p.calls.check, 1);
  assert.equal(p.calls.apply, 0, 'just started: someone may be about to type');
  await p.advance(2 * 60 * 1000 + 1000);
  assert.equal(p.calls.apply, 1);
  assert.match(p.calls.toasts[0], /Updating SimpleMail to v9\.9\.9/);
  assert.equal(p.calls.check, 1, 'waiting for quiet does not ask GitHub again');
});

test('waits while a compose window is open, then installs once it closes', async () => {
  const p = page();
  p.open.add('compose-backdrop');
  p.context.startAutoUpdate();
  await p.advance(30 * 60 * 1000);
  assert.equal(p.calls.apply, 0);
  p.open.delete('compose-backdrop');
  await p.advance(61 * 1000);
  assert.equal(p.calls.apply, 1);
});

test('typing keeps pushing it back', async () => {
  const p = page();
  p.context.startAutoUpdate();
  await p.advance(0);
  for (let i = 0; i < 5; i++) { await p.advance(60 * 1000); p.type(); }
  assert.equal(p.calls.apply, 0);
  await p.advance(3 * 60 * 1000);
  assert.equal(p.calls.apply, 1);
});

test('text left in the focused box counts as writing', async () => {
  const p = page();
  p.context.document.activeElement = { tagName: 'TEXTAREA', value: 'half a sentence' };
  p.context.startAutoUpdate();
  await p.advance(10 * 60 * 1000);
  assert.equal(p.calls.apply, 0);
  p.context.document.activeElement = { tagName: 'INPUT', value: '' };
  await p.advance(61 * 1000);
  assert.equal(p.calls.apply, 1);
});

test('nothing new: checks again in three hours, never installs', async () => {
  const p = page({ available: false });
  p.context.startAutoUpdate();
  await p.advance(0);
  await p.advance(3 * 60 * 60 * 1000 - 1000);
  assert.equal(p.calls.check, 1);
  await p.advance(2000);
  assert.equal(p.calls.check, 2);
  assert.equal(p.calls.apply, 0);
});

test('a failed install does not retry every minute', async () => {
  const p = page({ applyFails: true });
  p.context.startAutoUpdate();
  await p.advance(3 * 60 * 1000);
  assert.equal(p.calls.apply, 1);
  await p.advance(60 * 60 * 1000);
  assert.equal(p.calls.apply, 1);
  await p.advance(2 * 60 * 60 * 1000 + 1000);
  assert.equal(p.calls.check, 2);
  assert.equal(p.calls.apply, 2);
});

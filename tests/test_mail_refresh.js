/* Run with: node --test tests/test_mail_refresh.js */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function app() {
  const elements = new Map();
  const element = () => ({
    innerHTML: '', textContent: '', value: '', scrollTop: 0, style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, appendChild() {}, querySelectorAll: () => [],
  });
  const timers = new Map();
  let timerId = 0;
  const context = vm.createContext({
    window: { addEventListener() {} }, URL, console,
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
      },
      createElement: element, createTextNode: text => ({ textContent: text }), querySelectorAll: () => [],
    },
    setTimeout: () => 1, clearTimeout() {},
    setInterval(fn, delay) { timers.set(++timerId, { fn, delay }); return timerId; },
    clearInterval(id) { timers.delete(id); },
    confirm: () => true,
  });
  const run = (js) => vm.runInContext(js, context);
  run(source);
  run(`state.accounts = [{id:'a'}, {id:'b'}];
    state.activeAccountId = 'a';
    state.folders = [{key:'inbox', server:'INBOX', name:'Inbox', unread:2}];
    api = { set_active_account: async () => {} };
    renderMessages = () => { $('msg-list').innerHTML = state.messages.map(m => m.subject).join(','); };
    renderAccounts = () => {}; renderFolders = () => {};`);
  return { run, context, elements, timers, api: run('api') };
}

const mail = (uid, seen = false) => ({ uid, subject: `Message ${uid}`, seen });
const response = (envelopes, count = 2) => ({ envelopes, folder_unread: count, inbox_unread: count });

test('new mail refresh preserves reader, selection, search and list scroll', async () => {
  const a = app();
  a.api.list_messages = async () => response([mail('2'), mail('1', true)], 7);
  a.run(`state.selectedUid='1'; $('read-body').innerHTML='Reading message 1';
    $('search-box').value='receipt'; $('msg-list').scrollTop=150;`);
  await a.run('loadMessages({quiet:true})');
  assert.equal(a.run('state.messages.length'), 2);
  assert.equal(a.run('state.selectedUid'), '1');
  assert.equal(a.run("$('read-body').innerHTML"), 'Reading message 1');
  assert.equal(a.run("$('search-box').value"), 'receipt');
  assert.equal(a.run("$('msg-list').scrollTop"), 150);
  assert.equal(a.run("$('folder-count').textContent"), '7 unread · 2 shown');
});

test('timer polls every 30 seconds without overlapping requests or duplicating timers', async () => {
  const a = app(), pending = deferred();
  let calls = 0;
  a.api.list_messages = () => { calls++; return pending.promise; };
  a.run('startMailRefresh(); startMailRefresh()');
  assert.equal(a.timers.size, 1);
  const timer = [...a.timers.values()][0];
  assert.equal(timer.delay, 30000);
  const loading = timer.fn();
  await timer.fn();
  assert.equal(calls, 1);
  pending.resolve(response([]));
  await loading;
  await timer.fn();
  assert.equal(calls, 2);
});

test('temporary background failure preserves mail and recovers on the next refresh', async () => {
  const a = app();
  a.run("state.messages=[{uid:'1'}]; $('msg-list').innerHTML='Existing mail'");
  a.api.list_messages = async () => { throw new Error('offline'); };
  await a.run('loadMessages({quiet:true})');
  assert.equal(a.run("$('msg-list').innerHTML"), 'Existing mail');
  assert.equal(a.run("$('toast').textContent"), '');
  a.api.list_messages = async () => response([mail('2')]);
  await a.run('loadMessages({quiet:true})');
  assert.equal(a.run('state.messages[0].uid'), '2');
});

test('older account and folder responses cannot replace the new mailbox', async () => {
  const a = app(), pending = deferred();
  a.api.list_messages = () => pending.promise;
  const loading = a.run('loadMessages()');
  a.run("resetMailboxView(); state.activeAccountId='b'; state.messages=[{uid:'b'}]");
  pending.resolve(response([mail('a')]));
  await loading;
  assert.equal(a.run('state.messages[0].uid'), 'b');
});

test('latest manual refresh wins when replies arrive out of order', async () => {
  const a = app(), first = deferred(), second = deferred();
  a.api.list_messages = () => first.promise;
  const old = a.run('loadMessages()');
  a.api.list_messages = () => second.promise;
  const recent = a.run('loadMessages()');
  second.resolve(response([mail('new')]));
  await recent;
  first.resolve(response([mail('old')]));
  await old;
  assert.equal(a.run('state.messages[0].uid'), 'new');
});

test('reading while a refresh is pending keeps the message read', async () => {
  const a = app(), pending = deferred();
  a.run("state.messages=[{uid:'1',seen:false}]");
  a.api.list_messages = () => pending.promise;
  const loading = a.run('loadMessages({quiet:true})');
  a.api.get_message = async () => ({subject:'Message 1', text:'Message content'});
  await a.run("openMessage('1', null)");
  pending.resolve(response([mail('1', false)]));
  await loading;
  assert.equal(a.run('state.messages[0].seen'), true);
});

test('a slow message body cannot replace a newer selection or another account', async () => {
  const a = app(), pending = deferred();
  a.api.get_message = () => pending.promise;
  const old = a.run("openMessage('1', null)");
  a.api.get_message = async () => ({subject:'New selection', text:'New content'});
  await a.run("openMessage('2', null)");
  pending.resolve({subject:'Old selection', text:'Old content'});
  await old;
  assert.equal(a.run("$('read-subject').textContent"), 'New selection');
  const other = deferred();
  a.api.get_message = () => other.promise;
  const switched = a.run("openMessage('1', null)");
  a.run("resetMailboxView(); state.activeAccountId='b'; $('read-body').innerHTML='Another account'");
  other.resolve({subject:'Account a', text:'Account a body'});
  await switched;
  assert.equal(a.run("$('read-body').innerHTML"), 'Another account');
});

test('slow folder discovery cannot select a previously chosen account', async () => {
  const a = app(), pending = deferred();
  a.api.get_folders = (id) => id === 'a' ? pending.promise : Promise.resolve({
    folders: [{key:'inbox', server:'INBOX', name:'B inbox'}],
  });
  a.api.list_messages = async () => response([mail('b')]);
  const old = a.run("selectAccount('a')");
  await a.run("selectAccount('b')");
  pending.resolve({folders: [{key:'inbox',server:'OLD',name:'A inbox'}]});
  await old;
  assert.equal(a.run('state.activeAccountId'), 'b');
  assert.equal(a.run('state.folders[0].name'), 'B inbox');
  assert.equal(a.run('state.messages[0].uid'), 'b');
});

test('no refresh is attempted without a configured account or folder', async () => {
  const a = app();
  a.api.list_messages = () => assert.fail('unexpected request');
  a.run('state.activeAccountId=null');
  await a.run('loadMessages({quiet:true})');
  a.run("state.activeAccountId='a'; state.folders=[]");
  await a.run('loadMessages()');
});

test('Inbox badge refreshes while Sent remains selected', async () => {
  const a = app();
  a.run("state.folders.push({key:'sent',server:'Sent'}); state.currentFolder='sent'");
  a.api.list_messages = async () => ({envelopes: [mail('1', true)], folder_unread: 0, inbox_unread: 8});
  await a.run('loadMessages({quiet:true})');
  assert.equal(a.run('state.currentFolder'), 'sent');
  assert.equal(a.run('state.folders[0].unread'), 8);
  assert.equal(a.run("$('folder-count').textContent"), '0 unread · 1 shown');
});

test('email links allow web and email addresses while blocking local and active schemes', () => {
  const a = app();
  assert.equal(a.run("safeEmailLink('//example.com/receipt')"), 'https://example.com/receipt');
  assert.equal(a.run("safeEmailLink('mailto:hello@example.com')"), 'mailto:hello@example.com');
  for (const link of ['javascript:alert(1)', 'file:///C:/Windows/win.ini', 'data:text/html,test', '/settings']) {
    a.context.link = link;
    assert.equal(a.run('safeEmailLink(link)'), null);
  }
});

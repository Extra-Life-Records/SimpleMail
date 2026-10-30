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

test('cloud search reaches sparse indexed matches without manual empty-page clicks and remains bounded', async () => {
  const a = app(); a.run("state.accounts[0].provider='cloud'; $('search-box').value='late (match)'; queueMailSearch()");
  const calls = [];
  a.api.search_messages = async (_,__,cursor) => {
    calls.push(cursor); const number = cursor ? Number(cursor) + 1 : 1;
    return {items:number === 6 ? [mail('late')] : [], next_cursor:String(number),
      search_progress:{phase:'index',scanned:100,next_phase:'index'}};
  };
  await a.run('loadSearchResults()');
  assert.equal(calls.length,12); assert.equal(a.run('state.messages[0].uid'),'late');
  assert.equal(a.run('mailSearch.cursor'),'12'); assert.equal(a.run('mailSearch.busy'),false);
  assert.equal(a.run('mailSearch.scanned.index'),1200);
});

test('cloud no-match stays incomplete until exhausted and resume starts at the last cursor', async () => {
  const a=app(); a.run("state.accounts[0].provider='cloud'; $('search-box').value='absent'; queueMailSearch()");
  const calls=[];
  a.api.search_messages=async (_,__,cursor)=>{calls.push(cursor);const n=cursor?Number(cursor)+1:1;
    return {items:[],next_cursor:n===13?null:String(n)};};
  await a.run('loadSearchResults()');
  assert.equal(a.run('mailSearch.cursor'),'12'); assert.equal(a.run('state.messages.length'),0);
  await a.run('loadSearchResults(true)');
  assert.equal(calls.at(-1),'12'); assert.equal(a.run('mailSearch.cursor'),null);
});

test('cloud automatic continuation preserves results and retries the failed cursor once', async () => {
  const a=app(); a.run("state.accounts[0].provider='cloud'; $('search-box').value='match'; queueMailSearch()");
  a.api.search_messages=async (_,__,cursor)=>{if(cursor)throw Error('offline');return {items:[mail('first')],next_cursor:'next'};};
  await a.run('loadSearchResults()');
  assert.equal(a.run('mailSearch.retryMore'),true); assert.equal(a.run('state.messages.length'),1);
  a.api.search_messages=async (_,__,cursor)=>{assert.equal(cursor,'next');return {items:[mail('first'),mail('second')],next_cursor:null};};
  await a.run('loadSearchResults(mailSearch.retryMore)');
  assert.equal(a.run('state.messages.length'),2);
});

test('cloud pause stops after the in-flight read without dropping its result', async () => {
  const a=app(); a.run("state.accounts[0].provider='cloud'; $('search-box').value='match'; queueMailSearch()");
  const pending=deferred(); let calls=0;
  a.api.search_messages=()=>{calls++;return pending.promise;};
  const work=a.run('loadSearchResults()'); a.run('mailSearch.paused=true');
  pending.resolve({items:[mail('arrived')],next_cursor:'next'}); await work;
  assert.equal(calls,1); assert.equal(a.run('state.messages[0].uid'),'arrived');
  assert.equal(a.run('mailSearch.cursor'),'next'); assert.equal(a.run('mailSearch.busy'),false);
});

test('replacing a cloud query or mailbox prevents an old continuation from starting another request', async () => {
  const a=app(); a.run("state.accounts[0].provider='cloud'; $('search-box').value='old'; queueMailSearch()");
  const pending=deferred();let calls=0;
  a.api.search_messages=()=>{calls++;return pending.promise;};
  const old=a.run('loadSearchResults()');
  a.run("resetMailboxView(); state.activeAccountId='b'; state.messages=[{uid:'other'}]");
  pending.resolve({items:[mail('stale')],next_cursor:'old-next'});await old;
  assert.equal(calls,1);assert.equal(a.run('state.messages[0].uid'),'other');assert.equal(a.run('mailSearch'),null);
});

test('cloud elapsed-time budget returns control and preserves continuation', async () => {
  const a=app();a.run("state.accounts[0].provider='cloud'; $('search-box').value='match'; queueMailSearch(); let clock=0; Date.now=()=>clock");
  let calls=0;a.api.search_messages=async()=>{calls++;a.run('clock+=3000');return {items:[],next_cursor:String(calls)};};
  await a.run('loadSearchResults()');assert.equal(calls,3);assert.equal(a.run('mailSearch.cursor'),'3');
});

test('quiet cloud refresh exposes expired sign-in until a successful refresh', async () => {
  const a = app();
  a.run("state.accounts[0].provider='cloud'; state.messages=[{uid:'saved',subject:'Existing mail'}]");
  a.api.list_messages = async () => { throw Error('Reconnect employee mailboxes in Settings.'); };
  await a.run('loadMessages({quiet:true})');
  assert.equal(a.run("$('cloud-status').hidden"), false);
  assert.match(a.run("$('cloud-status-message').textContent"), /Reconnect/);
  assert.equal(a.run('state.messages[0].uid'), 'saved');
  a.api.list_messages = async () => response([mail('current')]);
  await a.run('loadMessages({quiet:true})');
  assert.equal(a.run("$('cloud-status').hidden"), true);
  assert.equal(a.run('state.messages[0].uid'), 'current');
});

test('server search keeps body-only results and appends complete continuation pages', async () => {
  const a = app();
  a.run("$('search-box').value='body-only'; queueMailSearch()");
  const calls=[];
  a.api.search_messages = async (...args) => {calls.push(args);return {items:[
    {...mail(args[2] ? 'second' : 'first'),sender:'Person',subject:'Unrelated subject',server_uid:'1',server_folder:args[2]?'Archive':'INBOX',validity:args[2]?'8':'7'}],next_cursor:args[2]?null:'next'};};
  await a.run('loadSearchResults()');
  assert.equal(a.run('state.messages.length'),1);
  await a.run('loadSearchResults(true)');
  assert.equal(a.run('state.messages.length'),2);
  assert.deepEqual(calls,[['a','body-only',null,null],['a','body-only','next',null]]);
  assert.equal(a.run("$('markall-btn').disabled"),true);
});

test('search continuation failure retains existing results and retries the same cursor', async () => {
  const a = app(); a.run("$('search-box').value='receipt'; queueMailSearch()");
  a.api.search_messages = async () => ({items:[{...mail('first'),sender:'Person'}],next_cursor:'next'});
  await a.run('loadSearchResults()');
  a.api.search_messages = async () => {throw Error('offline');};
  await a.run('loadSearchResults(true)');
  assert.equal(a.run('state.messages[0].uid'),'first');
  assert.equal(a.run('mailSearch.cursor'),'next');
  assert.equal(a.run('mailSearch.retryMore'),true);
  let cursor;
  a.api.search_messages = async (_,__,value) => {cursor=value;return {items:[{...mail('second'),sender:'Person'}],next_cursor:null};};
  await a.run('loadSearchResults(mailSearch.retryMore)');
  assert.equal(cursor,'next');
  assert.equal(a.run('state.messages.length'),2);
});

test('late search response cannot replace a newer query or another folder', async () => {
  const a = app(); const pending = deferred();
  a.run("$('search-box').value='old'; queueMailSearch()");
  a.api.search_messages = () => pending.promise;
  const old = a.run('loadSearchResults()');
  a.run("$('search-box').value='new'; queueMailSearch()");
  a.api.search_messages = async () => ({items:[{...mail('new'),sender:'Person'}],next_cursor:null});
  await a.run('loadSearchResults()');
  pending.resolve({items:[{...mail('old'),sender:'Person'}],next_cursor:null}); await old;
  assert.equal(a.run('state.messages[0].uid'),'new');
  const later = deferred(); a.api.search_messages = () => later.promise;
  const request=a.run('loadSearchResults()');
  a.api.list_messages=async()=>response([mail('normal')]);
  await a.run("selectFolder('inbox')");
  later.resolve({items:[{...mail('stale'),sender:'Person'}],next_cursor:null});await request;
  assert.equal(a.run('state.messages[0].uid'),'normal');
});

test('opening search results uses their source UID and validity and changes its source badge only', async () => {
  const a = app();
  a.run("state.folders.push({key:'archive',server:'Archive',unread:4}); state.messages=[{uid:'search:ref',server_uid:'1',server_folder:'Archive',validity:'8',seen:false}]");
  let received;
  a.api.get_message=async(...args)=>{received=args;return {subject:'Found',sender:'Person',to:'Owner',date:'today',text:'Body'};};
  await a.run("openMessage('search:ref',null)");
  assert.deepEqual(received,['a','Archive','1','8']);
  assert.equal(a.run('state.folders[0].unread'),2);
  assert.equal(a.run('state.folders[1].unread'),3);
});

test('recreated folder clears selection even when a different message reuses its UID', async () => {
  const a = app();
  a.run("state.folders[0].validity='7'; state.selectedUid='1'; $('read-body').innerHTML='Old message'");
  a.api.list_messages = async () => ({...response([mail('1')]),validity:'8'});
  await a.run('loadMessages({quiet:true})');
  assert.equal(a.run('state.selectedUid'),null);
  assert.equal(a.run('state.folders[0].validity'),'8');
  assert(!a.run("$('read-body').innerHTML").includes('Old message'));
});

test('filing sends observed folder identity and duplicate clicks do not start another move', async () => {
  const a = app();
  a.run("state.folders[0].validity='7'; state.messages=[{uid:'3',seen:false,subject:'Newsletter'}]; state.selectedUid='3'");
  const pending = deferred(); const calls=[];
  a.api.delete_message = async (...args) => {calls.push(args);return pending.promise;};
  const first = a.run('deleteSelected()');
  await a.run('deleteSelected()');
  assert.equal(calls.length,1);
  assert.deepEqual(calls[0],['a','INBOX','3','7']);
  pending.resolve({status:'uncertain',warning:'Needs checking'});
  await first;
  assert.equal(a.run('state.messages.length'),1);
  assert.equal(a.run('state.folders[0].unread'),2);
});

test('reply quotes escape incoming HTML outside the reader sandbox', async () => {
  const a = app();
  a.api.get_message = async () => ({html:'<img src=x onerror="window.pywebview.api.send_mail()">',
    text:'<img src=x onerror="steal()">', date:'Today', sender:'Customer'});
  const quote = await a.run("quoteOriginal({uid:'1'})");
  assert(!quote.includes('<img'));
  assert(quote.includes('&lt;img'));
});

test('local drafts are usable before remote folder pagination finishes', async () => {
  const a = app(), remote = deferred();
  a.run("state.currentFolder='drafts'; state.folders=[{key:'drafts',server:'Drafts',name:'Drafts'}]");
  a.api.list_messages = () => remote.promise;
  a.api.list_compose_drafts = async () => [{id:'local-1',status:'pending',updated_at:'2026-10-19',
    payload:{to:'',subject:'Ready immediately',body:'Saved work'}}];
  const loading = a.run('loadMessages()');
  await new Promise(setImmediate);
  assert.equal(a.run('state.messages[0].localDraftId'), 'local-1');
  assert.equal(a.run("$('msg-list').innerHTML"), 'Ready immediately');
  remote.resolve(response([mail('remote')]));
  await loading;
  assert.equal(a.run('state.messages.length'), 2);
  assert.equal(a.run('state.messages[0].localDraftId'), 'local-1');
});

test('late local drafts never replace a newly selected mailbox', async () => {
  const a = app(), local = deferred(), remote = deferred();
  a.run("state.currentFolder='drafts'; state.folders=[{key:'drafts',server:'Drafts',name:'Drafts'}]");
  a.api.list_messages = () => remote.promise;
  a.api.list_compose_drafts = () => local.promise;
  const loading = a.run('loadMessages()');
  a.run("resetMailboxView(); state.activeAccountId='b'; state.messages=[{uid:'b'}]");
  local.resolve([{id:'old',status:'pending',updated_at:'2026-10-19',payload:{to:'',subject:'Old',body:'Saved'}}]);
  await new Promise(setImmediate);
  assert.equal(a.run('state.messages[0].uid'), 'b');
  remote.resolve(response([])); await loading;
  assert.equal(a.run('state.messages[0].uid'), 'b');
});

test('local drafts remain visible when the IMAP Drafts folder is offline', async () => {
  const a = app();
  a.run("state.currentFolder='drafts'; state.folders=[{key:'drafts',server:'Drafts',name:'Drafts'}]");
  a.api.list_messages = async () => { throw Error('IMAP unavailable'); };
  a.api.list_compose_drafts = async () => [{id:'local-1',status:'pending',updated_at:'2026-09-30T12:00:00Z',
    payload:{to:'person@example.com',subject:'Recover me',body:'Saved work'}}];
  await a.run('loadMessages()');
  assert.equal(a.run("state.messages[0].localDraftId"),'local-1');
  assert.equal(a.run("$('msg-list').innerHTML"),'Recover me');
});

test('local cloud drafts do not hide an expired mailbox session', async () => {
  const a = app();
  a.run("state.accounts[0].provider='cloud'; state.currentFolder='drafts'; state.folders=[{key:'drafts',server:'Drafts',name:'Drafts'}]");
  a.api.list_messages = async () => { throw Error('Reconnect employee mailboxes in Settings.'); };
  a.api.list_compose_drafts = async () => [{id:'local-1',status:'pending',updated_at:'2026-10-08',
    payload:{to:'person@example.com',subject:'Recover me',body:'Saved work'}}];
  await a.run('loadMessages()');
  assert.equal(a.run("state.messages[0].localDraftId"), 'local-1');
  assert.equal(a.run("$('cloud-status').hidden"), false);
  assert.match(a.run("$('cloud-status-message').textContent"), /Reconnect/);
});

test('a local-only Drafts folder never marks Inbox read', async () => {
  const a = app();
  a.run("state.currentFolder='drafts'; state.folders=[{key:'drafts',server:null,name:'Drafts'}]");
  let calls=0; a.api.mark_all_read=async()=>{calls++;};
  await a.run('markAllRead()');
  assert.equal(calls,0);
});

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

const tick = () => new Promise(resolve => setImmediate(resolve));

test('going back to a folder shows its last list at once, then the refresh replaces it', async () => {
  const a = app();
  a.run("state.folders.push({key:'sent',server:'Sent',name:'Sent'})");
  a.api.list_messages = async (_, server) => response([mail(server + '-1')]);
  await a.run("selectFolder('inbox')");
  await a.run("selectFolder('sent')");
  const pending = deferred();
  a.api.list_messages = () => pending.promise;
  const back = a.run("selectFolder('inbox')");
  assert.equal(a.run("$('msg-list').innerHTML"), 'Message INBOX-1');
  pending.resolve(response([mail('INBOX-2'), mail('INBOX-1')]));
  await back;
  assert.equal(a.run("$('msg-list').innerHTML"), 'Message INBOX-2,Message INBOX-1');
});

test('sidebar folders are read one at a time, Sent first, without touching the open list', async () => {
  const a = app();
  a.run(`state.folders.push({key:'trash',server:'Trash',name:'Trash'}, {key:'sent',server:'Sent',name:'Sent'},
    {key:'custom',server:'Custom',name:'Custom'}); state.messages=[{uid:'open'}]`);
  const calls = []; let inFlight = 0, most = 0;
  a.api.list_messages = async (_, server) => {
    calls.push(server); most = Math.max(most, ++inFlight);
    await tick(); inFlight--;
    return response([mail(server)]);
  };
  await a.run("prefetchFolders('a')");
  assert.deepEqual(calls, ['Sent', 'Trash']);  // not Inbox (open), not "More folders"
  assert.equal(most, 1);
  assert.equal(a.run('state.messages[0].uid'), 'open');
  a.api.list_messages = () => new Promise(() => {});
  a.run("selectFolder('sent')");
  assert.equal(a.run("$('msg-list').innerHTML"), 'Message Sent');
});

test('opening a folder while its background read is running shares that read', async () => {
  const a = app();
  a.run("state.folders.push({key:'sent',server:'Sent',name:'Sent'})");
  const pending = deferred(); let calls = 0;
  a.api.list_messages = () => { calls++; return pending.promise; };
  const prefetch = a.run("prefetchFolders('a')");
  const open = a.run("selectFolder('sent')");
  pending.resolve(response([mail('sent')]));
  await Promise.all([prefetch, open]);
  assert.equal(calls, 1);
  assert.equal(a.run('state.messages[0].uid'), 'sent');
});

test('a background read for the previous mailbox never fills the new one', async () => {
  const a = app();
  a.run("state.folders.push({key:'sent',server:'Sent',name:'Sent'})");
  const pending = deferred();
  a.api.list_messages = () => pending.promise;
  const prefetch = a.run("prefetchFolders('a')");
  a.api.get_folders = async () => ({folders: [{key:'inbox',server:'INBOX',name:'Inbox'}, {key:'sent',server:'Sent',name:'Sent'}]});
  a.api.list_messages = async (_, server) => response([mail('b-' + server)]);
  await a.run("selectAccount('b')");
  pending.resolve(response([mail('a-secret')]));
  await prefetch;
  await a.run("selectFolder('sent')");
  assert.equal(a.run('state.messages[0].uid'), 'b-Sent');
  assert.equal(a.run(`folderCache.has(cacheKey('b','sent')) && folderCache.get(cacheKey('b','sent')).messages[0].uid`), 'b-Sent');
  assert.equal(a.run(`folderCache.get(cacheKey('a','sent'))`), undefined);
});

test('a message removed from a folder stays gone when going back to it', async () => {
  const a = app();
  a.run("state.folders.push({key:'sent',server:'Sent',name:'Sent'})");
  a.api.list_messages = async (_, server) => response(server === 'INBOX' ? [mail('keep'), mail('gone')] : [mail('s')]);
  await a.run("selectFolder('inbox')");
  a.run("state.messages = state.messages.filter(m => m.uid !== 'gone')");
  await a.run("selectFolder('sent')");
  a.api.list_messages = () => new Promise(() => {});
  a.run("selectFolder('inbox')");
  assert.equal(a.run("$('msg-list').innerHTML"), 'Message keep');
});

test('a cached folder refresh that loses to opening a message is read again', async () => {
  const a = app();
  a.run("state.folders.push({key:'sent',server:'Sent',name:'Sent'})");
  a.api.list_messages = async (_, server) => response([mail(server + '-old')]);
  await a.run("selectFolder('inbox')");
  await a.run("selectFolder('sent')");
  const stale = deferred();
  a.api.list_messages = () => stale.promise;
  const back = a.run("selectFolder('inbox')");
  await a.run("mailAction(async () => {})");  // e.g. reading a message
  a.api.list_messages = async () => response([mail('INBOX-new')]);
  stale.resolve(response([mail('INBOX-stale')]));
  await back; await tick();
  assert.equal(a.run('state.messages[0].uid'), 'INBOX-new');
});

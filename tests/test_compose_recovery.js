const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/compose.js'),'utf8');

function app() {
  const elements = new Map();
  const cache = new Map();
  const context = vm.createContext({
    console, crypto: {randomUUID:()=> 'draft-id'}, setTimeout:()=>1, clearTimeout(){},
    localStorage: {setItem:(key,value)=>cache.set(key,value),getItem:key=>cache.get(key),
      removeItem:key=>cache.delete(key),key:n=>[...cache.keys()][n],get length(){return cache.size;}},
    $:id=> {if(!elements.has(id)) elements.set(id,{value:'',textContent:'',contentEditable:'true',addEventListener(){},classList:{remove(){}}});return elements.get(id);},
    document:{querySelectorAll:()=>[]},
    composeText:()=>elements.get('compose-body')?.value || '',
    composeHtml:()=>elements.get('compose-body')?.value || '',
    state:{currentFolder:'inbox'}, api:{}, toast(){},
  });
  const run = code=>vm.runInContext(code,context);
  run(source);
  run("$('compose-to').value='person@example.com'; $('compose-subject').value='Subject'; $('compose-body').value='First'; startComposeSession('one')");
  return {run,context,cache,api:run('api')};
}

test('typing immediately writes a recovery snapshot before native autosave',()=>{
  const a=app(); a.run('composeChanged()');
  assert.equal(a.cache.size,1);
  const cached=JSON.parse([...a.cache.values()][0]);
  assert.equal(cached.payload.body,'First');
  assert.equal(cached.account_id,'one');
});

test('serialized saves preserve newer typing while an earlier save is in flight',async()=>{
  const a=app(); let resolve;const calls=[];
  a.api.save_compose_draft=(...args)=>{
    calls.push(args);
    return calls.length===1 ? new Promise(yes=>{resolve=yes}) : Promise.resolve({revision:2});
  };
  a.run('composeChanged()');
  const first=a.run('persistCompose()');
  await new Promise(yes=>setImmediate(yes));
  a.run("$('compose-body').value='Newer'; composeChanged()");
  const next=a.run('persistCompose()');
  resolve({revision:1});
  await Promise.all([first,next]);
  assert.equal(calls.length,2);
  assert.equal(calls[1][2],1);
  assert.equal(calls[1][5],'Newer');
  assert.equal(JSON.parse([...a.cache.values()][0]).payload.body,'Newer');
  assert.equal(a.run("$('compose-save-status').textContent"),'Saved on this device');
});

test('failed saving leaves recovery intact and permits a later retry',async()=>{
  const a=app();a.run('composeChanged()');
  a.api.save_compose_draft=async()=>{throw Error('Disk unavailable');};
  await assert.rejects(a.run('persistCompose()'));
  assert.equal(a.cache.size,1);
  assert.match(a.run("$('compose-save-status').textContent"),/Not saved/);
  a.api.save_compose_draft=async()=>({revision:1});
  await a.run('persistCompose()');
  assert.equal(a.run('composeSession.revision'),1);
});

test('recovery entries remain account scoped and cannot mask uncertain sends',()=>{
  const a=app();a.run('composeChanged()');
  assert.equal(a.run("composeRecoveryDrafts('two',[]).length"),0);
  const pending=a.run("composeRecoveryDrafts('one',[])");
  assert.equal(pending.length,1);
  a.context.nativeDraft={...pending[0],status:'uncertain'};
  assert.equal(a.run("composeRecoveryDrafts('one',[nativeDraft])[0].status"),'uncertain');
});

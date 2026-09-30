const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname,'../web/conversation.js'),'utf8');
function app() {
  const elements=new Map();
  const ctx=vm.createContext({
    $:id=>{if(!elements.has(id))elements.set(id,{open:false,hidden:false,disabled:false,textContent:'',innerHTML:'',addEventListener(){}});return elements.get(id);},
    state:{activeAccountId:'one',selectedUid:'3'},api:{},shortFrom:x=>x,
    escapeHtml:x=>String(x||'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
  });
  const run=code=>vm.runInContext(code,ctx);
  run(source);run("resetConversation('one','3','seed')");
  return {run,api:run('api')};
}
test('conversation renders escaped bodies and follows continuation cursors',async()=>{
  const a=app();let calls=0;
  a.api.get_conversation=async(account,ref,cursor)=>{
    calls++;return {items:[{message_ref:'msg'+calls,sender:'Person',folder:calls===1?'INBOX':'Sent',
      date:'2026-09-30',text:'<img onerror=attack()>',truncated:calls===1}],next_cursor:calls===1?'next':null};
  };
  await a.run('loadConversation()');
  assert.equal(a.run("$('conversation-more').hidden"),false);
  assert(a.run("$('conversation-items').innerHTML").includes('&lt;img'));
  assert(!a.run("$('conversation-items').innerHTML").includes('<img'));
  await a.run('loadConversation()');
  await a.run('loadConversation()');
  assert.equal(calls,2);
  assert.equal(a.run('conversationView.items.length'),2);
  assert.equal(a.run("$('conversation-more').hidden"),true);
});
test('late conversation cannot replace another selected message',async()=>{
  const a=app();let resolve;
  a.api.get_conversation=()=>new Promise(yes=>resolve=yes);
  const loading=a.run('loadConversation()');
  a.run("state.selectedUid='4';resetConversation('one','4','new-seed')");
  resolve({items:[{message_ref:'old',text:'Old message'}],next_cursor:null});
  await loading;
  assert.equal(a.run('conversationView.messageRef'),'new-seed');
  assert.equal(a.run('conversationView.items.length'),0);
});
test('temporary errors show retry and leave continuation unchanged',async()=>{
  const a=app();a.api.get_conversation=async()=>{throw Error('Offline');};
  await a.run('loadConversation()');
  assert.equal(a.run("$('conversation-more').hidden"),false);
  assert.match(a.run("$('conversation-items').textContent"),/Offline/);
  a.api.get_conversation=async()=>({items:[],next_cursor:'continue'});
  await a.run('loadConversation()');
  assert.equal(a.run('conversationView.cursor'),'continue');
});

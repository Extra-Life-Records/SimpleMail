const {chromium} = require(process.env.SIMPLEMAIL_PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({headless:true, ...(process.env.SIMPLEMAIL_CHROMIUM ? {executablePath:process.env.SIMPLEMAIL_CHROMIUM} : {})});
  try {
    const page = await browser.newPage({viewport:{width:1000,height:700}});
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.searchCalls = []; window.pendingSearch = null;
      const result = {uid:'late',server_uid:'late',server_folder:'Sent',sender:'Example',subject:'Late indexed (example)',date:'2025-04-24',seen:true,snippet:'Sent'};
      const accounts = [{id:'cloud:one',provider:'cloud',label:'Cloud fixture',email:'one@example.invalid'},
                        {id:'other',label:'Other fixture',email:'other@example.invalid'}];
      window.pywebview = {api:{
        get_config:async()=>({accounts,active_account:'cloud:one'}), set_active_account:async()=>{},check_update:async()=>({available:false}),
        get_folders:async()=>({folders:[{key:'inbox',server:'Inbox',name:'Inbox',unread:0}]}),
        list_messages:async()=>({envelopes:[{...result,uid:'ordinary',subject:'Ordinary inbox'}]}),list_compose_drafts:async()=>[],
        search_messages:async(account,query,cursor)=>{
          searchCalls.push({account,query,cursor});
          const n=cursor?Number(cursor)+1:1;
          if(query==='sparse') return {items:n===6||n===14?[result]:[],next_cursor:n===17?null:String(n),
            search_progress:{phase:n<=7?'index':'body',scanned:n<=7?100:10,next_phase:n<7?'index':'body'}};
          if(query==='none' && n===1) return {items:[],next_cursor:'1',search_progress:{phase:'index',scanned:100,next_phase:'body'}};
          if(query==='none' || query==='pause' || query==='old') return new Promise(resolve=>{
            window.pendingSearch=()=>{window.pendingSearch=null;resolve({items:query==='old'?[result]:[],next_cursor:query==='pause'&&!cursor?'1':null});};
          });
          return {items:[],next_cursor:null};
        },
        send_compose_draft:async()=>{throw Error('Sending must not be called');},
      }};
    });
    await page.goto(require('node:url').pathToFileURL(path.join(__dirname,'../web/index.html')).href);
    await page.getByText('Ordinary inbox',{exact:true}).waitFor();
    const search=page.getByRole('searchbox',{name:'Search mail'});
    await search.fill('sparse');
    await page.getByRole('button',{name:'Continue search',exact:true}).waitFor();
    assert.equal(await page.locator('.msg').count(),1);
    assert.equal(await page.evaluate(()=>searchCalls.filter(c=>c.query==='sparse').length),12);
    assert.match(await page.getByRole('status').innerText(),/full text checked/);
    await page.getByRole('button',{name:'Continue search',exact:true}).click();
    await page.waitForFunction(()=>mailSearch && !mailSearch.busy && !mailSearch.cursor);
    assert.equal(await page.locator('.msg').count(),1);
    assert.equal(await page.locator('#folder-count').innerText(),'1 found');
    await search.fill('none');
    await page.waitForFunction(()=>window.pendingSearch);
    assert.equal(await page.getByText('No matches',{exact:true}).count(),0);
    await page.getByRole('button',{name:'Pause search',exact:true}).waitFor();
    await page.evaluate(()=>pendingSearch());
    await page.getByText('No matches',{exact:true}).waitFor();
    await search.fill('pause'); await page.waitForFunction(()=>window.pendingSearch);
    await page.getByRole('button',{name:'Pause search',exact:true}).click();
    await page.evaluate(()=>pendingSearch());
    await page.getByRole('button',{name:'Continue search',exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>searchCalls.filter(c=>c.query==='pause').length),1);
    await page.getByText('Search is not finished',{exact:true}).waitFor();
    await search.fill('old'); await page.waitForFunction(()=>window.pendingSearch);
    await page.evaluate(()=>{window.resolveOld=window.pendingSearch;window.pendingSearch=null;});
    await search.fill('new'); await page.getByText('No matches',{exact:true}).waitFor();
    await page.evaluate(()=>resolveOld());
    assert.equal(await page.locator('.msg').count(),0);
    await search.fill('old'); await page.waitForFunction(()=>window.pendingSearch);
    await page.locator('#accounts button').nth(1).click();
    await page.getByText('Ordinary inbox',{exact:true}).waitFor();
    await page.evaluate(()=>pendingSearch());
    assert.equal(await page.locator('.msg').count(),1);
    assert.equal(await page.getByText('Late indexed (example)',{exact:true}).count(),0);
    assert.deepEqual(errors,[]);
    console.log('Cloud search: automatic sparse match, bounded continuation, visible progress, definitive exhaustion, pause and stale query/mailbox guards passed');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});

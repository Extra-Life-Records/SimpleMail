const {chromium}=require(process.env.SIMPLEMAIL_PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1300,height:850}});
  await page.context().route('https://example.invalid/**', r=>r.fulfill({status:200,body:'Fixture destination'}));
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
   const accounts=[{id:'hello',label:'Extra Life Records',identity:'hello@extraliferecords.com'},
    {id:'play',label:'Playloudr',identity:'hello@playloudr.com'},
    ...['one','two','three','four','five'].map(n=>({id:'cloud:'+n,label:n+'@extraliferecords.com',identity:n+'@extraliferecords.com',provider:'cloud'}))];
   window.calls=[];
   window.pywebview={api:{
    get_config:async()=>({accounts,active_account:'hello',cloud_configured:true}),
    set_active_account:async()=>{},check_update:async()=>({available:false}),
    get_folders:async()=>({folders:['Inbox','Sent','Drafts','Junk','Trash'].map(n=>({key:n.toLowerCase(),name:n,server:n,unread:0}))}),
    list_messages:async(id)=>({envelopes:[{uid:'abc',sender:'Sender <sender@example.invalid>',subject:'Message for '+id,date:'2026-10-07',seen:true}]}),
    get_message:async(id)=>({subject:'Message for '+id,sender:'Sender',to:id,date:'Today',text:'Body for '+id,html:id==='cloud:one'?'<p>Body for cloud:one</p><a href="https://example.invalid/accept?token=fixture">Accept invite</a><script>parent.attacked=true</script>':null,attachments:[]}),
    search_messages:async(id)=>({items:[{uid:'abc',server_uid:'abc',server_folder:'Inbox',sender:'Sender',subject:'Search '+id,seen:true}],next_cursor:null}),
    get_compose_context:async(id)=>({to:'sender@example.invalid',subject:'Reply for '+id,text:'Quoted '+id,sender:'Sender',date:'Today'}),
    save_compose_draft:async(...args)=>{calls.push(args);return {id:args[1],revision:args[2]+1};},
    list_compose_drafts:async()=>[]
   }};
  });
  await page.goto(require('node:url').pathToFileURL(require('node:path').join(__dirname,'../web/index.html')).href);
  await page.getByText('Message for hello',{exact:true}).waitFor();
  assert.equal(await page.locator('#accounts button').count(),7);
  assert.equal(await page.locator('#aws-inbox-btn').count(),0);
  for(const name of ['one','two','three','four','five']) {
   await page.locator('#accounts').getByRole('button',{name:name+'@extraliferecords.com',exact:true}).click();
   await page.getByText('Message for cloud:'+name,{exact:true}).click();
   if(name==='one') {
    const frame=page.frameLocator('#read-body iframe');
    await frame.getByText('Body for cloud:one',{exact:true}).waitFor();
    const popupPromise=page.waitForEvent('popup');
    await frame.getByRole('link',{name:'Accept invite'}).click();
    const popup=await popupPromise; await popup.waitForLoadState();
    assert.equal(popup.url(),'https://example.invalid/accept?token=fixture');
    await popup.close();
    assert.equal(await page.evaluate(()=>Boolean(window.attacked)),false);
   } else await page.getByText('Body for cloud:'+name,{exact:true}).waitFor();
  }
  await page.locator('#reply-btn').click();
  await page.locator('#compose-backdrop.show').waitFor();
  assert((await page.locator('#compose-backdrop').innerText()).includes('five@extraliferecords.com'));
  await page.getByRole('button',{name:'Keep draft',exact:true}).click();
  await page.locator('#compose-backdrop').waitFor({state:'hidden'});
  assert.equal((await page.evaluate(()=>calls))[0][0],'cloud:five');
  await page.locator('#search-box').fill('find');
  await page.getByText('Search cloud:five',{exact:true}).waitFor();
  await page.locator('#accounts').getByRole('button',{name:'Extra Life Records',exact:true}).click();
  await page.getByText('Message for hello',{exact:true}).waitFor();
  assert.equal(await page.getByText('Body for cloud:five',{exact:true}).count(),0);
  assert.deepEqual(errors,[]);
  console.log('Unified sidebar, five inbox readers, reply identity, drafts, search and return to IMAP passed.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});


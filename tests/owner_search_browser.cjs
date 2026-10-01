const {chromium}=require(process.env.SIMPLEMAIL_PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.SIMPLEMAIL_CHROMIUM?{executablePath:process.env.SIMPLEMAIL_CHROMIUM}:{})});
 try {
  const page=await browser.newPage({viewport:{width:980,height:620}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
   window.searchCalls=[];window.actionCalls=[];let continuationFailed=false;
   const result=(folder,n)=>({uid:`search:${folder}-${n}`,server_uid:String(n),server_folder:folder,
      validity:folder==='INBOX'?'7':'8',sender:'Sender <sender@example.invalid>',subject:`Found ${folder} ${n}`,date:'2026-10-01',seen:false,snippet:folder});
   window.pywebview={api:{
    get_config:async()=>({accounts:[{id:'fixture',label:'Fixture',email:'owner@example.invalid'}],active_account:'fixture'}),
    set_active_account:async()=>{},check_update:async()=>({available:false}),
    get_folders:async()=>({folders:[{key:'inbox',server:'INBOX',name:'Inbox',unread:50},{key:'archive',server:'Archive',name:'Archive',unread:25},{key:'junk',server:'Junk',name:'Junk',unread:0}]}),
    list_messages:async()=>({envelopes:[{uid:'99',sender:'Person',subject:'Loaded Inbox message',date:'2026-10-01',seen:true}],folder_unread:50,inbox_unread:50,validity:'7'}),
    search_messages:async(account,query,cursor,folder)=>{
     searchCalls.push({account,query,cursor,folder});
     if(query==='empty')return {items:[],next_cursor:null};
     if(query==='slow')return new Promise(resolve=>window.resolveOldSearch=()=>resolve({items:[result('INBOX',999)],next_cursor:null}));
     if(folder==='Junk')return {items:[{...result('Junk',1),subject:'Found Junk 1'}],next_cursor:null};
     if(cursor){if(!continuationFailed){continuationFailed=true;throw Error('Temporary search failure');}return {items:[result('Archive',2)],next_cursor:null};}
     return {items:[result('INBOX',1),result('Archive',1),...Array.from({length:23},(_,i)=>result('INBOX',i+2))],next_cursor:'page-two'};
    },
    get_message:async(...args)=>{actionCalls.push({action:'read',args});return {subject:'Full result',sender:'Sender',to:'Owner',date:'Today',text:'Body contains body-only',attachments:[],message_ref:'result-reference'};},
    set_seen:async(...args)=>{actionCalls.push({action:'unread',args});return {ok:true};},
    get_compose_context:async(...args)=>{actionCalls.push({action:'quote',args});return {subject:'Full result',sender:'Sender',date:'Today',text:'Body contains body-only',to:'sender@example.invalid',cc:'colleague@example.invalid',in_reply_to:'<original@example.invalid>',references:'<original@example.invalid>',attachments:[]};},
    mark_all_read:async()=>{throw Error('Search must not mark an entire folder read');}
    ,save_compose_draft:async(...args)=>{actionCalls.push({action:'draft',args});return {id:args[1],revision:args[2]+1};}
   }};
  });
  await page.goto(require('node:url').pathToFileURL(require('node:path').join(__dirname,'../web/index.html')).href);
  await page.getByText('Loaded Inbox message',{exact:true}).waitFor();
  await page.getByRole('searchbox',{name:'Search mail'}).fill('body-only');
  await page.getByText('Found Archive 1',{exact:true}).waitFor();
  assert.equal(await page.locator('.msg').count(),25);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.equal(await page.locator('#markall-btn').isDisabled(),true);
  await page.getByText('Found Archive 1',{exact:true}).click();
  await page.getByText('Body contains body-only',{exact:true}).waitFor();
  assert.deepEqual((await page.evaluate(()=>actionCalls))[0],{action:'read',args:['fixture','Archive','1','8']});
  await page.getByRole('button',{name:'Load more results',exact:true}).click();
  await page.getByRole('button',{name:'Try search again',exact:true}).waitFor();
  assert.equal(await page.locator('.msg').count(),25);
  await page.getByRole('button',{name:'Try search again',exact:true}).click();
  await page.getByText('Found Archive 2',{exact:true}).waitFor();
  assert.equal(await page.locator('.msg').count(),26);
  assert.equal(await page.getByRole('button',{name:'Load more results',exact:true}).count(),0);
  await page.locator('#unread-btn').click();
  assert.deepEqual((await page.evaluate(()=>actionCalls)).find(x=>x.action==='unread'),{action:'unread',args:['fixture','Archive','1',false,'8']});
  await page.locator('#reply-all-btn').click();
  await page.locator('#compose-backdrop.show').waitFor();
  assert.deepEqual((await page.evaluate(()=>actionCalls)).find(x=>x.action==='quote'),{action:'quote',args:['fixture','Archive','1',true,false,'8']});
  assert.equal(await page.locator('#compose-to').inputValue(),'sender@example.invalid');
  await page.getByRole('button',{name:'Keep draft',exact:true}).click();
  await page.locator('#compose-backdrop').waitFor({state:'hidden'});
  const saved=(await page.evaluate(()=>actionCalls)).find(x=>x.action==='draft').args;
  assert.equal(saved[3],'sender@example.invalid');assert.equal(saved[7],'colleague@example.invalid');
  assert.equal(saved[9],'<original@example.invalid>');
  await page.getByRole('searchbox',{name:'Search mail'}).fill('slow');
  await page.waitForFunction(()=>window.resolveOldSearch);
  await page.getByRole('searchbox',{name:'Search mail'}).fill('empty');
  await page.getByText('No matches',{exact:true}).waitFor();
  await page.evaluate(()=>resolveOldSearch());
  assert.equal(await page.locator('.msg').count(),0);
  await page.getByRole('searchbox',{name:'Search mail'}).fill('');
  await page.getByText('Loaded Inbox message',{exact:true}).waitFor();
  assert.equal(await page.locator('#markall-btn').isDisabled(),false);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({bodyOnlyResults:true,completePaging:true,retryPreservesResults:true,sourceIdentity:true,replyAllSource:true,staleSearchBlocked:true,clearRestoresInbox:true,errors,passed:true}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
